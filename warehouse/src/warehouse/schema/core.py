"""The record of what happened at events, as FIRST reported it.

Seasons are named by their starting year: 2025 means the 2025-26 season, DECODE.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from warehouse.schema.types import metadata

season = Table(
    "season",
    metadata,
    Column("start_year", Integer, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False),  # "2025-26"
    Column("game", Text, nullable=False),  # "DECODE"
    Column("rule_pack_version", Text, nullable=True),
    schema="core",
    comment="One row per FTC season, keyed by the year it starts in.",
)

event = Table(
    "event",
    metadata,
    # Event codes may change every year, so the uuid is the foreign-key target; `code` is a season-scoped label.
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("season", Integer, ForeignKey("core.season.start_year"), nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=True),
    Column("division_code", Text, nullable=True),
    Column("region_code", Text, nullable=True),
    Column("league_code", Text, nullable=True),
    Column("field_count", SmallInteger, nullable=True),
    Column("date_start", Date, nullable=False),
    Column("date_end", Date, nullable=True),
    Column("venue", Text, nullable=True),
    Column("city", Text, nullable=True),
    Column("state_prov", Text, nullable=True),
    Column("country", Text, nullable=True),
    Column("timezone", Text, nullable=True),
    Column("timezone_assumed", Boolean, nullable=False, server_default=text("false")),
    UniqueConstraint("season", "code", name="uq_event_season_code"),
    Index("ix_event_season_date_start", "season", "date_start"),
    Index("ix_event_region_code", "season", "region_code"),
    schema="core",
    comment="One row per in-person event. Remote and hybrid events are absent.",
)

team = Table(
    "team",
    metadata,
    Column("team_number", Integer, primary_key=True, autoincrement=False),
    Column("rookie_year", Integer, nullable=True),
    schema="core",
    comment="One row per team number, holding only what does not vary by season. The rest is in team_season.",
)

team_season = Table(
    "team_season",
    metadata,
    Column("season", Integer, ForeignKey("core.season.start_year"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("name_full", Text, nullable=True),
    Column("name_short", Text, nullable=True),
    # Where a team is from. TIMS pass-through, nullable and unvalidated. Not an eligibility field.
    Column("home_state", Text, nullable=True),
    Column("home_country", Text, nullable=True),
    # FIRST's regional assignment and the authoritative eligibility field. A null value is an ineligible
    # out-of-region team.
    Column("home_region", Text, nullable=True),
    Column("city", Text, nullable=True),
    Index("ix_team_season_home_region", "season", "home_region"),
    schema="core",
)

event_team = Table(
    "event_team",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("first_observed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Index("ix_event_team_event_id", "event_id"),
    Index("ix_event_team_team_number_event_id", "team_number", "event_id"),
    schema="core",
    comment=(
        "One row per team that competed at an event, as FTC Events listed them at schedule generation. No-shows "
        "are already excluded. Written once per event."
    ),
)

event_registration = Table(
    "event_registration",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("snapshot_date", Date, primary_key=True),
    Column("season", Integer, ForeignKey("core.season.start_year"), nullable=False),
    Column("event_code", Text, nullable=False),
    Column("last_updated_utc", TIMESTAMP(timezone=True), nullable=False),
    schema="core",
    comment="One row per team per snapshot date, read from registration CSVs loaded by hand.",
)

__all__ = [
    "event",
    "event_registration",
    "event_team",
    "season",
    "team",
    "team_season",
]
