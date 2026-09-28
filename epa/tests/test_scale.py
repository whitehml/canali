"""Season units, the layoff boost, carryover and rookie seeding."""

from __future__ import annotations

import pytest

from epa.scale import Carryover, LayoffBoost, SeasonScale, carry_forward, compute_scale, provisional_scale

SCALE = SeasonScale(season=2025, mu=80.0, sigma=50.0, rows=1000)

# ------------------------------------------------------------------------------------------------------ the scale


def test_a_scale_with_no_spread_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot normalize"):
        SeasonScale(season=2025, mu=80.0, sigma=0.0, rows=10)


def test_the_window_is_a_prefix_of_the_stream() -> None:
    early = [10.0, 20.0] * 50
    late = [1000.0, 2000.0] * 50
    windowed = compute_scale(early + late, 2025, window=100)
    assert (windowed.mu, windowed.rows) == (15.0, 100)
    assert compute_scale(early + late, 2025).mu == 757.5


def test_a_borrowed_scale_averages_previous_seasons_and_says_so() -> None:
    borrowed = provisional_scale(2026, [SCALE, SeasonScale(season=2024, mu=90.0, sigma=70.0, rows=10)])
    assert borrowed.provisional
    assert (borrowed.mu, borrowed.sigma) == (85.0, 60.0)
    assert not compute_scale([1.0, 2.0, 3.0], 2025).provisional


def test_a_first_season_has_no_scale_to_borrow() -> None:
    with pytest.raises(ValueError, match="no previous season"):
        provisional_scale(2022, [])


# --------------------------------------------------------------------------------------------------------- layoff


def test_a_team_that_has_not_competed_earns_no_boost() -> None:
    boost = LayoffBoost(sigma_per_30d=0.1)
    assert boost.points(None, SCALE) == boost.points(0.0, SCALE) == 0.0


def test_the_boost_grows_with_the_layoff_until_the_cap() -> None:
    boost = LayoffBoost(sigma_per_30d=0.1, cap_days=90.0)
    assert boost.points(30.0, SCALE) < boost.points(60.0, SCALE) < boost.points(90.0, SCALE)
    assert boost.points(90.0, SCALE) == boost.points(365.0, SCALE)


# ------------------------------------------------------------------------------------------------------ carryover

CARRY = Carryover(year_one_weight=0.7, mean_reversion=0.4, init_penalty=0.2)


def test_a_returning_team_is_pulled_part_way_toward_the_rookie_start() -> None:
    start = carry_forward(previous_norm=2000.0, second_norm=2000.0, carryover=CARRY, scale=SCALE)
    assert CARRY.rookie_norm < start.norm < 2000.0
    assert not start.is_rookie
    assert not start.returning_from_gap


def test_a_rookie_starts_at_the_rookie_norm() -> None:
    start = carry_forward(previous_norm=None, second_norm=None, carryover=CARRY, scale=SCALE)
    assert start.norm == pytest.approx(CARRY.rookie_norm)
    assert start.is_rookie


def test_a_gap_year_team_keeps_part_of_its_older_season() -> None:
    start = carry_forward(previous_norm=None, second_norm=2000.0, carryover=CARRY, scale=SCALE)
    assert CARRY.rookie_norm < start.norm < 2000.0
    assert start.returning_from_gap
    assert not start.is_rookie


def test_a_start_is_never_negative() -> None:
    start = carry_forward(previous_norm=0.0, second_norm=0.0, carryover=CARRY, scale=SCALE)
    assert start.scaled == pytest.approx(0.0)
