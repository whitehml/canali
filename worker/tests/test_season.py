"""Native replay parity and real SQLite checks for D1 transactions and invalidation."""

import asyncio
import json
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from engine_version import ENGINE_VERSION
from season import (
    SAVE_CURSOR,
    SAVE_RATINGS,
    Config,
    Event,
    handle,
    init_scale,
    priors,
    refresh_scale,
    replay,
    source_current,
    state_for,
)

from epa.constants import for_season
from epa.replay import TAG_POST_EVENT, TAG_PRE_EVENT, replay_season
from epa.scale import compute_scale
from warehouse.rules.partition import ResolvedPartition

YEAR = 2025
ROOT = Path(__file__).resolve().parents[1]


class Statement:
    def __init__(self, db, sql, args=()):
        self.db, self.sql, self.args = db, sql, args

    def bind(self, *args):
        return Statement(self.db, self.sql, args)

    async def all(self):
        rows = self.db.sql.execute(self.sql, self.args).fetchall()
        return SimpleNamespace(results=[SimpleNamespace(**dict(row)) for row in rows])

    async def first(self):
        rows = (await self.all()).results
        return rows[0] if rows else None

    async def run(self):
        return await self.all()


class Database:
    def __init__(self):
        self.sql = sqlite3.connect(":memory:", isolation_level=None)
        self.sql.row_factory = sqlite3.Row
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            self.sql.executescript(path.read_text())

    def prepare(self, sql):
        return Statement(self, sql)

    async def batch(self, statements):
        self.sql.execute("BEGIN")
        try:
            result = [await statement.all() for statement in statements]
            self.sql.execute("COMMIT")
            return result
        except Exception:
            self.sql.execute("ROLLBACK")
            raise


class Request:
    def __init__(self, method, document):
        self.method, self.document = method, document

    async def text(self):
        return json.dumps(self.document)


def call(db, action=None, code=None, method="GET", document=None):
    return asyncio.run(handle(db, Request(method, document), YEAR, action, code))


def event(code, day, red=120, blue=80):
    return Event(
        code=code,
        event_id=uuid.uuid5(uuid.NAMESPACE_DNS, code),
        day=day,
        event_type="League Meet",
        roster=[1, 2, 3, 4, 5],
        matches=[
            {"ordinal": 1, "red": [1, 2], "blue": [3, 4], "red_score": red, "blue_score": blue},
            {"ordinal": 2, "red": [1, 3], "blue": [2, 5], "red_score": None, "blue_score": None},
        ],
    )


def event_source(e):
    return {
        "event": {"code": e.code, "eventId": str(e.event_id), "dateStart": e.day.isoformat(), "typeName": e.event_type},
        "teams": {"teams": [{"teamNumber": t} for t in e.roster]},
        "schedule": {
            "schedule": [
                {
                    "matchNumber": m.ordinal,
                    "tournamentLevel": "QUALIFICATION",
                    "teams": [
                        {"station": f"{side.title()}{index}", "teamNumber": team}
                        for side in ("red", "blue")
                        for index, team in enumerate(getattr(m, side), 1)
                    ],
                    "scoreRedFinal": m.red_score,
                    "scoreBlueFinal": m.blue_score,
                    "scoreRedFoul": 0,
                    "scoreBlueFoul": 0,
                }
                for m in e.matches
            ]
        },
    }


def upload(db, e):
    return call(db, "events", e.code, "PUT", event_source(e))


def test_event_checkpoints_equal_one_native_replay():
    events = [event("A", "2025-10-01"), event("B", "2025-11-02", 100, 60), event("C", "2025-12-03", 30, 200)]
    config = Config(previous_norm={1: 1600}, second_norm={2: 1550})
    scale = init_scale(YEAR, [])
    history = {}
    starts = {}
    for e in events:
        starts[e.code] = {p["number"]: p for p in priors(e, YEAR, history, config, scale)}
        history.update({r["team"]: r for r in json.loads(json.dumps(replay(e, YEAR, history, config, scale)))})
    native = replay_season(
        [m for e in events for m in e.stream(YEAR)],
        constants=for_season(YEAR),
        partition=ResolvedPartition.total_only(YEAR),
        season_scale=None,
        init_scale=scale,
        layoff_boost=for_season(YEAR).layoff_boost,
        previous_norm=config.previous_norm,
        second_norm=config.second_norm,
        compute_norm=False,
    )
    for row in native.rows:
        if row.tag == TAG_PRE_EVENT:
            code = next(e.code for e in events if e.event_id == row.event_id)
            assert starts[code][row.team_number]["epa"] == row.epa_scaled
    finals = {row.team_number: row.epa_scaled for row in native.rows if row.tag == TAG_POST_EVENT}
    assert {team: row["epa"] for team, row in history.items()} == finals
    assert history[1]["played"] == 3
    assert starts["C"][5]["played"] == 0


