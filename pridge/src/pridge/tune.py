"""Lambda re-derivation.

`derive_lambda` returns the next-match optimum over a sample of one tier's events.
`observe` records the per-fit leave-one-out selection, whose median over fits with full column rank is a cross-check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from pridge import design
from pridge.constants import lambda_for
from pridge.design import Vector
from pridge.estimator import fit_grid
from pridge.evaluate import next_match_predictions
from pridge.prior import Prior
from warehouse.client import MatchRow


@dataclass(frozen=True, slots=True)
class LambdaGrid:
    """A log-spaced lambda search grid, ascending."""

    low: float
    high: float
    points: int

    def values(self) -> Vector:
        return np.logspace(np.log10(self.low), np.log10(self.high), self.points)


EXPLORATORY_GRID = LambdaGrid(low=1e-6, high=1e8, points=200)
LAMBDA_GRID = LambdaGrid(low=0.4, high=4.0, points=80)


@dataclass(frozen=True, slots=True)
class EventSample:
    """One event's qualification rows and the prior its fits regularize toward."""

    code: str
    rows: Sequence[MatchRow]
    prior: Prior


@dataclass(frozen=True, slots=True)
class LambdaObservation:
    """Where leave-one-out put the optimum for one fit."""

    event_code: str
    as_of_match: int
    n_rows: int
    n_teams: int
    identified: bool
    selected: float


@dataclass(frozen=True, slots=True)
class TuningReport:
    """The per-fit selections across a sample of events.

    `dropped` counts fits that raised. They are the ill-conditioned ones.
    """

    observations: list[LambdaObservation]
    grid: LambdaGrid
    dropped: int = 0

    @property
    def identified(self) -> list[LambdaObservation]:
        """Fits whose design has full column rank, the only ones leave-one-out can select on."""
        return [o for o in self.observations if o.identified]

    @property
    def pinned_low(self) -> int:
        return sum(o.selected <= self.grid.low * 1.000001 for o in self.identified)

    @property
    def pinned_high(self) -> int:
        return sum(o.selected >= self.grid.high * 0.999999 for o in self.identified)

    def median_selected(self) -> float:
        """The median selected lambda over identified fits."""
        selected = [o.selected for o in self.identified]
        if not selected:
            raise ValueError("no identified fit in the sample")
        return float(np.median(selected))


def observe(events: Sequence[EventSample], grid: LambdaGrid = EXPLORATORY_GRID, *, stride: int = 1) -> TuningReport:
    """Record the leave-one-out selected lambda at every `stride`-th match index of every event."""
    values = grid.values()
    observations: list[LambdaObservation] = []
    dropped = 0

    for event in events:
        ordinals = sorted({row.event_match_ordinal for row in event.rows if row.level == design.QUALIFICATION})
        for k in ordinals[::stride]:
            try:
                built = design.build(event.rows, as_of_match=k)
                best, _ = fit_grid(built.X, built.y, event.prior.vector(built.team_numbers), values)
            except (ValueError, LookupError):
                dropped += 1
                continue
            observations.append(
                LambdaObservation(
                    event_code=event.code,
                    as_of_match=k,
                    n_rows=built.n_rows,
                    n_teams=built.n_teams,
                    identified=int(np.linalg.matrix_rank(built.X)) == built.n_teams,
                    selected=best.lam,
                )
            )

    if not observations:
        raise ValueError("no fit produced an observation")
    return TuningReport(observations=observations, grid=grid, dropped=dropped)


