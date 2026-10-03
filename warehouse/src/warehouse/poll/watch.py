"""The events the poller follows in the latest season, by region or by name."""

from __future__ import annotations

import datetime as dt
import tomllib
import uuid
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, and_, func, or_, select

from warehouse.schema import core
from warehouse.tier import RATED_EVENT_TYPES

WATCH_FILE = Path(__file__).resolve().parents[3] / "config" / "watch.toml"


class WatchedEvent(NamedTuple):
    event_id: uuid.UUID
    code: str
    type: str | None
    date_start: dt.date
    date_end: dt.date | None
    timezone: str | None


class WatchList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path = WATCH_FILE) -> WatchList:
        with path.open("rb") as fh:
            return cls.model_validate(tomllib.load(fh))

    def resolve(self, conn: Connection) -> list[WatchedEvent]:
        """The watched events of the latest season in ``core.season``, in date order."""
        e = core.event.c
        rows = conn.execute(
            select(e.event_id, e.code, e.type, e.date_start, e.date_end, e.timezone)
            .where(
                e.season == select(func.max(core.season.c.season)).scalar_subquery(),
                or_(and_(e.region_code.in_(self.regions), e.type.in_(RATED_EVENT_TYPES)), e.code.in_(self.events)),
            )
            .order_by(e.date_start, e.code)
        )
        return [
            WatchedEvent(uuid.UUID(str(r.event_id)), r.code, r.type, r.date_start, r.date_end, r.timezone) for r in rows
        ]
