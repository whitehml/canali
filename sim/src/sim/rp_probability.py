"""Bonus ranking point probability."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import fmean

import numpy as np
import numpy.typing as npt
from scipy.special import ndtr, ndtri

from sim.rp_source import Identity, Recovery, RpSource
from warehouse.rules.model import to_column_name

Floats = npt.NDArray[np.float64]

HALF_STEP = 0.5
BUCKETS = 10
LOWEST_CAP_BUCKET_INDEX = 6
CLIP = 1e-9
IDENTITY = Identity()
CORRELATION_BOUND = 0.99


@dataclass(frozen=True, slots=True)
class RpCurve:
    """A generic model on an alliance's predicted points for an RP to estimate the RP's probability.

    The RP's threshold is turned into points by the curve's recovery, and is meant to be a reasonable first aproximation
    from coarse data.

    Attributes:
        shift: Points added to the predicted points.
        log_sd: Log of the spread at zero predicted points.
        log_sd_slope: Growth of the log spread per predicted point.
        cap: The highest probability the curve gives.
        rows: Alliance-rows the curve was fitted on.
        provisional: True when earlier seasons stand in.
        recovery: How the RP's quantity is recovered from points.
        correlation: How closely earning the RP follows the alliance's score draw, on the latent normal scale.
    """

    shift: float
    log_sd: float
    log_sd_slope: float
    cap: float = 1.0
    rows: int = 0
    provisional: bool = False
    recovery: Recovery = IDENTITY
    correlation: float = 0.0

    def points_threshold(self, threshold: float | Floats) -> Floats:
        """Each RP threshold as the fewest points whose recovered quantity reaches it."""
        t = np.asarray(threshold, dtype=np.float64)
        unique, inverse = np.unique(t, return_inverse=True)
        converted = np.array([self.recovery.points_threshold(float(u)) for u in unique], dtype=np.float64)
        return converted[inverse].reshape(t.shape)

    def uncapped_at(self, points: Floats, points_threshold: float | Floats) -> Floats:
        """The uncapped chance each alliance reaches a threshold already in points."""
        p = np.asarray(points, dtype=np.float64)
        sd = np.exp(self.log_sd + self.log_sd_slope * p)
        return np.asarray(ndtr((p + self.shift - (np.asarray(points_threshold) - HALF_STEP)) / sd), dtype=np.float64)

    def uncapped(self, points: Floats, threshold: float | Floats) -> Floats:
        return self.uncapped_at(points, self.points_threshold(threshold))

    def probability(self, points: Floats, threshold: float | Floats) -> Floats:
        """The chance each alliance reaches the RP's ``threshold``."""
        return np.minimum(self.uncapped(points, threshold), self.cap)


def points_sum(source: RpSource, *teams: Mapping[str, float]) -> float:
    """The alliance's predicted points for an RP, from each team's predicted components keyed by column name."""
    columns = [to_column_name(name) for name in source.points_from]
    missing = sorted({c for c in columns for team in teams if c not in team})
    if missing:
        raise ValueError(f"predictions lack {missing}")
    return float(sum(team[c] for team in teams for c in columns))


def draw_bonus(
    probabilities: Mapping[str, Floats],
    sources: Mapping[str, RpSource],
    rng: np.random.Generator,
    *,
    latent: Floats | None = None,
    correlations: Mapping[str, float] | None = None,
) -> dict[str, npt.NDArray[np.bool_]]:
    """Whether each alliance earns each bonus RP, keyed by rule-pack component.

    Each RP is earned when ``correlation * latent + sqrt(1 - correlation**2) * noise`` clears its probability, the noise
    shared by RPs on one source and ``latent`` the alliance's score draw on the normal scale.
    """
    noise: dict[RpSource, Floats] = {}
    earned: dict[str, npt.NDArray[np.bool_]] = {}
    for component, probability in probabilities.items():
        p = np.asarray(probability, dtype=np.float64)
        source = sources[component]
        if source not in noise:
            noise[source] = rng.standard_normal(p.shape)
        rho = 0.0 if latent is None or correlations is None else correlations.get(component, 0.0)
        x = noise[source] if rho == 0.0 else rho * np.asarray(latent) + np.sqrt(1.0 - rho**2) * noise[source]
        earned[component] = ndtr(-x) < p
    return earned


