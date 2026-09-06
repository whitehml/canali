"""The record of what happened at events, as FIRST reported it.

Seasons are named by their starting year: 2025 means the 2025-26 season, DECODE.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from warehouse.schema.types import (
    alliance,
    alliance_role,
    component_kind,
    component_level,
    jsonb,
    local,
    match_level,
    metadata,
    utc,
)

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

match = Table(
    "match",
    metadata,
    Column("match_id", UUID(as_uuid=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=False),
    Column("level", match_level, nullable=False),
    Column("series", Integer, nullable=False, server_default=text("0")),
    Column("match_number", Integer, nullable=False),
    Column("description", Text, nullable=True),
    utc("start_time_utc", nullable=True),
    local("start_time_local", nullable=True),
    utc("actual_start_time_utc", nullable=True),
    local("actual_start_time_local", nullable=True),
    utc("post_result_time_utc", nullable=True),
    local("post_result_time_local", nullable=True),
    utc("modified_on_utc", nullable=True),
    Column("score_red_final", Integer, nullable=True),
    Column("score_blue_final", Integer, nullable=True),
    Column("score_red_auto", Integer, nullable=True),
    Column("score_blue_auto", Integer, nullable=True),
    Column("score_red_foul", Integer, nullable=True),
    Column("score_blue_foul", Integer, nullable=True),
    Column("ingested_at_utc", TIMESTAMP(timezone=True), nullable=False),
    UniqueConstraint("event_id", "level", "series", "match_number", name="uq_match_natural_key"),
    schema="core",
    comment=(
        "One row per schedule slot, holding the latest result. We cannot distinguish between a correction and a "
        "replay, so a changed payload overwrites the row in place and the pre-replay result is gone."
    ),
)

match_team = Table(
    "match_team",
    metadata,
    Column("match_id", UUID(as_uuid=True), ForeignKey("core.match.match_id"), primary_key=True),
    Column("station", Text, primary_key=True),  # Red1, Red2, Blue1, Blue2
    Column("alliance", alliance, nullable=False),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), nullable=False),
    Column("surrogate", Boolean, nullable=False, server_default=text("false")),
    Column("no_show", Boolean, nullable=False, server_default=text("false")),
    Column("dq", Boolean, nullable=False, server_default=text("false")),
    Column("on_field", Boolean, nullable=False, server_default=text("true")),
    Column("alliance_role", alliance_role, nullable=True),
    Index("ix_match_team_team_number", "team_number"),
    schema="core",
    comment="The team slot. Surrogate and no-show live here, not on the match.",
)

match_breakdown = Table(
    "match_breakdown",
    metadata,
    Column("match_id", UUID(as_uuid=True), ForeignKey("core.match.match_id"), primary_key=True),
    Column("alliance", alliance, primary_key=True),
    Column("breakdown", jsonb(), nullable=False),
    schema="core",
    comment="Per-alliance component scores, validated in the application against the season's rule pack",
)

ranking = Table(
    "ranking",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("rank", Integer, nullable=False),
    Column("wins", Integer, nullable=True),
    Column("losses", Integer, nullable=True),
    Column("ties", Integer, nullable=True),
    Column("matches_played", Integer, nullable=True),
    Column("qual_average", Float, nullable=True),
    Column("dq", Integer, nullable=True),
    Column("sort_order_1", Float, nullable=True),
    Column("sort_order_2", Float, nullable=True),
    Column("sort_order_3", Float, nullable=True),
    Column("sort_order_4", Float, nullable=True),
    Column("sort_order_5", Float, nullable=True),
    Column("sort_order_6", Float, nullable=True),
    schema="core",
    comment="Official published rankings, one row per event and team.",
)

award = Table(
    "award",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("award_code", Integer, primary_key=True),
    Column("series", Integer, primary_key=True, server_default=text("1")),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), nullable=False),
    Column("source", Text, nullable=False, server_default=text("'ftc_events'")),
    schema="core",
    comment="Judged team awards that were won.",
)

advancement_points = Table(
    "advancement_points",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("points", Float, nullable=False),
    Column("detail", jsonb(), nullable=True),
    schema="core",
    comment=("Official advancement points for completed events. 2025 onward."),
)

advancement_slot = Table(
    "advancement_slot",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("slot", Integer, primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), nullable=True),
    # FIRST or ALREADY_ADVANCING. Only the FIRST rows consume one of the event's slots; ALREADY_ADVANCING are
    # listed for the ordering they occupy.
    Column("status", Text, nullable=True),
    schema="core",
    comment=("The order in which advancement slots were assigned."),
)

event_advancement = Table(
    "event_advancement",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("advances_to", Text, nullable=True),
    Column("slots", Integer, nullable=True),
    Column("slots_first_championship", Integer, nullable=True),
    schema="core",
    comment=("How many teams the event sends and where."),
)

rule_pack = Table(
    "rule_pack",
    metadata,
    Column("season", Integer, ForeignKey("core.season.start_year"), primary_key=True),
    Column("game", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("source_file", Text, nullable=False),
    Column("rp_win", Integer, nullable=True),
    Column("rp_tie", Integer, nullable=True),
    Column("rp_loss", Integer, nullable=True),
    Column("has_bonus_rp", Boolean, nullable=False, server_default=text("false")),
    Column("alliance_size", Integer, nullable=False, server_default=text("2")),
    Column("ranking_formula", Text, nullable=True),
    Column("tiebreakers", jsonb(), nullable=True),
    Column("playoff_structure", Text, nullable=True),
    Column("alliance_brackets", jsonb(), nullable=True),
    Column("playoff_implemented", Boolean, nullable=False, server_default=text("false")),
    Column("advancement_implemented", Boolean, nullable=False, server_default=text("false")),
    Column("loaded_at_utc", TIMESTAMP(timezone=True), nullable=False),
    schema="core",
    comment="Processed from files in this repo manually created off of the game manuals.",
)

rule_pack_component = Table(
    "rule_pack_component",
    metadata,
    Column("season", Integer, primary_key=True),
    Column("name", Text, primary_key=True),
    Column("level", component_level, nullable=False),
    Column("kind", component_kind, nullable=False),
    Column("is_subtotal", Boolean, nullable=False, server_default=text("false")),
    Column("is_derived", Boolean, nullable=False, server_default=text("false")),
    Column("column_name", Text, nullable=False),
    Column("partition_group", Text, nullable=True),
    Column("recovered_from", jsonb(), nullable=True),
    ForeignKeyConstraint(["season"], ["core.rule_pack.season"], name="fk_component_season"),
    UniqueConstraint("season", "column_name", name="uq_component_season_column"),
    schema="core",
    comment="One row per line item a match breakdown reports, and the phase totals those add up to.",
)

__all__ = [
    "advancement_points",
    "advancement_slot",
    "award",
    "event",
    "event_advancement",
    "event_registration",
    "event_team",
    "match",
    "match_breakdown",
    "match_team",
    "ranking",
    "rule_pack",
    "rule_pack_component",
    "season",
    "team",
    "team_season",
]
