"""The ingest layer.

``ingest_run`` remembers the timestamp FIRST last sent for each endpoint. Handed back unchanged, it asks for data only
if something changed; handed back altered, FIRST ignores it without complaint and resends everything, every time. An
empty response carries no timestamp, so there is nothing to remember: ``empty_count`` records the attempt and the next
pass tries again.

See the Last-Modified notes at https://ftc-events.firstinspires.org/api-docs.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from warehouse.schema.types import ingest_scope, jsonb, metadata

raw_payload = Table(
    "raw_payload",
    metadata,
    Column("payload_hash", Text, primary_key=True),
    Column("path", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("fetched_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Column("last_modified", Text, nullable=True),
    Column("byte_length", Integer, nullable=False),
    Index("ix_raw_payload_endpoint_fetched", "endpoint", "fetched_at_utc"),
    schema="raw",
    comment="Bodies live on disk, content-addressed and gzipped, reachable only through Warehouse tooling.",
)

ingest_run = Table(
    "ingest_run",
    metadata,
    Column("ingest_run_id", UUID(as_uuid=True), primary_key=True),
    Column("scope", ingest_scope, nullable=False),
    Column("season", Integer, nullable=False),
    Column("event_id", UUID(as_uuid=True), nullable=True),
    Column("endpoint", Text, nullable=False),
    Column("last_modified", Text, nullable=True),
    Column("payload_hash", Text, ForeignKey("raw.raw_payload.payload_hash"), nullable=True),
    Column("last_checked_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Column("last_changed_at_utc", TIMESTAMP(timezone=True), nullable=True),
    Column("last_status", Integer, nullable=True),
    Column("run_count", Integer, nullable=False, server_default=text("0")),
    Column("check_count", Integer, nullable=False, server_default=text("0")),
    Column("empty_count", Integer, nullable=False, server_default=text("0")),
    Column("rows_written", Integer, nullable=True),
    Column("notes", jsonb(), nullable=True),
    CheckConstraint(
        "(scope = 'season' AND event_id IS NULL) OR (scope = 'event' AND event_id IS NOT NULL)",
        name="scope_matches_event_id",
    ),
    # Two partial indexes, not one constraint: Postgres treats every NULL as distinct, so one unique constraint over
    # a nullable event_id would permit duplicate season-scope cursors.
    Index(
        "ingest_cursor_season",
        "season",
        "endpoint",
        unique=True,
        postgresql_where=text("event_id IS NULL"),
    ),
    Index(
        "ingest_cursor_event",
        "event_id",
        "endpoint",
        unique=True,
        postgresql_where=text("event_id IS NOT NULL"),
    ),
    schema="raw",
    comment=(
        "One row per (scope, endpoint), holding the timestamp to hand back on the next request. A reply saying "
        "nothing changed only moves last_checked_at_utc; a reply carrying data this table has not seen before "
        "increments run_count."
    ),
)

ingest_conflict = Table(
    "ingest_conflict",
    metadata,
    Column("ingest_conflict_id", UUID(as_uuid=True), primary_key=True),
    Column("observed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Column("table_name", Text, nullable=False),
    Column("key", jsonb(), nullable=False),
    Column("kind", Text, nullable=False),
    Column("stored", jsonb(), nullable=True),
    Column("incoming", jsonb(), nullable=True),
    Column("source", Text, nullable=True),
    Column("payload_hash", Text, nullable=True),
    Index("ix_ingest_conflict_table_observed", "table_name", "observed_at_utc"),
    schema="raw",
    comment=(
        "Conflicts between sources are recorded, not resolved. The FTC Events value is the more trusted and is "
        "the one that lands in the fact table; both readings are kept here."
    ),
)

ingest_diff = Table(
    "ingest_diff",
    metadata,
    Column("ingest_diff_id", UUID(as_uuid=True), primary_key=True),
    Column("observed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Column("table_name", Text, nullable=False),
    Column("key", jsonb(), nullable=False),
    Column("before", jsonb(), nullable=True),
    Column("after", jsonb(), nullable=True),
    Column("payload_hash", Text, nullable=True),
    Index("ix_ingest_diff_table_observed", "table_name", "observed_at_utc"),
    schema="raw",
    comment="What a row held before an upstream change overwrote it. The fact tables keep current values only.",
)

match_signal = Table(
    "match_signal",
    metadata,
    Column("signal_id", BigInteger, Identity(), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=False),
    Column("match_id", UUID(as_uuid=True), ForeignKey("core.match.match_id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("observed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint("kind IN ('scored', 'replayed')", name="kind"),
    schema="raw",
    comment=(
        "Each match of a watched event the live poller saw scored or replayed, written with the match itself. "
        "Read in signal_id order."
    ),
)
