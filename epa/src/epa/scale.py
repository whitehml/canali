"""Season units, carryover, and rookie initialization.

The season scale is the whole season's distribution and waits for the season to close, sigma growing two to three
times over within a season. The start scale is a prefix of N alliance-rows in event-sequential order, and is what a
carried rating is converted through. Until the prefix fills the season borrows a scale from previous seasons and every
row it produces is provisional.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from epa.model import ALLIANCE_SIZE
from epa.norm import INIT_PENALTY, init_norm, norm_to_z


@dataclass(frozen=True, slots=True)
class SeasonScale:
    """The season's alliance no-foul score distribution."""

    season: int
    mu: float
    sigma: float
    rows: int
    provisional: bool = False

    def __post_init__(self) -> None:
        if self.sigma <= 0:
            raise ValueError(
                f"season {self.season} scale has sigma={self.sigma}; a non-positive spread cannot normalize anything"
            )

    @property
    def team_mean(self) -> float:
        """A team's share of a typical alliance."""
        return self.mu / ALLIANCE_SIZE


def compute_scale(alliance_scores: Iterable[float], season: int, *, window: int | None = None) -> SeasonScale:
    """The real scale, from the first ``window`` alliance-rows in event-sequential order, or all of them when None."""
    values = list(alliance_scores)
    if window is not None:
        values = values[:window]
    if len(values) < 2:
        raise ValueError(f"season {season} scale needs at least 2 alliance-rows, got {len(values)}")
    return SeasonScale(
        season=season,
        mu=statistics.fmean(values),
        sigma=statistics.stdev(values),
        rows=len(values),
        provisional=False,
    )


def provisional_scale(season: int, previous: Sequence[SeasonScale]) -> SeasonScale:
    """The stand-in used until the window fills: the average of previous seasons' figures."""
    if not previous:
        raise ValueError(
            f"season {season} has no previous season to borrow a scale from; "
            f"a first season cannot be rated before its window fills"
        )
    return SeasonScale(
        season=season,
        mu=statistics.fmean([s.mu for s in previous]),
        sigma=statistics.fmean([s.sigma for s in previous]),
        rows=0,
        provisional=True,
    )


def to_scaled(z: float, scale: SeasonScale) -> float:
    """A z-score into this season's point units."""
    return z * scale.sigma + scale.team_mean


@dataclass(frozen=True, slots=True)
class LayoffBoost:
    """How much a team is assumed to have improved while it was not competing.

    The clock starts when a robot first takes a field, so a team that has not competed yet earns nothing.
    """

    sigma_per_30d: float
    """Improvement per 30 days of layoff, as a fraction of season sigma."""

    cap_days: float = 90.0
    """Layoff beyond this earns no further boost."""

    def points(self, layoff_days: float | None, scale: SeasonScale) -> float:
        if layoff_days is None or layoff_days <= 0.0:
            return 0.0
        capped = min(float(layoff_days), self.cap_days)
        return self.sigma_per_30d * scale.sigma * (capped / 30.0)


@dataclass(frozen=True, slots=True)
class Carryover:
    """How a team's rating enters the next season::

    prev  = w * norm[y - 1] + (1 - w) * norm[y - 2]
    start = (1 - reversion) * prev + reversion * init_norm(penalty)
    """

    year_one_weight: float
    mean_reversion: float
    init_penalty: float = INIT_PENALTY

    def __post_init__(self) -> None:
        if not 0.0 <= self.year_one_weight <= 1.0:
            raise ValueError(
                f"year_one_weight={self.year_one_weight} is not a weight; "
                f"it splits a team's history between the last two seasons"
            )
        if not 0.0 <= self.mean_reversion <= 1.0:
            raise ValueError(
                f"mean_reversion={self.mean_reversion} is not a fraction; "
                f"it blends a carried rating toward the no-history start"
            )

    @property
    def rookie_norm(self) -> float:
        return init_norm(self.init_penalty)

    def for_returning(self, previous_norm: float | None, second_norm: float | None) -> float:
        """The carried norm rating, a missing season contributing the rookie value rather than dropping out."""
        rookie = self.rookie_norm
        blended = self.year_one_weight * (previous_norm if previous_norm is not None else rookie) + (
            1.0 - self.year_one_weight
        ) * (second_norm if second_norm is not None else rookie)
        return (1.0 - self.mean_reversion) * blended + self.mean_reversion * rookie


@dataclass(frozen=True, slots=True)
class Initialization:
    """A team's starting rating, with the reason attached."""

    norm: float
    scaled: float
    is_rookie: bool
    returning_from_gap: bool


def carry_forward(
    *,
    previous_norm: float | None,
    second_norm: float | None,
    carryover: Carryover,
    scale: SeasonScale,
) -> Initialization:
    """Where a team starts the season, in points, through the start scale.

    Floored at zero.
    """
    is_rookie = previous_norm is None and second_norm is None
    gap = previous_norm is None and second_norm is not None
    norm = carryover.for_returning(previous_norm, second_norm)
    z = max(-scale.team_mean / scale.sigma, norm_to_z(norm))
    return Initialization(
        norm=norm,
        scaled=to_scaled(z, scale),
        is_rookie=is_rookie,
        returning_from_gap=gap,
    )