@dataclass(frozen=True, slots=True)
class SeasonLambdaReport:
    """Next-match squared error across a lambda grid, per event, for the events of one tier constant.

    Held per event because the bootstrap resamples events: alliances within an event share a fit and are not independent
    draws.
    """

    lambdas: Vector
    sse_by_event: list[Vector]
    n_by_event: list[int]
    event_codes: list[str]
    shipped: float

    @property
    def n(self) -> int:
        return sum(self.n_by_event)

    @property
    def mse(self) -> Vector:
        return np.sum(self.sse_by_event, axis=0) / max(self.n, 1)

    def relative_cost(self) -> Vector:
        """Each lambda's error as a multiple of the best lambda's."""
        mse = self.mse
        return np.asarray(mse / mse.min())

    def pick(self) -> float:
        """The season's argmin."""
        return float(self.lambdas[int(np.argmin(self.mse))])

    def cost_of(self, lam: float) -> float:
        """How much worse the nearest grid point to `lam` is than the season's argmin, as a fraction."""
        return float(self.relative_cost()[int(np.argmin(np.abs(self.lambdas - lam)))] - 1.0)

    def bootstrap_interval(self, level: float = 0.9, n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
        """Percentile interval for the argmin, resampling events with replacement."""
        if not self.sse_by_event:
            raise ValueError("no events measured")
        rng = np.random.default_rng(seed)
        stacked = np.array(self.sse_by_event)
        n_events = len(stacked)
        picks = np.empty(n_boot)
        for b in range(n_boot):
            picks[b] = self.lambdas[int(np.argmin(stacked[rng.integers(0, n_events, n_events)].sum(axis=0)))]
        tail = (1.0 - level) / 2.0
        return float(np.percentile(picks, 100 * tail)), float(np.percentile(picks, 100 * (1.0 - tail)))

    def verdict(self, level: float = 0.9, tolerance: float = 0.02) -> tuple[bool, str]:
        """Whether the season needs its own lambda."""
        cost = self.cost_of(self.shipped)
        low, high = self.bootstrap_interval(level=level)
        if cost > tolerance and not low <= self.shipped <= high:
            return True, (
                f"shipped lambda {self.shipped:g} costs {cost:.2%} against this season's {self.pick():g}, and the "
                f"{level:.0%} interval [{low:g}, {high:g}] excludes it"
            )
        if cost <= tolerance:
            return False, (
                f"shipped lambda {self.shipped:g} is within {cost:.2%} of this season's {self.pick():g}, "
                f"inside the {tolerance:.0%} tolerance"
            )
        return False, (
            f"shipped lambda {self.shipped:g} costs {cost:.2%}, but the {level:.0%} interval [{low:g}, {high:g}] "
            "still contains it"
        )


def derive_lambda(events: Sequence[EventSample], grid: LambdaGrid = LAMBDA_GRID) -> float:
    """The next-match optimum over a sample of events that share one tier constant."""
    return observe_season(events, grid=grid).pick()


def observe_season(
    events: Sequence[EventSample], shipped: float | None = None, grid: LambdaGrid = LAMBDA_GRID
) -> SeasonLambdaReport:
    """Next-match squared error at every candidate lambda, event by event.

    Each lambda is scored on the alliances `next_match_predictions` yields, which do not depend on lambda. `shipped`
    defaults to the constant the events' tier uses, and the events must all share one.
    """
    constants = {lambda_for(event.rows[0].event_type) for event in events}
    if len(constants) != 1:
        raise ValueError(f"events span {len(constants)} tier constants, expected one")
    shipped = constants.pop() if shipped is None else shipped
    values = grid.values()
    sse_by_event: list[Vector] = []
    n_by_event: list[int] = []
    codes: list[str] = []

    for event in events:
        sse = np.zeros(values.size)
        n = 0
        for i, lam in enumerate(values):
            predictions = next_match_predictions(event.rows, event.prior, lam=float(lam))
            sse[i] = sum(p.error * p.error for p in predictions)
            n = len(predictions)
        if n:
            sse_by_event.append(sse)
            n_by_event.append(n)
            codes.append(event.code)

    if not sse_by_event:
        raise ValueError("no event produced a scored prediction")
    return SeasonLambdaReport(
        lambdas=values, sse_by_event=sse_by_event, n_by_event=n_by_event, event_codes=codes, shipped=shipped
    )
