import sys
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[1]))
from auto_leave import LeaveComparison, advance_leave, base_points, qualification_pair
from snapshot import EventSnapshot

from sim.standings import RankingRules, standings


def source():
    path = Path(__file__).parents[2] / "worker/fixtures/forecast.json"
    snapshot = EventSnapshot.model_validate_json(path.read_text())
    return snapshot.model_copy(update={"min_iterations": 64, "max_iterations": 64})


@pytest.mark.parametrize(
    "base_rating, earns_before, earns_after", [(0, False, False), (7.5, False, True), (15, True, True)]
)
def test_leave_changes_exactly_three_points_and_uses_movement_threshold(base_rating, earns_before, earns_after):
    snapshot = source().model_copy(deep=True)
    for values in snapshot.field.predicted.values():
        values["auto_leave_points"] = 0
        values["teleop_base_points"] = base_rating
    request = LeaveComparison(team=1, current=0, improved=1)
    before = snapshot.model_dump_json()
    schedule, (current, improved) = qualification_pair(snapshot, request, np.random.default_rng(5))
    affected = (schedule.stations == 1).any(axis=2)
    played = np.array(snapshot.progress.played)[:, None] if snapshot.progress else np.zeros((len(affected), 1), bool)
    changed = affected & ~played
    assert changed.any()
    np.testing.assert_array_equal(improved.score - current.score, changed * 3)
    np.testing.assert_array_equal(improved.components["autoPoints"] - current.components["autoPoints"], changed * 3)
    np.testing.assert_array_equal(improved.components["teleopBasePoints"], current.components["teleopBasePoints"])
    assert (current.bonus["movementRP"][changed] == earns_before).all()
    assert (improved.bonus["movementRP"][changed] == earns_after).all()
    for name in ("goalRP", "patternRP"):
        np.testing.assert_array_equal(current.bonus[name], improved.bonus[name])
    if snapshot.progress:
        np.testing.assert_array_equal(
            current.score[np.array(snapshot.progress.played)],
            snapshot.progress.build().results.score[np.array(snapshot.progress.played)],
        )
    assert snapshot.model_dump_json() == before


def test_equal_rates_are_identical_and_checkpoint_boundaries_preserve_results():
    snapshot = source()
    request = LeaveComparison(team=1, current=0.4, improved=0.4)
    before = snapshot.model_dump_json()
    checkpoint = None
    while True:
        checkpoint, report = advance_leave(snapshot, request, checkpoint)
        if report["status"] == "complete":
            break
    result = report["result"]
    assert result["iterations"] == 64
    assert result["mean_rank"][0] == result["mean_rank"][1]
    assert result["rank_delta_se"] == 0
    for row in result["matches"]:
        assert row["current"] == row["improved"]
    # A checkpoint resumed by a fresh caller produces the same final counters.
    first, _ = advance_leave(snapshot.model_copy(update={"max_iterations": 16}), request)
    while True:
        first, resumed = advance_leave(snapshot, request, first)
        if resumed["status"] == "complete":
            break
    assert resumed == report
    assert snapshot.model_dump_json() == before


def test_base_totals_and_request_guards():
    expected = np.array([-1, 0, 5, 10, 15, 30, 40])
    np.testing.assert_array_equal(base_points(expected, np.full(7, 0.5)), [0, 0, 5, 10, 15, 30, 30])
    uniform = (np.arange(10000) + 0.5) / 10000
    for mean in (2.5, 7, 12, 21):
        assert base_points(np.full(10000, mean), uniform).mean() == pytest.approx(mean)
    with pytest.raises(ValidationError):
        LeaveComparison(team=1, current=float("nan"), improved=1)
    snapshot = source()
    for request in (
        LeaveComparison(team=999999, current=0, improved=1),
        LeaveComparison(team=1, current=1, improved=0),
    ):
        with pytest.raises(ValueError):
            advance_leave(snapshot, request)
    with pytest.raises(ValueError, match="schedule"):
        advance_leave(snapshot.model_copy(update={"schedule": None}), LeaveComparison(team=1, current=0, improved=1))
    finished = snapshot.model_copy(deep=True)
    finished.progress.played = [True] * len(finished.progress.played)
    with pytest.raises(ValueError, match="remaining"):
        advance_leave(finished, LeaveComparison(team=1, current=0, improved=1))


def test_surrogate_is_reported_and_does_not_enter_ranking_score():
    snapshot = source().model_copy(deep=True)
    schedule = snapshot.schedule.build()
    match, alliance, station = next(zip(*np.nonzero(schedule.stations == 1), strict=True))
    snapshot.schedule.surrogate = np.zeros(schedule.stations.shape, dtype=bool).tolist()
    snapshot.schedule.surrogate[match][alliance][station] = True
    request = LeaveComparison(team=1, current=0, improved=1)
    schedule, (_, results) = qualification_pair(snapshot, request, np.random.default_rng(3))
    rules = RankingRules.from_pack(snapshot.rule_pack)
    ranked = standings(schedule, results, rules, np.random.default_rng(4))
    counted = (schedule.stations == 1) & ~schedule.surrogate
    rp = np.broadcast_to(rules.alliance_rp(results)[..., None], schedule.stations.shape)
    index = np.flatnonzero(ranked.teams == 1)[0]
    assert ranked.sort_orders[index, 0] == pytest.approx(rp[counted].mean())
    # Mark all appearances unplayed to include the surrogate in the remaining-match report.
    snapshot.progress.played = [False] * len(snapshot.progress.played)
    _, report = advance_leave(snapshot.model_copy(update={"max_iterations": 1}), request)
    assert next(row for row in report["result"]["matches"] if row["match"] == match + 1)["surrogate"]
