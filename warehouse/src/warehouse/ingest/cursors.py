"""``raw.ingest_run``: the cursor store.

One row per (scope, endpoint), holding the ``Last-Modified`` string to hand back on the next request. Season scope
covers the endpoints that name no event, ``/{season}/events`` and ``/{season}/teams``.

A fetch lands one of three ways. A 304, or a 200 whose bytes are the ones already stored, only moves
``last_checked_at_utc``. A 200 carrying bytes the store has not seen opens a run. An empty 200 carries no
``Last-Modified``, so there is nothing to remember: the attempt is counted and the next pass asks again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Connection, and_, select, update
from sqlalchemy.dialects.postgresql import insert

from warehouse.ingest.client import ApiResponse
from warehouse.schema.raw import ingest_run


@dataclass(frozen=True, slots=True)
class CursorKey:
    """Which cursor a fetch belongs to."""

    endpoint: str
    season: int
    event_id: uuid.UUID | None = None

    @property
    def scope(self) -> str:
        return "event" if self.event_id is not None else "season"


@dataclass(frozen=True, slots=True)
class Cursor:
    """A stored cursor as it is read back."""

    ingest_run_id: uuid.UUID
    last_modified: str | None
    payload_hash: str | None
    run_count: int
    notes: dict[str, Any] | None = None

    @property
    def scope_token(self) -> str:
        return (self.notes or {}).get("scope_token") or ""


def _match(key: CursorKey) -> ColumnElement[bool]:
    if key.event_id is not None:
        return and_(ingest_run.c.event_id == key.event_id, ingest_run.c.endpoint == key.endpoint)
    return and_(
        ingest_run.c.event_id.is_(None),
        ingest_run.c.season == key.season,
        ingest_run.c.endpoint == key.endpoint,
    )


def read(conn: Connection, key: CursorKey) -> Cursor | None:
    row = conn.execute(
        select(
            ingest_run.c.ingest_run_id,
            ingest_run.c.last_modified,
            ingest_run.c.payload_hash,
            ingest_run.c.run_count,
            ingest_run.c.notes,
        ).where(_match(key))
    ).first()
    return Cursor(*row) if row else None


def _create(conn: Connection, key: CursorKey, now: datetime) -> Cursor:
    ingest_run_id = uuid.uuid4()
    conn.execute(
        insert(ingest_run).values(
            ingest_run_id=ingest_run_id,
            scope=key.scope,
            season=key.season,
            event_id=key.event_id,
            endpoint=key.endpoint,
            last_checked_at_utc=now,
            run_count=0,
            check_count=0,
            empty_count=0,
        )
    )
    return Cursor(ingest_run_id, None, None, 0, None)


def observe(
    conn: Connection,
    key: CursorKey,
    response: ApiResponse,
    *,
    payload_hash: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Fold one fetch into its cursor, returning whether a run opened."""
    now = now or datetime.now(UTC)
    current = read(conn, key) or _create(conn, key, now)

    values: dict[str, Any] = {
        "last_checked_at_utc": now,
        "last_status": response.status,
        "check_count": ingest_run.c.check_count + 1,
    }

    opened = False
    if response.outcome == "empty":
        values["empty_count"] = ingest_run.c.empty_count + 1
    elif response.outcome == "changed" and payload_hash != current.payload_hash:
        values |= {
            "last_modified": response.last_modified,
            "payload_hash": payload_hash,
            "last_changed_at_utc": now,
            "run_count": ingest_run.c.run_count + 1,
            "rows_written": None,
        }
        opened = True
    elif response.outcome == "changed":
        values["last_modified"] = response.last_modified

    conn.execute(update(ingest_run).where(ingest_run.c.ingest_run_id == current.ingest_run_id).values(**values))
    return opened


def set_rows_written(conn: Connection, key: CursorKey, rows: int, scope_token: str = "") -> None:
    """Record how many rows the transform produced, and the filter it produced them under."""
    conn.execute(update(ingest_run).where(_match(key)).values(rows_written=rows, notes={"scope_token": scope_token}))
