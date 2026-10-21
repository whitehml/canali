"""One-event EPA replay steps backed by indexed D1 team history."""

import json
import re
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import date

from components import attach, partition_for, version
from engine_version import ENGINE_VERSION
from first_event import qualification_matches
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, PositiveInt, model_validator

from epa.constants import MEASURED_INIT_SCALES, for_season
from epa.corpus import Alliance, RatedMatch
from epa.replay import TAG_POST_EVENT, TeamSeed, TeamState, apply_layoff, initial_series, replay_season
from epa.scale import Scale, carry_forward, compute_scale, provisional_scale
from warehouse.rules.model import RulePack
from warehouse.tier import tier_for

SAVE_RATINGS = (
    "INSERT OR REPLACE INTO season_rating(season, event_key, team, epa, played, last_played, components) "
    "SELECT ?, ?, json_extract(value, '$.team'), json_extract(value, '$.epa'), "
    "json_extract(value, '$.played'), json_extract(value, '$.last_played'), "
    "coalesce(json_extract(value, '$.components'), '{}') FROM json_each(?) "
    "WHERE EXISTS (SELECT 1 FROM season_run WHERE season = ? AND revision = ? AND cursor = ? "
    "AND scale_dirty = 0 AND engine_version = ?)"
)
SAVE_CURSOR = (
    "UPDATE season_run SET cursor = ? WHERE season = ? AND revision = ? AND cursor = ? "
    "AND scale_dirty = 0 AND engine_version = ? RETURNING cursor"
)
SCALE_SCORES = (
    "SELECT json_extract(m.value, '$.red_score') AS red, json_extract(m.value, '$.blue_score') AS blue "
    "FROM season_event e, json_each(e.document, '$.matches') m "
    "WHERE e.season = ? AND json_extract(m.value, '$.red_score') IS NOT NULL "
    "ORDER BY e.event_key, cast(json_extract(m.value, '$.ordinal') AS INTEGER) LIMIT ?"
)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_norm: dict[PositiveInt, FiniteFloat] = Field(default_factory=dict, max_length=20_000)
    second_norm: dict[PositiveInt, FiniteFloat] = Field(default_factory=dict, max_length=20_000)
    rule_pack: RulePack | None = None


class Match(BaseModel):
    ordinal: PositiveInt
    red: list[PositiveInt] = Field(min_length=1, max_length=2)
    blue: list[PositiveInt] = Field(min_length=1, max_length=2)
    red_score: FiniteFloat | None = Field(ge=0)
    blue_score: FiniteFloat | None = Field(ge=0)
    red_components: dict[str, FiniteFloat] | None = None
    blue_components: dict[str, FiniteFloat] | None = None

    @model_validator(mode="after")
    def valid_match(self):
        if (self.red_score is None) != (self.blue_score is None):
            raise ValueError("both scores must be present or null")
        if len(set(self.red + self.blue)) != len(self.red + self.blue):
            raise ValueError("scheduled teams must be distinct")
        return self


