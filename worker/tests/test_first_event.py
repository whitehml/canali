"""FIRST adapters preserve scored/unplayed distinctions and revision safety."""

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from first_event import IMPORT_INPUT, prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    parameters = json.loads((ROOT / "fixtures/event.json").read_text())
    parameters.pop("matches")
    slots = [
        {"teamNumber": team, "station": station, "surrogate": False, "noShow": False}
        for team, station in zip([1, 2, 3, 4], ["Red1", "Red2", "Blue1", "Blue2"], strict=True)
    ]
    match = {
        "matchNumber": 1,
        "tournamentLevel": "QUALIFICATION",
        "teams": slots,
        "scoreRedFinal": 135,
        "scoreRedFoul": 0,
        "scoreBlueFinal": 80,
        "scoreBlueFoul": 15,
    }
    unplayed = {**deepcopy(match), "matchNumber": 2, "scoreRedFinal": None, "scoreBlueFinal": None}
    return {
        "season": 2025,
        "event_code": "TEST",
        "teams": {"teams": [{"teamNumber": t} for t in range(1, 5)]},
        "schedule": {"schedule": [unplayed, match]},
        "parameters": parameters,
    }


def test_non_foul_scores_station_order_and_unplayed(source):
    source["schedule"]["schedule"][1]["teams"].reverse()
    document = prepare(source, "2025-TEST")
    assert document["matches"] == [
        {"ordinal": 1, "red": [1, 2], "blue": [3, 4], "red_score": 120, "blue_score": 80},
        {"ordinal": 2, "red": [1, 2], "blue": [3, 4], "red_score": None, "blue_score": None},
    ]


def test_no_shows_excluded_surrogates_and_dqs_retained(source):
    slots = source["schedule"]["schedule"][1]["teams"]
    slots[0]["noShow"] = True
    slots[1]["surrogate"] = True
    slots[2]["dq"] = True
    document = prepare(source, "2025-TEST")
    assert document["matches"][0]["red"] == [2]
    assert document["matches"][0]["blue"] == [3, 4]


@pytest.mark.parametrize("case", ["roster", "station", "duplicate", "foul", "half", "ordinal", "level", "empty"])
def test_incomplete_or_ambiguous_data_rejected(source, case):
    match = source["schedule"]["schedule"][1]
    if case == "roster":
        source["parameters"]["teams"].pop()
    elif case == "station":
        match["teams"][0]["station"] = "Blue1"
    elif case == "duplicate":
        match["teams"][0]["teamNumber"] = 2
    elif case == "foul":
        match["scoreRedFoul"] = None
    elif case == "half":
        match["scoreBlueFinal"] = None
    elif case == "ordinal":
        match["matchNumber"] = 2
    elif case == "level":
        match["tournamentLevel"] = "PLAYOFF"
    else:
        source["schedule"]["schedule"] = []
    with pytest.raises(ValueError):
        prepare(source, "2025-TEST")


def test_season_namespaces_event_key(source):
    with pytest.raises(ValueError, match="season-event_code"):
        prepare(source, "TEST")


def test_atomic_import_noop_correction_and_manual_replacement(source):
    db = sqlite3.connect(":memory:")
    db.executescript((ROOT / "migrations/0001_event_inputs.sql").read_text())
    db.executescript((ROOT / "migrations/0003_event_sources.sql").read_text())

    def save():
        document = json.dumps(prepare(source, "2025-TEST"), sort_keys=True)
        return db.execute(IMPORT_INPUT, ("2025-TEST", document, json.dumps(source, sort_keys=True))).fetchone()[0]

    assert save() == 1
    db.execute("INSERT INTO event_result VALUES ('2025-TEST', 1, '{}', CURRENT_TIMESTAMP)")
    assert save() == 1
    source["schedule"]["schedule"][1]["scoreRedFinal"] += 10
    assert save() == 2
    assert db.execute("SELECT source_document FROM event_input").fetchone()[0] == json.dumps(source, sort_keys=True)
    assert (
        db.execute(
            "SELECT r.document FROM event_result r JOIN event_input i "
            "ON i.event_code = r.event_code AND i.revision = r.input_revision"
        ).fetchone()
        is None
    )
    db.execute("UPDATE event_input SET source_document = NULL, revision = revision + 1")
    assert save() == 4
