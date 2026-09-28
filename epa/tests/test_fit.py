"""Leave-one-season-out selection and the carryover regression."""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from epa.fit import fit_carryover, leave_one_season_out
from epa.norm import init_norm


@dataclass(frozen=True)
class _Scored:
    label: str
    mse_by_season: dict[int, float]


STEADY = _Scored("steady", {2023: 10.0, 2024: 10.0, 2025: 10.0})
LUCKY = _Scored("lucky", {2023: 12.0, 2024: 12.0, 2025: 1.0})


def test_a_held_out_season_does_not_vote_for_its_own_candidate() -> None:
    held_2025 = {r.held_out: r for r in leave_one_season_out([STEADY, LUCKY], [2023, 2024, 2025])}[2025]
    assert (held_2025.chosen, held_2025.held_out_mse) == ("steady", 10.0)


ROOKIE = init_norm()


def _history(teams: int, rng: random.Random) -> tuple[dict[int, float], dict[int, float]]:
    """Two correlated but not collinear seasons of final norms."""
    older = {team: rng.gauss(1500.0, 150.0) for team in range(teams)}
    last = {team: 0.6 * older[team] + 0.4 * rng.gauss(1500.0, 150.0) for team in range(teams)}
    return last, older


def _starts(
    last: dict[int, float], older: dict[int, float], *, weight: float, reversion: float, rng: random.Random
) -> dict[int, float]:
    return {
        team: (1 - reversion) * (weight * last[team] + (1 - weight) * older[team])
        + reversion * ROOKIE
        + rng.gauss(0.0, 20.0)
        for team in last
    }


def test_the_carryover_regression_recovers_a_known_weight_and_reversion() -> None:
    rng = random.Random(4)
    last, older = _history(3000, rng)
    fit = fit_carryover({2024: last, 2023: older}, {2025: _starts(last, older, weight=0.75, reversion=0.4, rng=rng)})
    (transition,) = fit.transitions
    assert transition.year_one_weight == pytest.approx(0.75, abs=0.03)
    assert transition.mean_reversion == pytest.approx(0.4, abs=0.03)


def test_disagreeing_transitions_propose_the_weight_least_willing_to_credit_an_absent_season() -> None:
    rng = random.Random(5)
    last, older = _history(3000, rng)
    newest = {team: 0.6 * last[team] + 0.4 * rng.gauss(1500.0, 150.0) for team in last}
    fit = fit_carryover(
        {2022: older, 2023: last, 2024: newest},
        {
            2024: _starts(last, older, weight=0.9, reversion=0.4, rng=rng),
            2025: _starts(newest, last, weight=0.5, reversion=0.4, rng=rng),
        },
    )
    assert not fit.agree
    assert fit.proposal().year_one_weight == max(t.year_one_weight for t in fit.transitions)


def test_a_transition_without_two_distinguishable_prior_seasons_is_not_fitted() -> None:
    rng = random.Random(6)
    last, older = _history(500, rng)
    starts = {2025: _starts(last, older, weight=0.75, reversion=0.4, rng=rng)}
    assert fit_carryover({2024: last}, starts).transitions == ()
    assert fit_carryover({2024: last, 2023: dict(last)}, starts).transitions == ()
