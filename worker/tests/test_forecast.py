"""Checkpointed event runs must preserve the native simulator's results."""

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
from forecast import advance
from snapshot import EventSnapshot

from sim.runner import run_event


@pytest.fixture
def document():
    value = json.loads((Path(__file__).parents[1] / "fixtures" / "forecast.json").read_text())
    value.update(min_iterations=80, max_iterations=200, tolerance=0.08)
    return value


def finish(snapshot, batch_size=32):
    state = None
    for _ in range(200):
        state, report = advance(snapshot, state, batch_size=batch_size)
        state = json.loads(json.dumps(state))
        if report["status"] != "running":
            return state, report
    pytest.fail("event did not finish in its iteration budget")


def test_json_checkpoints_match_native_event_simulation(document):
    snapshot = EventSnapshot.model_validate(document)
    native = run_event(
        snapshot.draw(),
        snapshot.rule_pack,
        snapshot.models.build(),
        np.random.default_rng(snapshot.seed),
        slots=snapshot.slots,
        progress=snapshot.progress.build(),
        tolerance=snapshot.tolerance,
        min_iterations=snapshot.min_iterations,
    )
    expected = json.loads(json.dumps(asdict(native)))
    state, report = finish(snapshot)
    assert report["status"] == "complete"
    assert json.loads(json.dumps(report["result"])) == expected
    assert finish(snapshot, batch_size=7)[0] == state
    repeated, repeated_report = advance(snapshot, state)
    assert repeated == state
    assert repeated_report == report
    assert not any(name in state for name in ("outcomes", "draws"))
    teams = report["result"]["teams"].values()
    assert sum(team["p_captain"] for team in teams) == pytest.approx(4)
    assert sum(team["p_selected"] for team in teams) == pytest.approx(4)
    assert sum(team["p_win"] for team in teams) == pytest.approx(2)
    assert sum(team["p_advance"] for team in teams) == pytest.approx(3)
    assert report["result"]["teams"][12]["p_advance"] == 0
    assert all(sum(team["rank_probabilities"]) == pytest.approx(1) for team in teams)


@pytest.mark.parametrize("rating", [0.0, -1.0])
def test_nonpositive_team_epa_matches_native_simulator(document, rating):
    document["field"]["predicted"]["1"] = dict.fromkeys(document["field"]["predicted"]["1"], rating)
    document.update(min_iterations=20, tolerance=0.3)
    snapshot = EventSnapshot.model_validate(document)
    native = run_event(
        snapshot.draw(),
        snapshot.rule_pack,
        snapshot.models.build(),
        np.random.default_rng(snapshot.seed),
        slots=snapshot.slots,
        progress=snapshot.progress.build(),
        tolerance=snapshot.tolerance,
        min_iterations=snapshot.min_iterations,
    )
    _, report = finish(snapshot)
    assert json.loads(json.dumps(report["result"])) == json.loads(json.dumps(asdict(native)))


def test_generated_schedule_matches_native_simulation(document):
    document.update(schedule=None, progress=None, min_iterations=12, tolerance=0.3)
    snapshot = EventSnapshot.model_validate(document)
    native = run_event(
        snapshot.draw(),
        snapshot.rule_pack,
        snapshot.models.build(),
        np.random.default_rng(snapshot.seed),
        slots=snapshot.slots,
        tolerance=snapshot.tolerance,
        min_iterations=snapshot.min_iterations,
    )
    _, report = finish(snapshot, batch_size=5)
    assert json.loads(json.dumps(report["result"])) == json.loads(json.dumps(asdict(native)))


def test_iteration_budget_does_not_publish_an_unconverged_forecast(document):
    document.update(min_iterations=10, max_iterations=10, tolerance=1e-8)
    _, report = finish(EventSnapshot.model_validate(document))
    assert report["status"] == "exhausted"
    assert report["iterations"] == 10
    assert report["result"] is None


def test_elapsed_budget_checkpoints_after_a_whole_iteration(document, monkeypatch):
    ticks = iter((0, 3))
    monkeypatch.setattr("forecast.monotonic", lambda: next(ticks))
    state, report = advance(EventSnapshot.model_validate(document), None)
    assert report["status"] == "running"
    assert report["iterations"] == state["accumulator"]["iterations"] == 1


@pytest.mark.parametrize("championship", [False, True])
def test_events_without_slots_and_championship_slots(document, championship):
    document.update(min_iterations=12, tolerance=0.3)
    if championship:
        document["field"]["event_type"] = "Championship"
        document["slots"]["first_championship"] = 1
    else:
        document["slots"] = None
    _, report = finish(EventSnapshot.model_validate(document))
    teams = report["result"]["teams"].values()
    if championship:
        assert sum(team["p_advance_first_championship"] for team in teams) == pytest.approx(1)
    else:
        assert all(team["p_advance"] is None for team in teams)


def test_published_alliances_are_kept_across_batches(document):
    document["progress"]["played"] = [True] * 6
    document["progress"]["alliances"] = [[1, 2], [3, 4], [5, 6], [7, 8]]
    document["progress"]["decided"] = [{"series": 1, "game": 1, "winner": 1}]
    _, report = finish(EventSnapshot.model_validate(document))
    assert report["result"]["teams"][1]["p_captain"] == 1
    assert report["result"]["teams"][2]["p_selected"] == 1
    assert report["result"]["teams"][9]["p_selected"] == 0


@pytest.mark.parametrize("change", ["schedule", "progress", "curves", "awards", "nan", "slots", "limits"])
def test_invalid_prepared_inputs_are_rejected(document, change):
    if change == "schedule":
        document["schedule"]["stations"][0][0] = [1, 1]
    elif change == "progress":
        document["progress"]["score"] = [[1, 2]]
    elif change == "curves":
        document["models"]["rp_curves"] = {}
    elif change == "awards":
        document["models"]["awards"] = []
    elif change == "nan":
        document["field"]["predicted"]["1"]["auto_artifact_points"] = float("nan")
    elif change == "slots":
        document["slots"]["count"] = -1
    else:
        document["min_iterations"] = document["max_iterations"] + 1
    with pytest.raises((ValueError, KeyError)):
        EventSnapshot.model_validate(document)