def test_d1_resume_noop_correction_config_and_old_writes():
    db = Database()
    assert call(db, method="PUT", document={})[1] == 200
    a, b, c = event("A", "2025-10-01"), event("B", "2025-11-02"), event("C", "2025-12-03")
    for e in (c, a, b):
        assert upload(db, e)[1] == 200
    revision = call(db)[0]["revision"]
    assert upload(db, b)[0]["revision"] == revision
    assert call(db, "priors", "B")[1] == 409
    assert call(db, "step", method="POST")[0]["event"] == "A"
    before = call(db, "priors", "B")[0]
    assert before["teams"][0]["played"] == 1
    assert call(db, "step", method="POST")[0]["event"] == "B"
    assert call(db, "priors", "B")[0] == before
    assert call(db, "step", method="POST")[0]["event"] == "C"
    old = asyncio.run(state_for(db, YEAR))
    assert call(db, "step", method="POST")[0]["status"] == "complete"
    b.matches[0].red_score = 150
    upload(db, b)
    assert call(db)[0]["cursor"] == a.key
    assert [r[0] for r in db.sql.execute("SELECT DISTINCT event_key FROM season_rating")] == [a.key]
    stale = asyncio.run(
        db.batch(
            [
                db.prepare(SAVE_RATINGS).bind(
                    YEAR,
                    c.key,
                    json.dumps([{"team": 1, "epa": -999, "played": 99, "last_played": "2025-12-03"}]),
                    YEAR,
                    old.revision,
                    old.cursor,
                    ENGINE_VERSION,
                ),
                db.prepare(SAVE_CURSOR).bind(c.key, YEAR, old.revision, old.cursor, ENGINE_VERSION),
            ]
        )
    )
    assert stale[1].results == []
    assert call(db, "step", method="POST")[0]["event"] == "B"
    changed = call(db, "priors", "C")[0]
    assert changed["teams"][0]["epa"] > before["teams"][0]["epa"]
    assert call(db, "step", method="POST")[0]["event"] == "C"
    assert call(db, method="PUT", document={"previous_norm": {"1": 1650}})[0]["cursor"] == ""
    assert db.sql.execute("SELECT count(*) FROM season_rating").fetchone()[0] == 0


def test_initialization_window_and_prefix_corrections_reset_all():
    db = Database()
    call(db, method="PUT", document={})
    a, b, c = event("A", "2025-10-01"), event("B", "2025-11-02"), event("C", "2025-12-03")
    for e in (a, b):
        match = e.matches[0]
        e.matches = [match.model_copy(update={"ordinal": i + 1}) for i in range(1000)]
    for e in (a, b, c):
        upload(db, e)
    state = asyncio.run(state_for(db, YEAR))
    assert asyncio.run(refresh_scale(db, YEAR, state))
    expected = asdict(compute_scale([120.0, 80.0] * 2000, YEAR))
    assert call(db)[0]["init_scale"] == expected
    db.sql.execute(
        "INSERT INTO season_rating(season,event_key,team,epa,played,last_played) VALUES (?, ?, 1, 50, 1000, ?)",
        (YEAR, a.key, a.day.isoformat()),
    )
    db.sql.execute("UPDATE season_run SET cursor = ?", (c.key,))
    c.matches[0].red_score += 10
    upload(db, c)
    assert asyncio.run(refresh_scale(db, YEAR, asyncio.run(state_for(db, YEAR))))
    assert call(db)[0]["cursor"] == b.key
    assert db.sql.execute("SELECT count(*) FROM season_rating").fetchone()[0] == 1
    b.matches[0].red_score += 10
    upload(db, b)
    assert call(db)[0]["cursor"] == a.key
    assert asyncio.run(refresh_scale(db, YEAR, asyncio.run(state_for(db, YEAR))))
    assert call(db)[0]["cursor"] == ""
    assert db.sql.execute("SELECT count(*) FROM season_rating").fetchone()[0] == 0


def test_duplicate_steps_and_stale_season_priors_cannot_publish():
    db = Database()
    call(db, method="PUT", document={})
    a = event("A", "2025-10-01")
    upload(db, a)
    call(db, "step", method="POST")
    state = asyncio.run(state_for(db, YEAR))
    result = asyncio.run(
        db.batch(
            [
                db.prepare(SAVE_RATINGS).bind(YEAR, a.key, "[]", YEAR, state.revision, "", ENGINE_VERSION),
                db.prepare(SAVE_CURSOR).bind(a.key, YEAR, state.revision, "", ENGINE_VERSION),
            ]
        )
    )
    assert result[1].results == []
    document = json.dumps({"season_prior": {"season": YEAR, "revision": state.revision}})
    db.sql.execute("INSERT INTO event_input(event_code, revision, document) VALUES ('TEST', 1, ?)", (document,))
    query = "SELECT 1 FROM event_input i WHERE " + source_current("i")
    assert db.sql.execute(query).fetchone() is not None
    db.sql.execute("UPDATE season_run SET engine_version = 'different-engine'")
    assert db.sql.execute(query).fetchone() is None
    assert call(db)[0]["cursor"] == ""
    assert call(db, "priors", "A")[1] == 409


@pytest.mark.parametrize("change", ["remote", "hybrid", "practice", "half-score", "duplicate-team"])
def test_reject_invalid_season_sources(change):
    db = Database()
    call(db, method="PUT", document={})
    e = event("A", "2025-10-01")
    if change in ("remote", "hybrid"):
        # Source identity is validated independently from the prepared model.
        from season import prepare

        with pytest.raises(ValueError):
            prepare({"event": {"code": "A", change: True}, "teams": {}, "schedule": {}}, "A")
    else:
        bad = e.model_dump(mode="json")
        if change == "practice":
            bad["event_type"] = "Scrimmage"
        elif change == "half-score":
            bad["matches"][0]["blue_score"] = None
        else:
            bad["roster"].append(1)
        with pytest.raises(ValueError):
            Event.model_validate(bad)
