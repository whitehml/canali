"""Searches for the constants the engine runs on"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy

from epa.evaluate import Prediction, score_metrics
from epa.model import Schedule
from epa.norm import INIT_PENALTY, init_norm
from epa.scale import Carryover, LayoffBoost


class Scored(Protocol):
    """A grid candidate: what it is called, and what it scored in each season."""

    @property
    def label(self) -> str: ...

    @property
    def mse_by_season(self) -> dict[int, float]: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ``(K, M)`` pair and how it scored."""

    k: Schedule
    m: Schedule
    mse_by_season: dict[int, float]

    @property
    def label(self) -> str:
        return f"K={self.k.values} M={self.m.values}"

    @property
    def pooled_mse(self) -> float:
        """Unweighted: a season is the unit of generalization, so weighting by rows would let the largest decide."""
        return statistics.fmean(self.mse_by_season.values())

    def describe(self) -> str:
        per = ", ".join(f"{s}:{v:.1f}" for s, v in sorted(self.mse_by_season.items()))
        return f"{self.label} pooled={self.pooled_mse:.2f} ({per})"


@dataclass(frozen=True, slots=True)
class LayoffCandidate:
    """One ``LayoffBoost`` and how it scored.

    The signed residual is reported beside MSE because the term exists to remove a bias rather than to reduce
    variance.
    """

    boost: LayoffBoost
    mse_by_season: dict[int, float]
    signed_by_season: dict[int, float]

    @property
    def label(self) -> str:
        return f"sigma_per_30d={self.boost.sigma_per_30d:.3f} cap={self.boost.cap_days:g}d"

    @property
    def pooled_mse(self) -> float:
        return statistics.fmean(self.mse_by_season.values())

    @property
    def pooled_signed(self) -> float:
        return statistics.fmean(self.signed_by_season.values())

    def describe(self) -> str:
        per = ", ".join(f"{s}:{v:.1f}" for s, v in sorted(self.mse_by_season.items()))
        return f"{self.label} pooled={self.pooled_mse:.2f} signed={self.pooled_signed:+.2f} ({per})"


@dataclass(frozen=True, slots=True)
class LosoResult:
    """What a candidate chosen without a season scores on that season."""

    held_out: int
    chosen: str
    held_out_mse: float
    in_sample_mse: float

    @property
    def optimism(self) -> float:
        """How much better the in-sample number looks than the honest one."""
        return self.held_out_mse - self.in_sample_mse

    def summary(self) -> str:
        return (
            f"holding out {self.held_out}: chose {self.chosen}; held-out MSE {self.held_out_mse:.2f} "
            f"against in-sample {self.in_sample_mse:.2f} (optimism {self.optimism:+.2f})"
        )


def search_schedules(
    seasons: Sequence[int],
    replay_with: Callable[[int, Schedule, Schedule], Sequence[Prediction]],
    *,
    k_grid: Sequence[tuple[float, ...]],
    m_grid: Sequence[tuple[float, ...]],
    breakpoints: tuple[int, ...] = (4, 8),
) -> list[Candidate]:
    """Grid search over K and M, scored per season and returned best-first."""
    results: list[Candidate] = []
    for k_values, m_values in itertools.product(k_grid, m_grid):
        k = Schedule(breakpoints=breakpoints, values=k_values)
        m = Schedule(breakpoints=breakpoints, values=m_values)
        results.append(
            Candidate(
                k=k,
                m=m,
                mse_by_season={season: score_metrics(replay_with(season, k, m))[0] for season in seasons},
            )
        )
    return sorted(results, key=lambda c: c.pooled_mse)


