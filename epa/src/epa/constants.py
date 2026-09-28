"""Versioned model constants, fitted by ``epa fit-constants`` and derived in ``docs/methodology.md``.

``MODEL_VERSION`` names a set of ratings that may be compared with one another. Bump it whenever a rating computed now
would not be comparable to one already stored under that name.
"""

from __future__ import annotations

from dataclasses import dataclass

from epa.model import Schedule
from epa.scale import Carryover, LayoffBoost, Scale

MODEL_VERSION = "epa-0.9.0"

# How many alliance-rows in event-sequential order define the season-start field.
INIT_WINDOW = 4000

# Whole-season alliance no-foul score distributions, measured under `score - opponent_committed`.
MEASURED_SEASON_SCALES: dict[int, Scale] = {
    2022: Scale(season=2022, mu=75.4, sigma=49.8, rows=43736),
    2023: Scale(season=2023, mu=68.7, sigma=53.7, rows=50384),
    2024: Scale(season=2024, mu=88.0, sigma=68.5, rows=56864),
    2025: Scale(season=2025, mu=71.9, sigma=47.1, rows=60548),
}

# The spread of the field on the day a team starts, which is a different estimand from MEASURED_SEASON_SCALES and the
# only scale `carry_forward` may use.
MEASURED_INIT_SCALES: dict[int, Scale] = {
    2022: Scale(season=2022, mu=50.3, sigma=34.9, rows=4000),
    2023: Scale(season=2023, mu=40.7, sigma=30.2, rows=4000),
    2024: Scale(season=2024, mu=46.9, sigma=35.0, rows=4000),
    2025: Scale(season=2025, mu=47.7, sigma=29.0, rows=4000),
}

FITTED_CARRYOVER = Carryover(year_one_weight=0.7777, mean_reversion=0.4325)

# The learning rate, ramped on qualification matches played, and the margin weight, which is off.
FITTED_K = Schedule(breakpoints=(4, 8), values=(0.7, 0.5, 0.35))
FITTED_M = Schedule.constant(0.0)

# What a team is assumed to have gained over a break, in season sigma per 30 days.
FITTED_LAYOFF = LayoffBoost(sigma_per_30d=0.15, cap_days=60.0)

# What an elimination match is worth, as a fraction of a qualification match.
ELIM_WEIGHT = 1.0 / 3.0


@dataclass(frozen=True, slots=True)
class SeasonConstants:
    """One season's parameters. The rule pack declares the partition, so it is not among them."""

    season: int
    k: Schedule
    m: Schedule
    init_window: int = INIT_WINDOW
    carryover: Carryover | None = None
    layoff_boost: LayoffBoost | None = None
    elim_weight: float = 0.0
    """How much of a delta an elimination match applies, zero by default so a season built by hand rates
    qualification matches only."""


def for_season(season: int) -> SeasonConstants:
    """Constants for a season. No value here varies by season, so an unrun season is not a special case."""
    return SeasonConstants(
        season=season,
        k=FITTED_K,
        m=FITTED_M,
        init_window=INIT_WINDOW,
        carryover=FITTED_CARRYOVER,
        layoff_boost=FITTED_LAYOFF,
        elim_weight=ELIM_WEIGHT,
    )
