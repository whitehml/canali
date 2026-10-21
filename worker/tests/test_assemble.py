"""Snapshot assembly uses current ratings, official progress and guarded season revisions."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
from assemble import AssemblyInput, assemble, handle
from components import component_value
from engine_version import ENGINE_VERSION
from forecast_store import READ_STATE, SAVE_STATE, save_input
from forecast_store import handle as forecast_handle
from season import Config, init_scale, prepare
from snapshot import EventSnapshot
from test_season import Database, Request, call, event, event_source

from epa.constants import for_season
from epa.replay import TAG_POST_EVENT, replay_season
from sim.runner import run_event
from warehouse.rules.model import RulePack
from warehouse.rules.partition import resolve_partition

YEAR = 2025
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture():
    return json.loads((ROOT / "fixtures/forecast.json").read_text())


def source(code="A", day="2025-10-01"):
    document = event_source(event(code, day))
    document["schedule"]["schedule"][1].update(scoreRedFinal=172, scoreBlueFinal=149)
    document["teams"]["teams"][0].update(city="Town", rookieYear=YEAR)
    document["teams"]["teams"][1].update(city="Town", rookieYear=YEAR - 2)
    document["schedule"]["schedule"][0]["teams"][0]["dq"] = True
    document["schedule"]["schedule"][1]["teams"][0].update(dq=True, surrogate=True)
    document["scores"] = {"matchScores": []}
    for number, scores in ((1, (120, 80)), (2, (172, 149))):
        sides = []
        for name, score in zip(("Red", "Blue"), scores, strict=True):
            sides.append(
                {
                    "alliance": name,
                    "autoArtifactPoints": 24,
                    "autoLeavePoints": 6,
                    "autoPatternPoints": 0,
                    "teleopArtifactPoints": score - 40,
                    "teleopBasePoints": 10,
                    "teleopDepotPoints": 0,
                    "teleopPatternPoints": 0,
                    "autoPoints": 30,
                    "teleopPoints": score - 30,
                    "preFoulTotal": 0,
                    "movementRP": True,
                    "goalRP": False,
                    "patternRP": False,
                }
            )
        document["scores"]["matchScores"].append(
            {"matchLevel": "QUALIFICATION", "matchSeries": 0, "matchNumber": number, "alliances": sides}
        )
    return document


def options(fixture, **overrides):
    return AssemblyInput.model_validate(
        {
            "models": fixture["models"],
            "selection_published": True,
            "through": 1,
            "context": {
                "allied": {1: [2]},
                "history": {1: {"InspirePlaced": 1.0}},
                "inspire_won_before": [2],
                "slots": {"count": 2, "already_advanced": [4]},
            },
            "min_iterations": 1,
            "max_iterations": 1,
            "tolerance": 0.5,
            "seed": 42,
            **overrides,
        }
    )


def test_current_components_and_progress_equal_native_at_cutoff(fixture):
    pack = RulePack.model_validate(fixture["rule_pack"])
    raw = source()
    event_input = prepare(raw, "A", pack)
    snapshot = assemble(
        event_input, raw, Config(rule_pack=pack), init_scale(YEAR, []), history={}, options=options(fixture)
    )
    first = next(event_input.stream(YEAR))
    partition = resolve_partition(YEAR, [c.model_dump() for c in pack.components])
    # Native typed-view columns are projected independently of the assembly's field builder.
    by_column = {c.column_name: c.name for c in pack.components}
    native = replay_season(
        [first],
        constants=for_season(YEAR),
        partition=partition,
        season_scale=None,
        init_scale=init_scale(YEAR, []),
        layoff_boost=for_season(YEAR).layoff_boost,
        breakdowns={
            (first.match_id, side["alliance"].upper()): {
                column: float(side[by_column[column]]) for column in partition.columns
            }
            for side in raw["scores"]["matchScores"][0]["alliances"]
        },
        compute_norm=False,
    )
    for row in native.rows:
        if row.tag == TAG_POST_EVENT:
            assert snapshot.field.predicted[row.team_number] == row.components
    assert snapshot.as_of_match == 1
    assert snapshot.last_match.model_dump() == {"level": "QUALIFICATION", "number": 1, "series": None}
    assert snapshot.progress.played == [True, False]
    assert snapshot.progress.score == [[120, 80], [0, 0]]
    assert snapshot.progress.components["preFoulTotal"] == [[120, 80], [0, 0]]
    assert snapshot.progress.components["autoPoints"] == [[30, 30], [0, 0]]
    assert snapshot.progress.dq[0][0][0] is True
    assert not any(flag for side in snapshot.progress.dq[1] for flag in side)
    assert snapshot.schedule.surrogate[1][0][0] is True
    assert snapshot.field.rookie == frozenset({1})
    assert snapshot.field.team_age == {1: 0, 2: 2}
    assert snapshot.field.city[1] == "Town"
    assert snapshot.slots.already_advanced == frozenset({4})
    assert event_input.matches[1].red_score == 172
    changed = deepcopy(raw)
    changed["schedule"]["schedule"][1]["scoreRedFinal"] += 20
    changed["scores"]["matchScores"][1]["alliances"][0]["teleopArtifactPoints"] += 20
    changed["scores"]["matchScores"][1]["alliances"][0]["teleopPoints"] += 20
    later = assemble(
        prepare(changed, "A", pack),
        changed,
        Config(rule_pack=pack),
        init_scale(YEAR, []),
        history={},
        options=options(fixture),
    )
    assert snapshot.model_dump() == later.model_dump()


def test_zero_cutoff_and_unknown_schedule(fixture):
    pack = RulePack.model_validate(fixture["rule_pack"])
    raw = source()
    snap = assemble(
        prepare(raw, "A", pack),
        raw,
        Config(rule_pack=pack),
        init_scale(YEAR, []),
        history={},
        options=options(fixture, through=0),
    )
    assert snap.progress.played == [False, False]
    assert all(
        sum(values.values()) == pytest.approx(init_scale(YEAR, []).team_mean - 0.2 * init_scale(YEAR, []).sigma)
        for values in snap.field.predicted.values()
    )
    raw["schedule"]["schedule"] = []
    raw["scores"]["matchScores"] = []
    snap = assemble(
        prepare(raw, "A", pack),
        raw,
        Config(rule_pack=pack),
        init_scale(YEAR, []),
        history={},
        options=options(fixture, through=0, selection_published=False),
    )
    assert snap.schedule is None and snap.progress is None


def test_explicit_end_of_qualifications_replays_before_selection(fixture):
    pack = RulePack.model_validate(fixture["rule_pack"])
    raw = source()
    snapshot = assemble(
        prepare(raw, "A", pack),
        raw,
        Config(rule_pack=pack),
        init_scale(YEAR, []),
        history={},
        options=options(fixture, through=2),
    )
    assert snapshot.progress.played == [True, True]
    assert snapshot.progress.alliances is None


@pytest.mark.parametrize(
    "case", ["after-selection", "beyond-cutoff", "foreign-context", "no-components", "missing-bonus"]
)
def test_reject_unsupported_or_incomplete_inputs(fixture, case):
    pack = RulePack.model_validate(fixture["rule_pack"])
    raw = source()
    setting = options(fixture)
    config = Config(rule_pack=pack)
    if case == "after-selection":
        setting.through = None
    elif case == "beyond-cutoff":
        setting.through = 3
    elif case == "foreign-context":
        setting.context.allied[99] = frozenset()
    elif case == "no-components":
        config.rule_pack = None
    else:
        raw["scores"]["matchScores"][0]["alliances"][0].pop("goalRP")
    with pytest.raises(ValueError):
        assemble(prepare(raw, "A", pack), raw, config, init_scale(YEAR, []), history={}, options=setting)


def test_recovered_components_are_recursive_and_finite(fixture):
    pack = RulePack.model_validate(fixture["rule_pack"])
    assert component_value(pack, "preFoulTotal", {"preFoulTotal": 0, "autoPoints": 32, "teleopPoints": 139}) == 171
    with pytest.raises(ValueError):
        component_value(pack, "preFoulTotal", {"autoPoints": float("nan"), "teleopPoints": 139})


def test_d1_assembly_result_parity_and_stale_season_guards(fixture):
    db = Database()
    call(db, method="PUT", document={"rule_pack": fixture["rule_pack"]})
    raw = source()
    call(db, "events", "A", "PUT", raw)
    call(db, "step", method="POST")
    setting = options(fixture)
    report, status = asyncio.run(handle(db, Request("POST", setting.model_dump(mode="json")), YEAR, "A"))
    assert status == 200
    snapshot = EventSnapshot.model_validate(report["snapshot"])
    assert snapshot.season_prior.revision == call(db)[0]["revision"]
    repeated, status = asyncio.run(handle(db, Request("POST", setting.model_dump(mode="json")), YEAR, "A"))
    assert status == 200 and repeated["revision"] == report["revision"]
    actual, status = asyncio.run(forecast_handle(db, Request("POST", None), "2025-A", True))
    assert status == 200 and actual["status"] == "complete"
    native = run_event(
        snapshot.draw(),
        snapshot.rule_pack,
        snapshot.models.build(),
        np.random.default_rng(snapshot.seed),
        slots=snapshot.slots,
        progress=snapshot.progress.build(),
        tolerance=snapshot.tolerance,
        min_iterations=1,
    )
    assert actual["result"] == asdict(native)
    assert asyncio.run(save_input(db, "2025-A", snapshot))[0]["revision"] == report["revision"]
    assert asyncio.run(forecast_handle(db, Request("GET", None), "2025-A", False))[1] == 200
    old_step = db.sql.execute("SELECT step FROM forecast_state").fetchone()[0]
    raw["schedule"]["schedule"][1]["scoreRedFinal"] += 1
    raw["scores"]["matchScores"][1]["alliances"][0]["teleopArtifactPoints"] += 1
    raw["scores"]["matchScores"][1]["alliances"][0]["teleopPoints"] += 1
    call(db, "events", "A", "PUT", raw)
    assert db.sql.execute(READ_STATE, ("2025-A", ENGINE_VERSION)).fetchone() is None
    assert (
        db.sql.execute(
            SAVE_STATE, ("{}", "running", "{}", "2025-A", 1, ENGINE_VERSION, old_step, "2025-A", 1)
        ).fetchone()
        is None
    )
    assert asyncio.run(forecast_handle(db, Request("GET", None), "2025-A", False))[1] == 404
    assert asyncio.run(forecast_handle(db, Request("POST", None), "2025-A", True))[1] == 404
    assert asyncio.run(save_input(db, "2025-A", snapshot))[1] == 409
    assert asyncio.run(handle(db, Request("POST", setting.model_dump(mode="json")), YEAR, "A"))[1] == 409


def test_context_requires_explicit_history_and_slots(fixture):
    body = options(fixture).model_dump(mode="json")
    body.pop("context")
    with pytest.raises(ValueError):
        AssemblyInput.model_validate(body)
    body["context"] = {"allied": {}, "history": {"1": {"Invented": 1}}, "inspire_won_before": [], "slots": None}
    with pytest.raises(ValueError):
        AssemblyInput.model_validate(body)
