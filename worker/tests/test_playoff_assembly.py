"""Full-event assembly preserves official progress and rates playoff scores with native weighting."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import numpy as np
import pytest
from assemble import assemble, handle
from forecast_store import handle as forecast_handle
from forecast_store import save_input
from playoff_input import playoff_inputs
from season import Config, init_scale, prepare, replay
from test_assemble import ROOT, YEAR, options, source
from test_season import Database, Request, call

from sim.runner import run_event
from warehouse.rules.model import RulePack


@pytest.fixture
def fixture():
    return json.loads((ROOT / "fixtures/forecast.json").read_text())


def setting(fixture, games=()):
    rows, scores = [], []
    for n, (red, blue) in enumerate(games, 1):
        row = deepcopy(source()["schedule"]["schedule"][0])
        row.update(
            tournamentLevel="PLAYOFF",
            series=1,
            matchNumber=n,
            scoreRedFinal=red,
            scoreBlueFinal=blue,
            scoreRedFoul=0,
            scoreBlueFoul=0,
        )
        for station, team in zip(row["teams"], (1, 2, 3, 4), strict=True):
            station.update(teamNumber=team, dq=False, surrogate=False)
        rows.append(row)
        score = deepcopy(source()["scores"]["matchScores"][0])
        score.update(matchLevel="PLAYOFF", matchSeries=1, matchNumber=n)
        for side, points in zip(score["alliances"], (red, blue), strict=True):
            side.update(teleopArtifactPoints=points - 40, teleopPoints=points - 30)
        scores.append(score)
    return options(
        fixture,
        through=None,
        alliances={
            "alliances": [
                {"number": 2, "captain": {"teamNumber": 3}, "round1": {"teamNumber": 4}},
                {"number": 1, "captain": {"teamNumber": 1}, "round1": {"teamNumber": 2}},
            ]
        },
        playoff_schedule={"schedule": rows},
        playoff_scores={"matchScores": scores},
    )


def snapshot(fixture, setting):
    raw = source()
    pack = RulePack.model_validate(fixture["rule_pack"])
    return assemble(
        prepare(raw, "A", pack), raw, Config(rule_pack=pack), init_scale(YEAR, []), history={}, options=setting
    )


def test_selection_and_playoffs_update_snapshot_and_ratings(fixture):
    selected = snapshot(fixture, setting(fixture))
    assert selected.progress.alliances == [(1, 2), (3, 4)]
    assert selected.progress.decided == []
    assert selected.last_match.model_dump() == {"level": "QUALIFICATION", "number": 2, "series": None}
    live = snapshot(fixture, setting(fixture, [(120, 80)]))
    assert live.as_of_match == 3
    assert live.last_match.model_dump() == {"level": "PLAYOFF", "number": 1, "series": 1}
    assert live.progress.decided[0].model_dump() == {"series": 1, "game": 1, "winner": 1}
    assert live.field.predicted[1] != selected.field.predicted[1]
    assert live.field.predicted[5] == selected.field.predicted[5]
    raw = source()
    event = prepare(raw, "A", live.rule_pack)
    extras, breakdowns, _ = playoff_inputs(
        event,
        live.rule_pack,
        live.progress.alliances,
        setting(fixture, [(120, 80)]).playoff_schedule,
        setting(fixture, [(120, 80)]).playoff_scores,
    )
    before = replay(event, YEAR, {}, Config(rule_pack=live.rule_pack), init_scale(YEAR, []))
    after = replay(
        event,
        YEAR,
        {},
        Config(rule_pack=live.rule_pack),
        init_scale(YEAR, []),
        playoffs=extras,
        playoff_breakdowns=breakdowns,
    )
    assert {r["team"]: r["played"] for r in before} == {r["team"]: r["played"] for r in after}


def test_ties_and_completed_final_are_fixed_and_match_native_simulation(fixture):
    live = snapshot(fixture, setting(fixture, [(100, 100), (120, 80), (130, 90)]))
    assert live.as_of_match == 5
    assert live.last_match.number == 3
    assert [(d.game, d.winner) for d in live.progress.decided] == [(1, 1), (2, 1)]
    db = Database()
    asyncio.run(save_input(db, "2025-A", live))
    actual, status = asyncio.run(forecast_handle(db, Request("POST", None), "2025-A", True))
    assert status == 200
    native = run_event(
        live.draw(),
        live.rule_pack,
        live.models.build(),
        np.random.default_rng(live.seed),
        slots=live.slots,
        progress=live.progress.build(),
        tolerance=live.tolerance,
        min_iterations=1,
    )
    assert actual["result"] == asdict(native)
    assert actual["result"]["teams"][1]["p_win"] == 1
    assert actual["result"]["teams"][3]["p_win"] == 0


def test_historical_cutoff_masks_selection_and_all_playoff_information(fixture):
    later = setting(fixture, [(120, 80)])
    later.through = 2
    historical = snapshot(fixture, later)
    assert historical.progress.alliances is None
    assert not historical.progress.decided
    assert historical.last_match.level == "QUALIFICATION"
    assert snapshot(fixture, later.model_copy(update={"through": 0})).last_match is None
    assert historical.field.predicted == snapshot(fixture, setting(fixture)).field.predicted


def test_last_match_uses_official_numbers_and_includes_tied_playoffs(fixture):
    raw = source()
    for row, score in zip(raw["schedule"]["schedule"], raw["scores"]["matchScores"], strict=True):
        row["matchNumber"] += 5
        score["matchNumber"] += 5
    pack = RulePack.model_validate(fixture["rule_pack"])
    historical = assemble(
        prepare(raw, "A", pack), raw, Config(rule_pack=pack), init_scale(YEAR, []),
        history={}, options=options(fixture),
    )
    assert historical.as_of_match == 1
    assert historical.last_match.number == 6
    live = snapshot(fixture, setting(fixture, [(120, 80), (100, 100)]))
    assert live.last_match.model_dump() == {"level": "PLAYOFF", "number": 2, "series": 1}
    assert len(live.progress.decided) == 1


def test_playoff_fouls_are_subtracted_and_no_shows_do_not_receive_updates(fixture):
    live = setting(fixture, [(120, 80)])
    row = live.playoff_schedule["schedule"][0]
    row.update(scoreRedFinal=130, scoreBlueFinal=85, scoreRedFoul=5, scoreBlueFoul=10)
    row["teams"][0]["noShow"] = True
    selected = snapshot(fixture, setting(fixture))
    current = snapshot(fixture, live)
    assert current.field.predicted[1] == selected.field.predicted[1]
    assert current.field.predicted[2] != selected.field.predicted[2]
    raw = source()
    prepared = prepare(raw, "A", current.rule_pack)
    matches, _, _ = playoff_inputs(
        prepared, current.rule_pack, current.progress.alliances, live.playoff_schedule, live.playoff_scores
    )
    assert matches[0].red.teams == (2,)
    assert matches[0].red.score_no_foul == 120
    assert matches[0].blue.score_no_foul == 80


def test_d1_full_event_assembly_is_idempotent_and_rejects_invalid_corrections(fixture):
    db = Database()
    call(db, method="PUT", document={"rule_pack": fixture["rule_pack"]})
    call(db, "events", "A", "PUT", source())
    call(db, "step", method="POST")
    live = setting(fixture, [(120, 80)])
    request = Request("POST", live.model_dump(mode="json"))
    saved, status = asyncio.run(handle(db, request, YEAR, "A"))
    assert status == 200 and saved["snapshot"]["as_of_match"] == 3
    again, status = asyncio.run(handle(db, request, YEAR, "A"))
    assert status == 200 and again["revision"] == saved["revision"]
    live.playoff_scores["matchScores"][0]["alliances"][0]["teleopArtifactPoints"] += 1
    with pytest.raises(ValueError, match="reconcile"):
        asyncio.run(handle(db, Request("POST", live.model_dump(mode="json")), YEAR, "A"))
    row = db.sql.execute("SELECT revision FROM forecast_input WHERE event_code = '2025-A'").fetchone()
    assert row[0] == saved["revision"]


@pytest.mark.parametrize("case", ["seeds", "teams", "breakdown", "series", "duplicate", "after-final"])
def test_invalid_progress_is_rejected_before_publication(fixture, case):
    bad = setting(fixture, [(120, 80)])
    if case == "seeds":
        bad.alliances["alliances"][0]["number"] = 3
    elif case == "teams":
        bad.playoff_schedule["schedule"][0]["teams"][0]["teamNumber"] = 5
    elif case == "breakdown":
        bad.playoff_scores["matchScores"][0]["alliances"][0]["teleopArtifactPoints"] += 1
    elif case == "series":
        bad.playoff_schedule["schedule"][0]["series"] = 2
        bad.playoff_scores["matchScores"][0]["matchSeries"] = 2
    elif case == "duplicate":
        bad.playoff_schedule["schedule"] *= 2
    else:
        bad = setting(fixture, [(120, 80), (120, 80), (120, 80)])
    with pytest.raises(ValueError):
        snapshot(fixture, bad)
