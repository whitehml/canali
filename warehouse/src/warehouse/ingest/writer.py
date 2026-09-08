"""Fact-base writes, and the rules that govern revisions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Connection, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from warehouse.ingest import transforms
from warehouse.schema import core
from warehouse.schema.raw import ingest_conflict, ingest_diff

log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


_MAX_BIND_PARAMS = 60_000

_ROLE_CHUNK = _MAX_BIND_PARAMS // 2


def _chunks(rows: Sequence[Mapping[str, Any]], columns: int) -> Iterator[Sequence[Mapping[str, Any]]]:
    size = max(1, _MAX_BIND_PARAMS // max(columns, 1))
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# ------------------------------------------------------------------------------------------------------- logging


def log_conflict(
    conn: Connection,
    *,
    table_name: str,
    key: Mapping[str, Any],
    kind: str,
    stored: Any = None,
    incoming: Any = None,
    source: str = "ftc_events",
    payload_hash: str | None = None,
) -> None:
    conn.execute(
        insert(ingest_conflict).values(
            ingest_conflict_id=uuid.uuid4(),
            observed_at_utc=_now(),
            table_name=table_name,
            key=_jsonable(key),
            kind=kind,
            stored=_jsonable(stored),
            incoming=_jsonable(incoming),
            source=source,
            payload_hash=payload_hash,
        )
    )


def log_diff(
    conn: Connection,
    *,
    table_name: str,
    key: Mapping[str, Any],
    before: Any,
    after: Any,
    payload_hash: str | None = None,
) -> None:
    conn.execute(
        insert(ingest_diff).values(
            ingest_diff_id=uuid.uuid4(),
            observed_at_utc=_now(),
            table_name=table_name,
            key=_jsonable(key),
            before=_jsonable(before),
            after=_jsonable(after),
            payload_hash=payload_hash,
        )
    )


# ------------------------------------------------------------------------------------------------------- upserts


def upsert_seasons(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(core.season).values(list(rows))
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["season"],
            set_={"name": stmt.excluded.name, "game": stmt.excluded.game},
        )
    )
    return len(rows)


def upsert_events(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    for chunk in _chunks(rows, len(core.event.columns)):
        stmt = insert(core.event).values(list(chunk))
        updatable = {c.name: stmt.excluded[c.name] for c in core.event.columns if c.name not in ("event_id", "season")}
        conn.execute(stmt.on_conflict_do_update(index_elements=["event_id"], set_=updatable))
    return len(rows)


def ensure_teams(conn: Connection, team_numbers: Iterable[int]) -> int:
    """Insert an identity row if absent, touching nothing already there."""
    numbers = sorted(set(team_numbers))
    if not numbers:
        return 0
    rows = [{"team_number": n} for n in numbers]
    for chunk in _chunks(rows, 1):
        stmt = insert(core.team).values(list(chunk))
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["team_number"]))
    return len(numbers)


def upsert_teams(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(core.team).values(list(rows))
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["team_number"],
            set_={"rookie_year": func.coalesce(stmt.excluded.rookie_year, core.team.c.rookie_year)},
        )
    )
    interpolate_rookie_years(conn)
    return len(rows)


def interpolate_rookie_years(conn: Connection) -> int:
    filled = conn.execute(
        text(
            """
            UPDATE core.team t
            SET rookie_year = (
                SELECT k.rookie_year
                FROM core.team k
                WHERE k.rookie_year IS NOT NULL
                ORDER BY abs(k.team_number - t.team_number), k.team_number
                LIMIT 1
            )
            WHERE t.rookie_year IS NULL
            """
        )
    ).rowcount
    return int(filled)


def upsert_team_seasons(conn: Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_teams(conn, (r["team_number"] for r in rows))
    for chunk in _chunks(rows, len(core.team_season.columns)):
        stmt = insert(core.team_season).values(list(chunk))
        updatable = {
            c.name: stmt.excluded[c.name] for c in core.team_season.columns if c.name not in ("season", "team_number")
        }
        conn.execute(stmt.on_conflict_do_update(index_elements=["season", "team_number"], set_=updatable))
    return len(rows)


def write_event_teams(
    conn: Connection,
    event_id: uuid.UUID,
    team_numbers: Sequence[int],
    *,
    payload_hash: str | None = None,
) -> int:
    """Write once.

    FTC Events publishes the team list at schedule generation, already reflecting no-shows. The first non-empty
    observation is inserted and never updated, and a later pull that disagrees is logged as a conflict.
    """
    if not team_numbers:
        return 0

    stored = set(
        conn.execute(select(core.event_team.c.team_number).where(core.event_team.c.event_id == event_id)).scalars()
    )
    incoming = set(team_numbers)

    if stored:
        if stored != incoming:
            log_conflict(
                conn,
                table_name="core.event_team",
                key={"event_id": str(event_id)},
                kind="write_once_disagreement",
                stored=sorted(stored),
                incoming=sorted(incoming),
                payload_hash=payload_hash,
            )
            log.warning(
                "event_team.conflict",
                event_id=str(event_id),
                added=sorted(incoming - stored),
                removed=sorted(stored - incoming),
            )
        return 0

    ensure_teams(conn, incoming)
    conn.execute(
        insert(core.event_team).values(
            [
                {"event_id": event_id, "team_number": number, "first_observed_at_utc": _now()}
                for number in sorted(incoming)
            ]
        )
    )
    return len(incoming)


# ------------------------------------------------------------------------------------------------------- matches


def _stored_match(conn: Connection, event_id: uuid.UUID, level: str, series: int, number: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(core.match).where(
                core.match.c.event_id == event_id,
                core.match.c.level == level,
                core.match.c.series == series,
                core.match.c.match_number == number,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    stored = dict(row)
    stored["teams"] = [
        dict(t)
        for t in conn.execute(
            select(
                core.match_team.c.station,
                core.match_team.c.team_number,
                core.match_team.c.surrogate,
                core.match_team.c.no_show,
                core.match_team.c.dq,
            ).where(core.match_team.c.match_id == row["match_id"])
        ).mappings()
    ]
    return stored


def update_alliance_roles(
    conn: Connection,
    event_id: uuid.UUID,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    totals = {"updated": 0, "unmatched": 0, "disagreed": 0}
    if not rows:
        return totals

    stored = {
        (r["level"], r["series"], r["match_number"], r["station"]): r
        for r in conn.execute(
            select(
                core.match.c.level,
                core.match.c.series,
                core.match.c.match_number,
                core.match_team.c.station,
                core.match_team.c.match_id,
                core.match_team.c.team_number,
                core.match_team.c.alliance_role,
            )
            .select_from(core.match_team.join(core.match, core.match.c.match_id == core.match_team.c.match_id))
            .where(core.match.c.event_id == event_id)
        )
        .mappings()
        .all()
    }

    pending: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for row in rows:
        slot = stored.get((row["level"], row["series"], row["match_number"], row["station"]))
        if slot is None:
            totals["unmatched"] += 1
            continue
        if slot["team_number"] != row["team_number"] or (
            slot["alliance_role"] is not None and slot["alliance_role"] != row["alliance_role"]
        ):
            totals["disagreed"] += 1
            continue
        if slot["alliance_role"] == row["alliance_role"]:
            continue
        pending.setdefault(row["alliance_role"], []).append((slot["match_id"], row["station"]))

    for role, keys in pending.items():
        for start in range(0, len(keys), _ROLE_CHUNK):
            chunk = keys[start : start + _ROLE_CHUNK]
            conn.execute(
                core.match_team.update()
                .where(tuple_(core.match_team.c.match_id, core.match_team.c.station).in_(chunk))
                .values(alliance_role=role)
            )
            totals["updated"] += len(chunk)
    return totals


def write_matches_non_authoritative(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str = "ftcscout",
    payload_hash: str | None = None,
) -> tuple[int, int]:
    """Write matches from a supplier that does not outrank FTC Events, returning written and conflict counts.

    An empty slot is filled; an occupied slot stands and the disagreement is logged with both readings.
    """
    written = 0
    conflicts = 0

    for row in rows:
        teams = list(row.get("teams") or [])
        stored = _stored_match(conn, row["event_id"], row["level"], row["series"], row["match_number"])
        payload = {k: v for k, v in row.items() if k != "teams"}

        if stored is None:
            _insert_match(conn, payload, teams)
            written += 1
            continue

        if transforms.results_differ(stored, {**payload, "teams": teams}):
            conflicts += 1
            log_conflict(
                conn,
                table_name="core.match",
                key={
                    "event_id": str(row["event_id"]),
                    "level": row["level"],
                    "series": row["series"],
                    "match_number": row["match_number"],
                },
                kind="source_disagreement",
                stored={f: stored.get(f) for f in transforms.RESULT_FIELDS},
                incoming={f: payload.get(f) for f in transforms.RESULT_FIELDS},
                source=source,
                payload_hash=payload_hash,
            )

    return written, conflicts


def write_matches(
    conn: Connection,
    rows: Sequence[Mapping[str, Any]],
    *,
    payload_hash: str | None = None,
) -> tuple[int, int]:
    """Write matches, overwriting a slot in place when its results changed, and return written and replay counts.

    A replay arrives as the same level, series and match number with changed scores. The stored row is overwritten
    and the before and after logged.
    """
    written = 0
    replays = 0

    for row in rows:
        teams = list(row.get("teams") or [])
        stored = _stored_match(conn, row["event_id"], row["level"], row["series"], row["match_number"])
        payload = {k: v for k, v in row.items() if k != "teams"}

        if stored is None:
            _insert_match(conn, payload, teams)
            written += 1
            continue

        if not transforms.results_differ(stored, {**payload, "teams": teams}):
            conn.execute(
                core.match.update()
                .where(core.match.c.match_id == stored["match_id"])
                .values(
                    modified_on_utc=payload.get("modified_on_utc"),
                    actual_start_time_utc=payload.get("actual_start_time_utc"),
                    actual_start_time_local=payload.get("actual_start_time_local"),
                    post_result_time_utc=payload.get("post_result_time_utc"),
                    post_result_time_local=payload.get("post_result_time_local"),
                )
            )
            continue

        log_diff(
            conn,
            table_name="core.match",
            key={
                "event_id": str(row["event_id"]),
                "level": row["level"],
                "series": row["series"],
                "match_number": row["match_number"],
            },
            before={f: stored.get(f) for f in transforms.RESULT_FIELDS},
            after={f: payload.get(f) for f in transforms.RESULT_FIELDS},
            payload_hash=payload_hash,
        )
        _overwrite_match(conn, uuid.UUID(str(stored["match_id"])), payload, teams)
        written += 1
        replays += 1

    return written, replays


def _insert_match(conn: Connection, payload: Mapping[str, Any], teams: Sequence[Mapping[str, Any]]) -> uuid.UUID:
    match_id = uuid.uuid4()
    ensure_teams(conn, (t["team_number"] for t in teams))
    conn.execute(insert(core.match).values(match_id=match_id, ingested_at_utc=_now(), **dict(payload)))
    if teams:
        conn.execute(insert(core.match_team).values([{"match_id": match_id, **dict(t)} for t in teams]))
    return match_id


def _overwrite_match(
    conn: Connection,
    match_id: uuid.UUID,
    payload: Mapping[str, Any],
    teams: Sequence[Mapping[str, Any]],
) -> None:
    """Replace a slot's stored results and team assignments in place."""
    ensure_teams(conn, (t["team_number"] for t in teams))
    conn.execute(core.match.update().where(core.match.c.match_id == match_id).values(ingested_at_utc=_now(), **payload))
    conn.execute(core.match_team.delete().where(core.match_team.c.match_id == match_id))
    if teams:
        conn.execute(insert(core.match_team).values([{"match_id": match_id, **dict(t)} for t in teams]))


