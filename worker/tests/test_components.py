"""Component priors match native replay and invalid breakdowns cannot replace good data."""

import json
from copy import deepcopy

import pytest
from components import partition_for, values
from season import Config, prepare, priors, replay
from test_season import Database, call, event, event_source

from epa.constants import for_season
from epa.replay import TAG_POST_EVENT, TAG_PRE_EVENT, replay_season
from warehouse.rules.model import RulePack

YEAR = 2025


@pytest.fixture
def pack():
    return RulePack(
        season=YEAR,
        game="TEST",
        components=[
            {"name": "autoPoints", "kind": "numeric", "partition_group": "leaf", "is_subtotal": True},
            {"name": "teleopPoints", "kind": "numeric", "partition_group": "leaf", "is_subtotal": True},
        ],
    )


def source(code="A", day="2025-10-01", red_auto=30, blue_auto=10):
    document = event_source(event(code, day))
    document["scores"] = {
        "matchScores": [
            {
                "matchLevel": "QUALIFICATION",
                "matchSeries": 0,
                "matchNumber": 1,
                "alliances": [
                    {"alliance": "Red", "autoPoints": red_auto, "teleopPoints": 120 - red_auto, "preFoulTotal": 0},
                    {"alliance": "Blue", "autoPoints": blue_auto, "teleopPoints": 80 - blue_auto, "preFoulTotal": 0},
                ],
            }
        ]
    }
    return document


def test_component_checkpoints_and_layoff_equal_native_replay(pack):
    from season import init_scale

    config = Config(rule_pack=pack, previous_norm={1: 1600})
    scale = init_scale(YEAR, [])
    sources = [source(), source("B", "2025-11-20", 100, 40), source("C", "2026-01-02", 0, 80)]
    events = [prepare(s, s["event"]["code"], pack) for s in sources]
    starts, history = {}, {}
    for e in events:
        starts[e.code] = {row["number"]: row for row in priors(e, YEAR, history, config, scale)}
        history.update({row["team"]: row for row in json.loads(json.dumps(replay(e, YEAR, history, config, scale)))})
    matches = [m for e in events for m in e.stream(YEAR)]
    breakdowns = {
        (m.match_id, side): getattr(e.matches[0], f"{side.lower()}_components")
        for e in events
        for m in e.stream(YEAR)
        for side in ("RED", "BLUE")
    }
    native = replay_season(
        matches,
        constants=for_season(YEAR),
        partition=partition_for(YEAR, pack),
        season_scale=None,
        init_scale=scale,
        layoff_boost=for_season(YEAR).layoff_boost,
        previous_norm=config.previous_norm,
        breakdowns=breakdowns,
        compute_norm=False,
    )
    for row in native.rows:
        if row.tag == TAG_PRE_EVENT:
            code = next(e.code for e in events if e.event_id == row.event_id)
            assert starts[code][row.team_number]["components"] == row.components
            assert starts[code][row.team_number]["epa"] == row.epa_scaled
    finals = {row.team_number: row.components for row in native.rows if row.tag == TAG_POST_EVENT}
    assert {team: row["components"] for team, row in history.items()} == finals
    assert starts["C"][5]["played"] == 0
    assert sum(starts["C"][5]["components"].values()) == starts["C"][5]["epa"]


def test_state_scoring_and_boolean_projection():
    pack = RulePack(
        season=YEAR,
        game="TEST",
        components=[
            {"name": "robot1", "kind": "enum", "level": "team"},
            {"name": "robot2", "kind": "enum", "level": "team"},
            {"name": "flag", "kind": "boolean", "partition_group": "leaf"},
            {
                "name": "finishPoints",
                "kind": "numeric",
                "partition_group": "leaf",
                "state_scoring": {"states_from": ["robot1", "robot2"], "points": {"FULL": 10, "PARTIAL": 5}},
            },
        ],
    )
    assert values(pack, {"flag": True, "robot1": "FULL", "robot2": "UNKNOWN"}) == {"flag": 1.0, "finish_points": 10.0}
    with pytest.raises(ValueError, match="missing robot state"):
        values(pack, {"flag": True, "robot1": "FULL"})
    with pytest.raises(ValueError, match="boolean"):
        values(pack, {"flag": 1, "robot1": "FULL", "robot2": "PARTIAL"})


