"""The cross-season rating: a season percentile mapped through a right-skewed distribution."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

from scipy.stats import expon, exponnorm

NORM_MEAN = 1500.0
NORM_SD = 250.0

# How far below average a team with no history starts, in sigma.
INIT_PENALTY = 0.2

# Right-skewed because team strength is: the gap between the best team and the median is far larger than between the
# median and the worst.
_TARGET = exponnorm(1.6, -0.3, 0.2)

# Above this quantile the empirical CDF has too few teams to estimate a tail from, so an exponential is fitted to the
# top decile and blended in.
_TAIL_FRACTION = 0.1

# The map is evaluated per team per row, precomputed on a percentile grid and interpolated between knots.
_KNOTS = 101


def init_norm(penalty: float = INIT_PENALTY) -> float:
    """Where a team with no usable history starts."""
    return NORM_MEAN - penalty * NORM_SD


def norm_to_z(norm: float) -> float:
    return (norm - NORM_MEAN) / NORM_SD


@dataclass(frozen=True, slots=True)
class NormMap:
    """One season's EPA to norm-rating map, built from that season's whole population."""

    quantiles: tuple[float, ...]
    norms: tuple[float, ...]
    teams: int

    def __call__(self, epa: float) -> float:
        """Interpolate, flat outside the fitted range."""
        i = bisect_left(self.quantiles, epa)
        if i == 0:
            return self.norms[0]
        if i >= len(self.quantiles):
            return self.norms[-1]
        x0, x1 = self.quantiles[i - 1], self.quantiles[i]
        y0, y1 = self.norms[i - 1], self.norms[i]
        if x1 == x0:
            return y1
        return y0 + (y1 - y0) * (epa - x0) / (x1 - x0)


def build_norm_map(season_epas: Sequence[float]) -> NormMap:
    """Fit the map for a season from its final team EPAs."""
    total = len(season_epas)
    if total < _KNOTS:
        raise ValueError(
            f"a norm map needs at least {_KNOTS} teams to fit a percentile against, got {total}; "
            f"a season this small has no population to rank within"
        )

    ascending = sorted(season_epas)
    descending = ascending[::-1]
    cutoff = int(total * _TAIL_FRACTION)

    body = exponnorm(*exponnorm.fit(descending))
    tail = expon(*expon.fit(descending[:cutoff])) if cutoff > 0 else None

    def percentile(epa: float) -> float:
        body_p: float = body.cdf(epa)
        if tail is None:
            return body_p
        rank_from_top = total - bisect_left(ascending, epa)
        if rank_from_top >= cutoff:
            return body_p
        tail_p: float = tail.cdf(epa)
        tail_p = 1 - _TAIL_FRACTION * (1 - tail_p)
        blend = min(1.0, 2 * (cutoff - rank_from_top) / cutoff)
        return blend * tail_p + (1 - blend) * body_p

    knots = [ascending[((total - 1) * i) // (_KNOTS - 1)] for i in range(_KNOTS)]
    norms = [NORM_MEAN + NORM_SD * float(_TARGET.ppf(percentile(epa))) for epa in knots]
    return NormMap(quantiles=tuple(knots), norms=tuple(norms), teams=total)
