"""Standings calculations."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from sim.schedule import Schedule
from sim.standings import MatchResults, RankingRules, Standings, Tiebreaker, standings

RULES = RankingRules(
    rp_win=3, rp_tie=1, rp_loss=0, bonus_rp={}, tiebreakers=(Tiebreaker("avg", "x"), Tiebreaker("random"))
)


def _rank(
    stations: list[list[list[int]]],
    score: list[list[float]],
    x: list[list[float]],
    surrogate: npt.NDArray[np.bool_] | None = None,
    dq: npt.NDArray[np.bool_] | None = None,
) -> Standings:
    teams = np.array(stations)
    schedule = Schedule(teams, np.zeros(teams.shape, bool) if surrogate is None else surrogate)
    results = MatchResults(score=np.array(score, float), bonus={}, components={"x": np.array(x, float)}, dq=dq)
    return standings(schedule, results, RULES, np.random.default_rng(0))


def _by_team(table: Standings) -> dict[int, tuple[float, float, int]]:
    return {
        int(t): (float(s[0]), float(s[1]), int(p))
        for t, s, p in zip(table.teams, table.sort_orders, table.matches_played, strict=True)
    }


def test_a_disqualified_appearance_counts_as_played_and_scores_zero() -> None:
    dq = np.zeros((1, 2, 2), bool)
    dq[0, 0, 0] = True
    table = _rank([[[1, 2], [3, 4]]], [[10, 5]], [[7, 4]], dq=dq)

    assert _by_team(table) == {1: (0, 0, 1), 2: (3, 7, 1), 3: (0, 4, 1), 4: (0, 4, 1)}
    assert table.teams[0] == 2
    assert table.teams[-1] == 1


def test_a_surrogate_appearance_scores_for_the_alliance_but_not_the_team() -> None:
    surrogate = np.zeros((2, 2, 2), bool)
    surrogate[1, 0, 1] = True
    table = _rank([[[1, 2], [3, 4]], [[5, 1], [2, 3]]], [[5, 10], [10, 5]], [[2, 8], [6, 1]], surrogate=surrogate)

    assert _by_team(table)[1] == (0, 2, 1)
    assert _by_team(table)[5] == (3, 6, 1)