@pytest.mark.parametrize(
    "case",
    [
        "missing-scores",
        "missing-side",
        "duplicate",
        "wrong-match",
        "missing-component",
        "null",
        "nan",
        "boolean",
        "mismatch",
        "playoff",
    ],
)
def test_bad_breakdowns_do_not_replace_the_saved_event(pack, case):
    db = Database()
    call(db, method="PUT", document={"rule_pack": pack.model_dump(mode="json")})
    good = source()
    call(db, "events", "A", "PUT", good)
    revision = call(db)[0]["revision"]
    bad = deepcopy(good)
    scores = bad["scores"]["matchScores"]
    red = scores[0]["alliances"][0]
    if case == "missing-scores":
        bad.pop("scores")
    elif case == "missing-side":
        scores[0]["alliances"].pop()
    elif case == "duplicate":
        scores[0]["alliances"].append(red)
    elif case == "wrong-match":
        scores[0]["matchNumber"] = 99
    elif case == "missing-component":
        red.pop("autoPoints")
    elif case == "null":
        red["autoPoints"] = None
    elif case == "nan":
        red["autoPoints"] = float("nan")
    elif case == "boolean":
        red["autoPoints"] = True
    elif case == "mismatch":
        red["autoPoints"] += 1
    else:
        scores[0]["matchLevel"] = "PLAYOFF"
    with pytest.raises(ValueError):
        call(db, "events", "A", "PUT", bad)
    assert call(db)[0]["revision"] == revision
    assert json.loads(db.sql.execute("SELECT source FROM season_event").fetchone()[0]) == good


def test_d1_component_history_corrections_and_partition_change(pack):
    db = Database()
    call(db, method="PUT", document={})
    a, b = source(), source("B", "2025-11-01")
    call(db, "events", "A", "PUT", a)
    call(db, "events", "B", "PUT", b)
    call(db, method="PUT", document={"rule_pack": pack.model_dump(mode="json")})
    assert call(db, "step", method="POST")[1] == 409
    assert call(db, "priors", "B")[1] == 409
    for code, doc in (("A", a), ("B", b)):
        assert call(db, "events", code, "PUT", doc)[1] == 200
    assert call(db, "step", method="POST")[0]["event"] == "A"
    prior = call(db, "priors", "B")[0]
    assert prior["teams"][0]["played"] == 1
    assert set(prior["teams"][0]["components"]) == {"auto_points", "teleop_points"}
    assert call(db, "step", method="POST")[0]["event"] == "B"
    for row in db.sql.execute("SELECT epa,components FROM season_rating"):
        assert sum(json.loads(row["components"]).values()) == row["epa"]
    a["schedule"]["schedule"][0]["scoreRedFinal"] += 10
    a["scores"]["matchScores"][0]["alliances"][0]["autoPoints"] += 10
    call(db, "events", "A", "PUT", a)
    assert call(db, "priors", "B")[1] == 409
    assert call(db, "step", method="POST")[0]["event"] == "A"
    assert call(db, "priors", "B")[0]["teams"][0]["components"] != prior["teams"][0]["components"]
    changed = pack.model_copy(update={"version": "2"})
    call(db, method="PUT", document={"rule_pack": changed.model_dump(mode="json")})
    assert db.sql.execute("SELECT count(*) FROM season_rating").fetchone()[0] == 0
    assert call(db, "step", method="POST")[1] == 409


def test_rule_pack_must_match_season_and_supply_a_fittable_partition(pack):
    with pytest.raises(ValueError, match="match the replay season"):
        partition_for(2024, pack)
    with pytest.raises(ValueError, match="fittable partition"):
        partition_for(YEAR, RulePack(season=YEAR, game="EMPTY"))