def fit_correlation(probabilities: Sequence[Floats], earned: Sequence[npt.NDArray[np.bool_]], latent: Floats) -> float:
    """The one correlation with ``latent`` that maximizes the likelihood of RPs drawn on one source."""
    from scipy.optimize import minimize_scalar

    z = np.asarray(latent, dtype=np.float64)
    pairs = [(ndtri(np.clip(p, CLIP, 1.0 - CLIP)), e) for p, e in zip(probabilities, earned, strict=True)]

    def loss(rho: float) -> float:
        total = 0.0
        for a, e in pairs:
            q = np.clip(ndtr((a + rho * z) / np.sqrt(1.0 - rho**2)), CLIP, 1.0 - CLIP)
            total -= float(np.sum(np.where(e, np.log(q), np.log(1.0 - q))))
        return total

    return float(minimize_scalar(loss, bounds=(-CORRELATION_BOUND, CORRELATION_BOUND), method="bounded").x)


def fit_recovery(source: RpSource, observed_points: Floats, observed_quantity: Floats) -> Recovery:
    """The source's recovery refit on observed rows, or as declared when there are none."""
    if len(observed_points) == 0:
        return source.recovery
    return source.recovery.fit(observed_points, observed_quantity)


def log_loss(predicted: Floats, earned: npt.NDArray[np.bool_]) -> float:
    p = np.clip(predicted, CLIP, 1.0 - CLIP)
    return float(-np.mean(np.where(earned, np.log(p), np.log(1.0 - p))))


def top_bucket_cap(predicted: Floats, earned: npt.NDArray[np.bool_], *, min_rows: int) -> float:
    """The earned rate in the highest probability bucket with at least ``min_rows`` rows, or 1 when none has."""
    bucket = np.minimum((predicted * BUCKETS).astype(np.int64), BUCKETS - 1)
    for b in range(BUCKETS - 1, LOWEST_CAP_BUCKET_INDEX - 1, -1):
        rows = bucket == b
        if rows.sum() >= min_rows:
            return float(earned[rows].mean())
    return 1.0


def fit_curve(
    points: Floats,
    thresholds: Floats,
    earned: npt.NDArray[np.bool_],
    *,
    source: RpSource,
    observed_points: Floats,
    observed_quantity: Floats,
    min_bucket_rows: int,
    start: RpCurve | None = None,
) -> RpCurve:
    """The source's recovery fitted on the observed rows, then the curve that minimizes log loss and its cap.

    ``points`` are predicted, ``thresholds`` are the RP's own, and the observed points and quantity are the RP's
    ``points_from`` sum and ``sum_of`` sum from each row's breakdown.
    """
    from scipy.optimize import minimize

    if len(points) == 0:
        raise ValueError("no rows to fit an RP curve on")
    recovery = fit_recovery(source, observed_points, observed_quantity)
    first = start or RpCurve(shift=0.0, log_sd=float(np.log(np.std(points) + 1.0)), log_sd_slope=0.0)
    t = RpCurve(0.0, 0.0, 0.0, recovery=recovery).points_threshold(thresholds)

    def loss(w: Floats) -> float:
        return log_loss(RpCurve(w[0], w[1], w[2]).uncapped_at(points, t), earned)

    w = minimize(
        loss, np.array([first.shift, first.log_sd, first.log_sd_slope]), method="Nelder-Mead", options={"maxiter": 4000}
    ).x
    curve = RpCurve(float(w[0]), float(w[1]), float(w[2]), rows=len(points), recovery=recovery)
    return replace(curve, cap=top_bucket_cap(curve.uncapped_at(points, t), earned, min_rows=min_bucket_rows))


def provisional_curve(previous: Sequence[RpCurve], recovery: Recovery) -> RpCurve:
    """The stand-in until a season has enough rows: earlier seasons' curves averaged, on this season's recovery."""
    if not previous:
        raise ValueError("no earlier season's RP curve to stand in")
    return RpCurve(
        shift=fmean(c.shift for c in previous),
        log_sd=fmean(c.log_sd for c in previous),
        log_sd_slope=fmean(c.log_sd_slope for c in previous),
        cap=fmean(c.cap for c in previous),
        provisional=True,
        recovery=recovery,
        correlation=fmean(c.correlation for c in previous),
    )


def resolve_curve(
    points: Floats,
    thresholds: Floats,
    earned: npt.NDArray[np.bool_],
    *,
    source: RpSource,
    observed_points: Floats,
    observed_quantity: Floats,
    min_rows: int,
    min_bucket_rows: int,
    previous: Sequence[RpCurve],
) -> RpCurve:
    """The season's curve from its rows so far, or the earlier seasons' until it has ``min_rows``."""
    if len(points) < min_rows:
        return provisional_curve(previous, fit_recovery(source, observed_points, observed_quantity))
    return fit_curve(
        points,
        thresholds,
        earned,
        source=source,
        observed_points=observed_points,
        observed_quantity=observed_quantity,
        min_bucket_rows=min_bucket_rows,
    )