class Event(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$")
    event_id: uuid.UUID
    day: date
    event_type: str
    roster: list[PositiveInt] = Field(min_length=4, max_length=128)
    matches: list[Match] = Field(max_length=1000)
    partition_version: str = ""

    @property
    def key(self):
        return f"{self.day.isoformat()}:{self.code}"

    @model_validator(mode="after")
    def valid_event(self):
        if tier_for(self.event_type) is None:
            raise ValueError("event type is not rated")
        if len(set(self.roster)) != len(self.roster):
            raise ValueError("roster teams must be unique")
        previous = 0
        for match in self.matches:
            if match.ordinal <= previous or not set(match.red + match.blue) <= set(self.roster):
                raise ValueError("match ordinals must increase and teams must be on the roster")
            previous = match.ordinal
        return self

    def stream(self, season):
        for match in self.matches:
            if match.red_score is not None:
                yield RatedMatch(
                    match_id=uuid.uuid5(self.event_id, str(match.ordinal)),
                    event_id=self.event_id,
                    season=season,
                    event_code=self.code,
                    event_match_ordinal=match.ordinal,
                    red=Alliance(tuple(match.red), match.red_score),
                    blue=Alliance(tuple(match.blue), match.blue_score),
                    event_type=self.event_type,
                    event_date=self.day,
                )


def prepare(source: dict, code: str, pack: RulePack | None = None) -> Event:
    if (
        not isinstance(source, dict)
        or not {"event", "teams", "schedule"} <= set(source)
        or set(source) - {"event", "teams", "schedule", "scores"}
    ):
        raise ValueError("season event needs event metadata, teams and schedule")
    event = source["event"]
    if event["code"] != code or event.get("remote") or event.get("hybrid"):
        raise ValueError("event code must match a non-remote, non-hybrid event")
    roster = [team["teamNumber"] for team in source["teams"]["teams"]]
    prepared = Event(
        code=code,
        event_id=event["eventId"],
        day=date.fromisoformat(event["dateStart"][:10]),
        event_type=event["typeName"],
        roster=roster,
        matches=qualification_matches(roster, source["schedule"]),
        partition_version=version(pack),
    )
    if pack is not None:
        if "scores" not in source:
            raise ValueError("component replay requires official score breakdowns")
        attach(prepared.matches, source["scores"], pack)
    return prepared


def init_scale(season: int, scores: list[float]) -> Scale:
    window = for_season(season).init_window
    if len(scores) < window:
        return provisional_scale(season, list(MEASURED_INIT_SCALES.values()))
    return compute_scale(scores, season, window=window)


def priors(event: Event, season: int, history: dict, config: Config, scale: Scale) -> list[dict]:
    constants = for_season(season)
    partition = partition_for(season, config.rule_pack)
    result = []
    for team in sorted(event.roster):
        old = history.get(team)
        components = (
            (old["components"] if config.rule_pack else {"__total__": old["epa"]})
            if old
            else initial_series(
                carry_forward(
                    previous_norm=config.previous_norm.get(team),
                    second_norm=config.second_norm.get(team),
                    carryover=constants.carryover,
                    init_scale=scale,
                ),
                partition,
            )
        )
        state = TeamState(team_number=team, series=dict(components), played=old["played"] if old else 0)
        apply_layoff(
            state, constants.layoff_boost, event.day, date.fromisoformat(old["last_played"]) if old else None, scale
        )
        row = {"number": team, "epa": state.total, "played": state.played}
        if config.rule_pack:
            row["components"] = state.series
        result.append(row)
    return result


def replay(
    event: Event, season: int, history: dict, config: Config, scale: Scale, *, playoffs=(), playoff_breakdowns=None
) -> list[dict]:
    constants = for_season(season)
    qualifications = list(event.stream(season))
    matches = [*qualifications, *playoffs]
    result = replay_season(
        matches,
        constants=constants,
        partition=partition_for(season, config.rule_pack),
        season_scale=None,
        init_scale=scale,
        layoff_boost=constants.layoff_boost,
        previous_norm=config.previous_norm,
        second_norm=config.second_norm,
        compute_norm=False,
        seed={
            team: TeamSeed(
                series=row["components"] if config.rule_pack else {"__total__": row["epa"]},
                played=row["played"],
                last_played=date.fromisoformat(row["last_played"]),
            )
            for team, row in history.items()
        },
        breakdowns={
            (match.match_id, side): getattr(prepared, f"{side.lower()}_components")
            for match, prepared in zip(
                qualifications, (m for m in event.matches if m.red_score is not None), strict=True
            )
            for side in ("RED", "BLUE")
        }
        | (playoff_breakdowns or {})
        if config.rule_pack
        else None,
    )
    counts = Counter(team for match in qualifications for team in (*match.red.teams, *match.blue.teams))
    return [
        {
            "team": row.team_number,
            "epa": row.epa_scaled,
            "played": history.get(row.team_number, {}).get("played", 0) + counts[row.team_number],
            "last_played": event.day.isoformat(),
            "components": row.components,
        }
        for row in result.rows
        if row.tag == TAG_POST_EVENT
    ]


async def history_for(db, season: int, event: Event) -> dict:
    rows = (
        await db.prepare(
            "SELECT h.* FROM json_each(?) roster JOIN season_rating h ON h.season = ? AND h.team = roster.value "
            "AND h.event_key = (SELECT max(p.event_key) FROM season_rating p "
            "WHERE p.season = h.season AND p.team = h.team AND p.event_key < ?)"
        )
        .bind(json.dumps(event.roster), season, event.key)
        .all()
    )
    return {
        row.team: {
            "epa": row.epa,
            "played": row.played,
            "last_played": row.last_played,
            "components": json.loads(row.components),
        }
        for row in rows.results
    }


async def state_for(db, season):
    return await db.prepare("SELECT * FROM season_run WHERE season = ?").bind(season).first()


async def incompatible_event(db, season: int, config: Config):
    return (
        await db.prepare("SELECT event_code FROM season_event WHERE season = ? AND partition_version != ? LIMIT 1")
        .bind(season, version(config.rule_pack))
        .first()
    )


async def refresh_scale(db, season, state):
    rows = await db.prepare(SCALE_SCORES).bind(season, for_season(season).init_window // 2).all()
    scale = json.dumps(
        asdict(init_scale(season, [value for row in rows.results for value in (row.red, row.blue)])), sort_keys=True
    )
    saved = (
        await db.prepare(
            "UPDATE season_run SET init_scale = ?, scale_dirty = 0, "
            "cursor = CASE WHEN init_scale IS NOT ? THEN '' ELSE cursor END "
            "WHERE season = ? AND revision = ? AND engine_version = ? RETURNING revision"
        )
        .bind(scale, scale, season, state.revision, ENGINE_VERSION)
        .first()
    )
    return saved is not None


async def step(db, season, state):
    if state.engine_version != ENGINE_VERSION:
        await (
            db.prepare("UPDATE season_run SET engine_version = ? WHERE season = ? AND revision = ?")
            .bind(ENGINE_VERSION, season, state.revision)
            .run()
        )
        state = await state_for(db, season)
    config = Config.model_validate_json(state.config)
    incompatible = await incompatible_event(db, season, config)
    if incompatible:
        return {"error": f"reimport {incompatible.event_code} for the current rule pack before replay"}, 409
    if state.scale_dirty:
        if not await refresh_scale(db, season, state):
            return {"error": "season changed; retry"}, 409
        state = await state_for(db, season)
    if state.scale_dirty or state.engine_version != ENGINE_VERSION:
        return {"error": "season changed; retry"}, 409
    row = (
        await db.prepare(
            "SELECT document FROM season_event WHERE season = ? AND event_key > ? ORDER BY event_key LIMIT 1"
        )
        .bind(season, state.cursor)
        .first()
    )
    if row is None:
        return {"status": "complete", "revision": state.revision, "cursor": state.cursor}, 200
    event = Event.model_validate_json(row.document)
    history = await history_for(db, season, event)
    ratings = replay(event, season, history, config, Scale(**json.loads(state.init_scale)))
    batch = await db.batch(
        [
            db.prepare(SAVE_RATINGS).bind(
                season,
                event.key,
                json.dumps(ratings, allow_nan=False),
                season,
                state.revision,
                state.cursor,
                ENGINE_VERSION,
            ),
            db.prepare(SAVE_CURSOR).bind(event.key, season, state.revision, state.cursor, ENGINE_VERSION),
        ]
    )
    if not batch[1].results:
        return {"error": "season or checkpoint changed; retry"}, 409
    return {
        "status": "running",
        "revision": state.revision,
        "cursor": event.key,
        "event": event.code,
        "rated_teams": len(ratings),
    }, 202


async def handle(db, request, season: int, action: str | None, code: str | None) -> tuple[dict, int]:
    if action is None and request.method == "PUT":
        body = await request.text()
        if len(body.encode()) > 512_000:
            return {"error": "input exceeds 512000 bytes"}, 413
        parsed = Config.model_validate_json(body)
        partition_for(season, parsed.rule_pack)
        config = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, allow_nan=False)
        await (
            db.prepare(
                "INSERT INTO season_run(season, config, engine_version) VALUES (?, ?, ?) "
                "ON CONFLICT(season) DO UPDATE SET config = excluded.config, engine_version = excluded.engine_version"
            )
            .bind(season, config, ENGINE_VERSION)
            .run()
        )
    state = await state_for(db, season)
    if state is None:
        return {"error": "configure the season first"}, 404
    if action is None and request.method in ("PUT", "GET"):
        return {
            "season": season,
            "revision": state.revision,
            "cursor": state.cursor,
            "scale_dirty": bool(state.scale_dirty),
            "init_scale": json.loads(state.init_scale) if state.init_scale else None,
            "components": list(partition_for(season, Config.model_validate_json(state.config).rule_pack).columns),
        }, 200
    if action == "step" and request.method == "POST":
        return await step(db, season, state)
    if action == "events" and request.method == "PUT":
        body = await request.text()
        if len(body.encode()) > 512_000:
            return {"error": "input exceeds 512000 bytes"}, 413
        source = json.loads(body)
        event = prepare(source, code, Config.model_validate_json(state.config).rule_pack)
        await (
            db.prepare(
                "INSERT INTO season_event(season, event_code, event_key, document, source, partition_version) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(season, event_code) DO UPDATE SET event_key = excluded.event_key, "
                "document = excluded.document, source = excluded.source, partition_version = excluded.partition_version"
            )
            .bind(
                season,
                code,
                event.key,
                event.model_dump_json(),
                json.dumps(source, sort_keys=True, allow_nan=False),
                event.partition_version,
            )
            .run()
        )
        return {"event": code, "revision": (await state_for(db, season)).revision}, 200
    if action == "priors" and request.method == "GET":
        return await read_priors(db, season, code)
    return {"error": "method not allowed"}, 405


async def read_priors(db, season: int, code: str) -> tuple[dict, int]:
    state = await state_for(db, season)
    if state is None:
        return {"error": "configure the season first"}, 404
    if await incompatible_event(db, season, Config.model_validate_json(state.config)):
        return {"error": "reimport the season events for the current rule pack"}, 409
    row = (
        await db.prepare("SELECT document FROM season_event WHERE season = ? AND event_code = ?")
        .bind(season, code)
        .first()
    )
    if row is None:
        return {"error": "import this event into the season first"}, 404
    event = Event.model_validate_json(row.document)
    pending = (
        await db.prepare(
            "SELECT 1 AS pending FROM season_event WHERE season = ? AND event_key > ? AND event_key < ? LIMIT 1"
        )
        .bind(season, state.cursor, event.key)
        .first()
    )
    if pending or state.scale_dirty or state.engine_version != ENGINE_VERSION:
        return {"error": "replay preceding events before reading priors"}, 409
    teams = priors(
        event,
        season,
        await history_for(db, season, event),
        Config.model_validate_json(state.config),
        Scale(**json.loads(state.init_scale)),
    )
    current = await state_for(db, season)
    if current.revision != state.revision or current.scale_dirty:
        return {"error": "season changed; retry"}, 409
    constants = for_season(season)
    return {
        "teams": teams,
        "k": asdict(constants.k),
        "m": asdict(constants.m),
        "season_prior": {"season": season, "revision": state.revision},
    }, 200


def source_current(alias: str) -> str:
    """Keep results with frozen season priors hidden after corrections or engine changes."""
    if re.fullmatch(r"[a-z]+", alias) is None:
        raise ValueError("invalid SQL alias")
    return (
        f"(json_extract({alias}.document, '$.season_prior') IS NULL OR EXISTS "
        f"(SELECT 1 FROM season_run s WHERE s.season = json_extract({alias}.document, '$.season_prior.season') "
        f"AND s.revision = json_extract({alias}.document, '$.season_prior.revision') "
        f"AND s.scale_dirty = 0 AND s.engine_version = '{ENGINE_VERSION}'))"
    )
