"""An alliance's score drawn around its predicted score."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import fmean

import numpy as np
import numpy.typing as npt
from scipy import special

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]

MIN_FIT_PREDICTION = 5.0

# Bounds on intercept, slope, log dispersion, log of dof above 2, and log skew.
FIT_BOUNDS = ((-50.0, 50.0), (0.2, 3.0), (np.log(0.5), np.log(300.0)), (np.log(0.5), np.log(500.0)), (-1.5, 1.5))

# Gauss-Legendre nodes on (0, 1) for a match's win probability.
_NODES, _WEIGHTS = np.polynomial.legendre.leggauss(32)
QUANTILES, QUANTILE_WEIGHTS = (_NODES + 1.0) / 2.0, _WEIGHTS / 2.0

# Bounds on the foul points' standard deviation, in points.
FOUL_SD_BOUNDS = (0.0, 100.0)

# Foul points come in multiples of this, and a draw below it is no foul at all.
FOUL_STEP = 5.0
FIT_CHUNK = 4096
CLIP = 1e-9


@dataclass(frozen=True, slots=True)
class ScoreModel:
    """A skewed Student-t on an alliance's score, its variance proportional to the predicted score.

    ``score = intercept + slope * mu + sqrt(dispersion * mu) * e``, with ``e`` a Fernandez-Steel skewed t, rounded to
    whole points and floored at zero. The two alliances of a match are drawn independently. The official score adds the
    foul points each alliance receives, a gamma of mean ``foul_mean`` and variance ``foul_variance`` taken to the
    nearest ``FOUL_STEP``, a draw below one step counting as none.

    Attributes:
        intercept: Points added to every alliance's location.
        slope: Location per predicted point.
        dispersion: Variance per predicted point.
        dof: Degrees of freedom of the t.
        skew: Ratio of the right tail's scale to the left's; 1 is symmetric.
        foul_mean: Mean foul points an alliance receives.
        foul_variance: Variance of the foul points an alliance receives, as fitted on official results.
        rows: Alliance-rows the model was fitted on.
        provisional: True when earlier seasons stand in.
    """

    intercept: float
    slope: float
    dispersion: float
    dof: float
    skew: float
    foul_mean: float = 0.0
    foul_variance: float = 0.0
    rows: int = 0
    provisional: bool = False

    def foul_points(self, shape: tuple[int, ...], rng: np.random.Generator) -> Floats:
        """Foul points received by each alliance, in multiples of ``FOUL_STEP`` and never negative."""
        if self.foul_mean <= 0.0 or self.foul_variance <= 0.0:
            return np.zeros(shape)
        drawn = rng.gamma(self.foul_mean**2 / self.foul_variance, self.foul_variance / self.foul_mean, shape)
        return np.where(drawn < FOUL_STEP, 0.0, FOUL_STEP * np.rint(drawn / FOUL_STEP))

    def location(self, mu: Floats) -> Floats:
        return self.intercept + self.slope * np.asarray(mu, dtype=np.float64)

    def scale(self, mu: Floats) -> Floats:
        return np.sqrt(self.dispersion * np.maximum(np.asarray(mu, dtype=np.float64), 0.0))

    def standard_quantile(self, u: Floats) -> Floats:
        """The standard skewed t at probability ``u``."""
        u = np.asarray(u, dtype=np.float64)
        g2 = self.skew**2
        below = u < 1.0 / (1.0 + g2)
        left = special.stdtrit(self.dof, u * (1.0 + g2) / 2.0) / self.skew
        right = special.stdtrit(self.dof, (1.0 + (u - 1.0 / (1.0 + g2)) * (1.0 + 1.0 / g2)) / 2.0) * self.skew
        return np.asarray(np.where(below, left, right), dtype=np.float64)

    def quantile(self, mu: Floats, u: Floats) -> Floats:
        """The score at probability ``u`` around each predicted score, before rounding."""
        return self.location(mu) + self.scale(mu) * self.standard_quantile(u)

    def cdf(self, mu: Floats, score: Floats) -> Floats:
        """The probability of a score at most ``score``, before rounding; rows with no spread are left as NaN."""
        scale = self.scale(mu)
        z = np.divide(
            np.asarray(score, dtype=np.float64) - self.location(mu),
            scale,
            out=np.full(np.shape(scale), np.nan),
            where=scale > 0,
        )
        g2 = self.skew**2
        below = 2.0 / (1.0 + g2) * special.stdtr(self.dof, z * self.skew)
        above = 1.0 / (1.0 + g2) + 2.0 * g2 / (1.0 + g2) * (special.stdtr(self.dof, z / self.skew) - 0.5)
        return np.asarray(np.where(z < 0, below, above), dtype=np.float64)

    def sample(self, mu: Floats, rng: np.random.Generator) -> Ints:
        """One whole-point score per predicted score in ``mu``."""
        mu = np.asarray(mu, dtype=np.float64)
        return self.whole_points(self.quantile(mu, rng.random(mu.shape)))

    @staticmethod
    def whole_points(score: Floats) -> Ints:
        return np.maximum(np.rint(score), 0.0).astype(np.int64)

    def log_density(self, mu: Floats, score: Floats) -> Floats:
        """Log density of each ``score`` around its predicted score, before rounding."""
        from scipy import stats

        scale = self.scale(mu)
        z = (np.asarray(score, dtype=np.float64) - self.location(mu)) / scale
        skewed = np.where(z < 0, z * self.skew, z / self.skew)
        density = stats.t.logpdf(skewed, self.dof) + np.log(2.0 / (self.skew + 1.0 / self.skew)) - np.log(scale)
        return np.asarray(density, dtype=np.float64)


def _from_vector(w: Floats, rows: int = 0) -> ScoreModel:
    return ScoreModel(
        intercept=float(w[0]),
        slope=float(w[1]),
        dispersion=float(np.exp(w[2])),
        dof=2.0 + float(np.exp(w[3])),
        skew=float(np.exp(w[4])),
        rows=rows,
    )


def _to_vector(model: ScoreModel) -> Floats:
    return np.array(
        [model.intercept, model.slope, np.log(model.dispersion), np.log(model.dof - 2.0), np.log(model.skew)]
    )


def fit_score_model(mu: Floats, score: Floats, *, start: ScoreModel | None = None) -> ScoreModel:
    """The model that maximizes the likelihood of these rows, those predicted below ``MIN_FIT_PREDICTION`` left out."""
    from scipy import optimize

    keep = np.asarray(mu, dtype=np.float64) > MIN_FIT_PREDICTION
    mu, score = np.asarray(mu, dtype=np.float64)[keep], np.asarray(score, dtype=np.float64)[keep]
    if len(mu) == 0:
        raise ValueError("no rows to fit a score model on")
    first = start or ScoreModel(intercept=0.0, slope=1.0, dispersion=10.0, dof=10.0, skew=1.0)

    def loss(w: Floats) -> float:
        return -float(_from_vector(w).log_density(mu, score).sum())

    w = optimize.minimize(loss, _to_vector(first), method="L-BFGS-B", bounds=FIT_BOUNDS).x
    return _from_vector(w, rows=len(mu))


def fit_foul_variance(model: ScoreModel, mu: Floats, outcome: Floats) -> float:
    """The foul points variance that minimizes log loss on official results, foul points taken as a normal.

    Only the two alliances' difference in foul points changes a result, so their mean cancels. ``mu`` is each match's
    predicted scores shaped ``(match, alliance)``, and ``outcome`` the first alliance's official
    result: 1 for a win, a half for a tie, 0 for a loss.
    """
    from scipy import optimize

    mu = np.asarray(mu, dtype=np.float64)
    outcome = np.asarray(outcome, dtype=np.float64)
    if len(mu) == 0:
        raise ValueError("no matches to fit the foul variance on")
    first = model.quantile(mu[:, :1], QUANTILES[None, :])
    second = model.quantile(mu[:, 1:], QUANTILES[None, :])
    weights = np.outer(QUANTILE_WEIGHTS, QUANTILE_WEIGHTS)

    def loss(sd: float) -> float:
        total = 0.0
        for start in range(0, len(mu), FIT_CHUNK):
            chunk = slice(start, start + FIT_CHUNK)
            margin = first[chunk, :, None] - second[chunk, None, :]
            p = np.einsum("mij,ij->m", special.ndtr(margin / max(sd * np.sqrt(2.0), CLIP)), weights)
            p = np.clip(p, CLIP, 1.0 - CLIP)
            o = outcome[chunk]
            total -= float(np.sum(o * np.log(p) + (1.0 - o) * np.log(1.0 - p)))
        return total / len(mu)

    sd = optimize.minimize_scalar(loss, bounds=FOUL_SD_BOUNDS, method="bounded").x
    return float(sd**2)


def provisional_score_model(previous: Sequence[ScoreModel]) -> ScoreModel:
    """The stand-in until a season has enough rows: earlier seasons' models averaged."""
    if not previous:
        raise ValueError("no earlier season's score model to stand in")
    return ScoreModel(
        intercept=fmean(m.intercept for m in previous),
        slope=fmean(m.slope for m in previous),
        dispersion=fmean(m.dispersion for m in previous),
        dof=fmean(m.dof for m in previous),
        skew=fmean(m.skew for m in previous),
        foul_mean=fmean(m.foul_mean for m in previous),
        foul_variance=fmean(m.foul_variance for m in previous),
        provisional=True,
    )


def resolve_score_model(
    mu: Floats,
    score: Floats,
    *,
    match_mu: Floats,
    outcome: Floats,
    foul_points: Floats,
    min_rows: int,
    previous: Sequence[ScoreModel],
) -> ScoreModel:
    """The season's model and foul points from its rows so far, or the earlier seasons' until it has ``min_rows``.

    ``match_mu`` and ``outcome`` are the matches the foul variance is fitted on, as ``fit_foul_variance`` takes them,
    and ``foul_points`` each row's foul points received.
    """
    if len(mu) < min_rows:
        return provisional_score_model(previous)
    model = fit_score_model(mu, score, start=provisional_score_model(previous) if previous else None)
    return replace(
        model,
        foul_mean=float(np.mean(foul_points)),
        foul_variance=fit_foul_variance(model, match_mu, outcome),
    )