def search_layoff(
    seasons: Sequence[int],
    replay_with: Callable[[int, LayoffBoost], Sequence[Prediction]],
    *,
    sigma_grid: Sequence[float],
    cap_grid: Sequence[float],
) -> list[LayoffCandidate]:
    """Grid search over the layoff boost, scored per season and returned best-first.

    ``sigma_grid`` must contain zero, so the report always says what turning the term off costs.
    """
    results: list[LayoffCandidate] = []
    for sigma, cap in itertools.product(sigma_grid, cap_grid):
        boost = LayoffBoost(sigma_per_30d=sigma, cap_days=cap)
        mse: dict[int, float] = {}
        signed: dict[int, float] = {}
        for season in seasons:
            predictions = replay_with(season, boost)
            mse[season] = score_metrics(predictions)[0]
            signed[season] = statistics.fmean(p.error for p in predictions) if predictions else float("nan")
        results.append(LayoffCandidate(boost=boost, mse_by_season=mse, signed_by_season=signed))
    return sorted(results, key=lambda c: c.pooled_mse)


def leave_one_season_out(candidates: Sequence[Scored], seasons: Sequence[int]) -> list[LosoResult]:
    """Choose each season's candidate on the other seasons, then report what it scores on that one.

    Nothing is replayed: the grid already holds every candidate's per-season MSE, and selection is the only thing that
    has to be made blind.
    """
    out: list[LosoResult] = []
    for held in seasons:
        others = [season for season in seasons if season != held]
        if not others:
            continue
        best = min(candidates, key=lambda c: statistics.fmean([c.mse_by_season[s] for s in others]))
        out.append(
            LosoResult(
                held_out=held,
                chosen=best.label,
                held_out_mse=best.mse_by_season[held],
                in_sample_mse=statistics.fmean([best.mse_by_season[s] for s in others]),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class TransitionFit:
    """One season transition's carryover estimate.

    ``start = (1 - r) * [w * n1 + (1 - w) * n2] + r * init`` is linear in the two prior seasons, so it is fitted as
    ``start = A*n1 + B*n2 + c`` and recovered as ``r = 1 - (A + B)``, ``w = A / (A + B)``.
    """

    from_season: int
    to_season: int
    year_one_weight: float
    mean_reversion: float
    n: int
    weight_stderr: float

    def interval(self, z: float = 1.96) -> tuple[float, float]:
        return (
            self.year_one_weight - z * self.weight_stderr,
            self.year_one_weight + z * self.weight_stderr,
        )

    def summary(self) -> str:
        lo, hi = self.interval()
        return (
            f"{self.from_season}->{self.to_season}: w={self.year_one_weight:.4f} (95% CI {lo:.4f}, {hi:.4f})  "
            f"reversion={self.mean_reversion:.4f}  n={self.n}"
        )


@dataclass(frozen=True, slots=True)
class CarryoverFit:
    """Every transition's estimate."""

    transitions: tuple[TransitionFit, ...]
    init_penalty: float

    @property
    def agree(self) -> bool:
        """Whether the per-transition intervals on ``w`` overlap pairwise."""
        for left, right in itertools.combinations(self.transitions, 2):
            lo1, hi1 = left.interval()
            lo2, hi2 = right.interval()
            if hi1 < lo2 or hi2 < lo1:
                return False
        return True

    @property
    def pooled_weight(self) -> float:
        """NaN with no transitions: a single-season corpus has nothing to estimate carryover from."""
        if not self.transitions:
            return float("nan")
        return statistics.fmean([t.year_one_weight for t in self.transitions])

    @property
    def pooled_reversion(self) -> float:
        if not self.transitions:
            return float("nan")
        return statistics.fmean([t.mean_reversion for t in self.transitions])

    @property
    def conservative_weight(self) -> float:
        """The largest weight on last season, which is the least willing to credit an absent one."""
        if not self.transitions:
            return float("nan")
        return max(t.year_one_weight for t in self.transitions)

    def proposal(self) -> Carryover:
        """What to put in the constants file, pooling only where the transitions agree."""
        return Carryover(
            year_one_weight=self.pooled_weight if self.agree else self.conservative_weight,
            mean_reversion=self.pooled_reversion,
            init_penalty=self.init_penalty,
        )

    def report(self) -> str:
        if not self.transitions:
            return (
                f"no season transitions in this run, so carryover is not estimable; "
                f"init_penalty={self.init_penalty:.4f}"
            )
        lines = [t.summary() for t in self.transitions]
        lines.append(
            f"pooled w={self.pooled_weight:.4f} reversion={self.pooled_reversion:.4f}; "
            f"init_penalty={self.init_penalty:.4f}"
        )
        lines.append(
            "transitions agree, so pooling is defensible"
            if self.agree
            else f"transitions disagree, so pooling would hide it; using w={self.conservative_weight:.4f}"
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _TwoSeasonRegression:
    """``start = slope_one*n1 + slope_two*n2 + intercept``, with the slopes' covariance."""

    slope_one: float
    slope_two: float
    intercept: float
    variance_one: float
    variance_two: float
    covariance: float

    @property
    def carried(self) -> float:
        """``A + B``: how much of a team's past survives into the new season."""
        return self.slope_one + self.slope_two

    def weight_stderr(self) -> float:
        """The standard error of ``w = A / (A + B)``."""
        total = self.carried
        if total <= 0:
            return float("nan")
        gradient_one = self.slope_two / total**2
        gradient_two = -self.slope_one / total**2
        variance = (
            gradient_one**2 * self.variance_one
            + gradient_two**2 * self.variance_two
            + 2 * gradient_one * gradient_two * self.covariance
        )
        return float(max(variance, 0.0) ** 0.5)


def _regress_two_seasons(
    first: Sequence[float], second: Sequence[float], starts: Sequence[float]
) -> _TwoSeasonRegression:
    """Two-predictor ordinary least squares with an intercept."""
    n = len(starts)
    if n < 4:
        raise ValueError(f"a two-predictor regression needs at least 4 points, got {n}")
    a1, a2, target = numpy.asarray(first), numpy.asarray(second), numpy.asarray(starts)
    mean1, mean2, mean_y = float(a1.mean()), float(a2.mean()), float(target.mean())
    design = numpy.column_stack([a1 - mean1, a2 - mean2])
    gram = design.T @ design

    condition = float(numpy.linalg.cond(gram))
    if not numpy.isfinite(condition) or condition > 1e10:
        raise ValueError("the two carried seasons are collinear; no split is identifiable")

    inverse = numpy.linalg.inv(gram)
    beta = inverse @ design.T @ (target - mean_y)
    residuals = (target - mean_y) - design @ beta
    sigma2 = float(residuals @ residuals) / (n - 3)
    slope_one, slope_two = float(beta[0]), float(beta[1])
    return _TwoSeasonRegression(
        slope_one=slope_one,
        slope_two=slope_two,
        intercept=mean_y - slope_one * mean1 - slope_two * mean2,
        variance_one=sigma2 * float(inverse[0, 0]),
        variance_two=sigma2 * float(inverse[1, 1]),
        covariance=sigma2 * float(inverse[0, 1]),
    )


def fit_carryover(
    finals: Mapping[int, Mapping[int, float]],
    starts: Mapping[int, Mapping[int, float]],
    *,
    init_penalty: float = INIT_PENALTY,
) -> CarryoverFit:
    """Fit the norm-rating carryover

    ``finals[season][team]`` is a team's end-of-season norm rating and ``starts[season][team]`` its early-season norm
    performance.
    """
    seasons = sorted(starts)
    transitions: list[TransitionFit] = []
    rookie = init_norm(init_penalty)

    for season in seasons:
        first = finals.get(season - 1)
        second = finals.get(season - 2)
        if first is None or second is None:
            continue
        x1, x2, ys = [], [], []
        for team, value in starts[season].items():
            if team not in first and team not in second:
                continue
            x1.append(first.get(team, rookie))
            x2.append(second.get(team, rookie))
            ys.append(value)
        if len(ys) < 4:
            continue
        try:
            fit = _regress_two_seasons(x1, x2, ys)
        except ValueError:
            continue
        if fit.carried <= 0:
            continue
        transitions.append(
            TransitionFit(
                from_season=season - 1,
                to_season=season,
                year_one_weight=min(1.0, max(0.0, fit.slope_one / fit.carried)),
                mean_reversion=min(1.0, max(0.0, 1.0 - fit.carried)),
                n=len(ys),
                weight_stderr=fit.weight_stderr(),
            )
        )

    return CarryoverFit(transitions=tuple(transitions), init_penalty=init_penalty)