def write_breakdowns(
    conn: Connection,
    event_id: uuid.UUID,
    breakdowns: Sequence[tuple[str, int, int, str, Mapping[str, Any]]],
) -> int:
    """Attach score details to the one match in each slot."""
    written = 0
    for level, series, number, side, breakdown in breakdowns:
        match_id = conn.execute(
            select(core.match.c.match_id).where(
                core.match.c.event_id == event_id,
                core.match.c.level == level,
                core.match.c.series == series,
                core.match.c.match_number == number,
            )
        ).scalar()
        if match_id is None:
            continue
        stmt = insert(core.match_breakdown).values(match_id=match_id, alliance=side, breakdown=dict(breakdown))
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["match_id", "alliance"],
                set_={"breakdown": stmt.excluded.breakdown},
            )
        )
        written += 1
    return written


# ------------------------------------------------------------------------------------------------------- awards

_AWARD_KEY = ["event_id", "award_code", "series"]


def write_awards(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    """Awards from FTC Events, which outranks every other supplier."""
    if not rows:
        return 0

    ensure_teams(conn, (r["team_number"] for r in rows))
    incoming = sorted({(r["award_code"], r["series"]) for r in rows})
    conn.execute(
        core.award.delete().where(
            core.award.c.event_id == event_id,
            core.award.c.source.in_(sorted({r["source"] for r in rows})),
            tuple_(core.award.c.award_code, core.award.c.series).notin_(incoming),
        )
    )
    for chunk in _chunks([dict(r) for r in rows], len(core.award.columns)):
        stmt = insert(core.award).values(list(chunk))
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=_AWARD_KEY,
                set_={"team_number": stmt.excluded.team_number, "source": stmt.excluded.source},
            )
        )
    return len(rows)


