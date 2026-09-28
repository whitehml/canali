"""Test the update rule."""

from __future__ import annotations

import pytest

from epa.model import Schedule, residual, update_for_match

# ------------------------------------------------------------------------------------------------------ schedules


def test_a_schedule_refuses_a_value_count_that_does_not_match_its_breakpoints() -> None:
    with pytest.raises(ValueError, match="one longer"):
        Schedule(breakpoints=(4, 8), values=(0.7, 0.5))


def test_a_schedule_refuses_breakpoints_that_do_not_ascend() -> None:
    with pytest.raises(ValueError, match="ascending"):
        Schedule(breakpoints=(8, 4), values=(0.7, 0.5, 0.35))


def test_a_schedule_steps_down_on_reaching_each_breakpoint() -> None:
    # A breakpoint belongs to the bucket above it.
    schedule = Schedule(breakpoints=(4, 8), values=(0.7, 0.5, 0.35))
    assert [schedule.at(played) for played in (0, 3, 4, 7, 8, 20)] == [0.7, 0.7, 0.5, 0.5, 0.35, 0.35]


def test_a_constant_schedule_is_flat_at_every_match_count() -> None:
    flat = Schedule.constant(0.25)
    assert {flat.at(played) for played in (0, 4, 8, 100)} == {0.25}


# ------------------------------------------------------------------------------------------------------- residual


def test_at_zero_margin_weight_the_opponent_score_does_not_enter_the_residual() -> None:
    def against(opponent_score: float) -> float:
        return residual(
            own_score=120.0, own_predicted=100.0, opponent_score=opponent_score, opponent_predicted=100.0, m=0.0
        )

    assert against(40.0) == against(100.0) == against(200.0) == 20.0


def test_a_positive_margin_weight_discounts_an_opponent_who_also_beat_prediction() -> None:
    alone = residual(own_score=120.0, own_predicted=100.0, opponent_score=100.0, opponent_predicted=100.0, m=0.25)
    shared = residual(own_score=120.0, own_predicted=100.0, opponent_score=120.0, opponent_predicted=100.0, m=0.25)
    assert 0.0 < shared < alone


# --------------------------------------------------------------------------------------------------------- update


def test_a_match_moves_only_the_alliance_it_is_applied_for() -> None:
    update = update_for_match(
        {1: 50.0, 2: 50.0, 3: 50.0, 4: 50.0},
        own_teams=(1, 2),
        opponent_teams=(3, 4),
        own_score=80.0,
        opponent_score=140.0,
        k=0.7,
        m=0.0,
    )
    assert set(update.deltas) == {1, 2}
    assert update.deltas[1] < 0.0
    assert update.predicted == {"own": 100.0, "opponent": 100.0}