def write_awards_non_authoritative(
    conn: Connection,
    event_id: uuid.UUID,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str = "ftcscout",
    payload_hash: str | None = None,
) -> tuple[int, int]:
    """Awards from a supplier that does not outrank FTC Events, returning written and conflict counts."""
    if not rows:
        return 0, 0

    stored = {
        (r["award_code"], r["series"]): r["team_number"]
        for r in conn.execute(
            select(core.award.c.award_code, core.award.c.series, core.award.c.team_number).where(
                core.award.c.event_id == event_id
            )
        ).mappings()
    }

    fresh = [r for r in rows if (r["award_code"], r["series"]) not in stored]
    conflicts = 0
    for row in rows:
        held = stored.get((row["award_code"], row["series"]))
        if held is None or held == row["team_number"]:
            continue
        conflicts += 1
        log_conflict(
            conn,
            table_name="core.award",
            key={"event_id": str(event_id), "award_code": row["award_code"], "series": row["series"]},
            kind="source_disagreement",
            stored={"team_number": held},
            incoming={"team_number": row["team_number"]},
            source=source,
            payload_hash=payload_hash,
        )

    if fresh:
        ensure_teams(conn, (r["team_number"] for r in fresh))
        for chunk in _chunks([dict(r) for r in fresh], len(core.award.columns)):
            conn.execute(insert(core.award).values(list(chunk)).on_conflict_do_nothing(index_elements=_AWARD_KEY))
    return len(fresh), conflicts


# ---------------------------------------------------------------------------------------------------- alliances


def write_playoff_alliances(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    """The seated alliances, replaced whole."""
    seated = {team for r in rows for team in (r[slot] for slot in transforms.ALLIANCE_SLOTS) if team is not None}
    return _replace_alliance(conn, core.playoff_alliance, event_id, rows, seated)


def write_alliance_picks(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    """The pick log, each seated pick carrying the alliance the team ended up on."""
    seats = {
        team: number
        for number, *slots in conn.execute(
            select(
                core.playoff_alliance.c.alliance_number,
                *(core.playoff_alliance.c[slot] for slot in transforms.ALLIANCE_SLOTS),
            ).where(core.playoff_alliance.c.event_id == event_id)
        )
        for team in slots
        if team is not None
    }
    numbered = [
        {
            **dict(r),
            "alliance_number": seats.get(r["team_number"]) if r["action"] in transforms.SEATING_ACTIONS else None,
        }
        for r in rows
    ]
    return _replace_alliance(conn, core.playoff_alliance_pick, event_id, numbered, {r["team_number"] for r in rows})


def _replace_alliance(
    conn: Connection,
    table: Any,
    event_id: uuid.UUID,
    rows: Sequence[Mapping[str, Any]],
    team_numbers: Iterable[int],
) -> int:
    if not rows:
        return 0
    ensure_teams(conn, team_numbers)
    conn.execute(table.delete().where(table.c.event_id == event_id))
    stamped = [{**dict(r), "ingested_at_utc": _now()} for r in rows]
    for chunk in _chunks(stamped, len(table.columns)):
        conn.execute(insert(table).values(list(chunk)))
    return len(rows)


# ------------------------------------------------------------------------------------------- simple replacements


def _replace(conn: Connection, table: Any, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    """Delete and reinsert, for endpoints that publish a complete list each time."""
    if not rows:
        return 0
    numbers = {r["team_number"] for r in rows if r.get("team_number") is not None}
    ensure_teams(conn, numbers)
    conn.execute(table.delete().where(table.c.event_id == event_id))
    for chunk in _chunks([dict(r) for r in rows], len(table.columns)):
        conn.execute(insert(table).values(list(chunk)))
    return len(rows)


def write_rankings(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    return _replace(conn, core.ranking, event_id, rows)


def write_advancement_points(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    """Official points for a completed event."""
    return _replace(conn, core.advancement_points, event_id, rows)


def write_advancement_slots(conn: Connection, event_id: uuid.UUID, rows: Sequence[Mapping[str, Any]]) -> int:
    return _replace(conn, core.advancement_slot, event_id, rows)


def write_event_advancement(conn: Connection, row: Mapping[str, Any] | None) -> int:
    if row is None:
        return 0
    stmt = insert(core.event_advancement).values(dict(row))
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={c.name: stmt.excluded[c.name] for c in core.event_advancement.columns if c.name != "event_id"},
        )
    )
    return 1
