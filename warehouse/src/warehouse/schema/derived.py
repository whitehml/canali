"""Model outputs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from warehouse.schema.types import jsonb, metadata

fit_run = Table(
    "fit_run",
    metadata,
    Column("fit_run_id", UUID(as_uuid=True), primary_key=True),
    Column("model", Text, nullable=False),  # 'epa' | 'pridge'
    Column("model_version", Text, nullable=False),
    Column("prior_version", Text, nullable=True),  # the upstream version read, null for epa
    Column("scope", Text, nullable=False),  # 'event:<uuid>' | 'season:<year>'
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=True),
    Column("season", Integer, nullable=True),
    Column("started_at_utc", TIMESTAMP(timezone=True), nullable=False),
    Column("finished_at_utc", TIMESTAMP(timezone=True), nullable=True),
    Column("notes", jsonb(), nullable=True),
    # Append to the end of what we've already predicted before.
    Index(
        "fit_run_live_uniq",
        "event_id",
        "model",
        "model_version",
        "prior_version",
        unique=True,
        postgresql_where=text("event_id IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    ),
    # A completed batch run replaces the one it supersedes.
    Index(
        "fit_run_batch_uniq",
        "model",
        "season",
        "model_version",
        "prior_version",
        unique=True,
        postgresql_where=text("event_id IS NULL AND finished_at_utc IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    ),
    schema="derived",
)


def _fit_run_fk() -> Column[Any]:
    return Column(
        "fit_run_id",
        UUID(as_uuid=True),
        ForeignKey("derived.fit_run.fit_run_id", ondelete="CASCADE"),
        nullable=False,
    )


team_epa = Table(
    "team_epa",
    metadata,
    _fit_run_fk(),
    Column("season", Integer, nullable=False),
    Column("team_number", Integer, nullable=False),
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=True),
    Column("as_of_match", Integer, nullable=True),
    Column("model_version", Text, nullable=False),
    Column("tag", Text, nullable=False, server_default=text("'match'")),
    Column("epa_norm", Float, nullable=True),
    Column("epa_scaled", Float, nullable=False),
    Column("scale_provisional", Boolean, nullable=False, server_default=text("false")),
    Column("components", jsonb(), nullable=True),
    CheckConstraint("tag IN ('season_start', 'pre_event', 'post_event', 'match')", name="tag"),
    CheckConstraint("(event_id IS NULL) = (as_of_match IS NULL)", name="event_scope"),
    CheckConstraint("(tag = 'season_start') = (event_id IS NULL)", name="season_start_has_no_event"),
    Index(
        "uq_team_epa_row",
        "fit_run_id",
        "event_id",
        "team_number",
        "as_of_match",
        "tag",
        unique=True,
        postgresql_nulls_not_distinct=True,
    ),
    Index("ix_team_epa_latest", "season", "team_number", "model_version", text("as_of_match DESC")),
    schema="derived",
)

team_pridge = Table(
    "team_pridge",
    metadata,
    _fit_run_fk(),
    Column("season", Integer, nullable=False),
    Column("team_number", Integer, nullable=False),
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=True),
    Column("as_of_match", Integer, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("component", Text, nullable=False, server_default=text("'total'")),
    Column("pridge", Float, nullable=False),
    Column("lambda_", Float, nullable=True),
    UniqueConstraint("fit_run_id", "event_id", "team_number", "component", "as_of_match", name="uq_team_pridge_row"),
    Index("ix_team_pridge_latest", "season", "team_number", "model_version", text("as_of_match DESC")),
    schema="derived",
)

team_event_opr = Table(
    "team_event_opr",
    metadata,
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), primary_key=True),
    Column("team_number", Integer, ForeignKey("core.team.team_number"), primary_key=True),
    Column("opr_total", Float, nullable=True),
    Column("opr_total_np", Float, nullable=True),
    Column("opr_auto", Float, nullable=True),
    Column("opr_teleop", Float, nullable=True),
    Column("computed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    schema="derived",
)

event_sim_run = Table(
    "event_sim_run",
    metadata,
    Column("sim_run_id", UUID(as_uuid=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("core.event.event_id"), nullable=False),
    Column("epa_version", Text, nullable=False),
    Column("sim_version", Text, nullable=False),
    Column("as_of_match", Integer, nullable=False),
    Column("n_iterations", Integer, nullable=False),
    Column("achieved_mcse", Float, nullable=False),
    Column("computed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    UniqueConstraint("event_id", "epa_version", "sim_version", "as_of_match", name="uq_event_sim_run_key"),
    schema="derived",
)

event_sim_result = Table(
    "event_sim_result",
    metadata,
    Column(
        "sim_run_id",
        UUID(as_uuid=True),
        ForeignKey("derived.event_sim_run.sim_run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("team_number", Integer, primary_key=True),
    Column("p_attend", Float, nullable=False),
    Column("mean_rank", Float, nullable=True),
    Column("rank_probabilities", jsonb(), nullable=True),
    Column("p_captain", Float, nullable=False),
    Column("p_selected", Float, nullable=False),
    Column("p_win", Float, nullable=False),
    Column("p_advance", Float, nullable=True),
    Column("p_advance_first_championship", Float, nullable=True),
    CheckConstraint(
        "p_attend BETWEEN 0 AND 1 AND p_captain BETWEEN 0 AND 1 AND p_selected BETWEEN 0 AND 1"
        " AND p_win BETWEEN 0 AND 1 AND p_advance BETWEEN 0 AND 1"
        " AND p_advance_first_championship BETWEEN 0 AND p_advance",
        name="probabilities",
    ),
    CheckConstraint("mean_rank >= 1", name="mean_rank"),
    schema="derived",
)

season_sim_run = Table(
    "season_sim_run",
    metadata,
    Column("sim_run_id", UUID(as_uuid=True), primary_key=True),
    Column("season", Integer, nullable=False),
    Column("region_code", Text, nullable=False),
    Column("epa_version", Text, nullable=False),
    Column("sim_version", Text, nullable=False),
    Column("n_iterations", Integer, nullable=False),
    Column("achieved_mcse", Float, nullable=False),
    Column("computed_at_utc", TIMESTAMP(timezone=True), nullable=False),
    UniqueConstraint("season", "region_code", "epa_version", "sim_version", name="uq_season_sim_run_key"),
    schema="derived",
)

season_sim_result = Table(
    "season_sim_result",
    metadata,
    Column(
        "sim_run_id",
        UUID(as_uuid=True),
        ForeignKey("derived.season_sim_run.sim_run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("team_number", Integer, primary_key=True),
    Column("p_attend_rcmp", Float, nullable=False),
    Column("p_premier", Float, nullable=False),
    Column("p_cmp", Float, nullable=False),
    CheckConstraint(
        "p_attend_rcmp BETWEEN 0 AND 1 AND p_premier BETWEEN 0 AND 1 AND p_cmp BETWEEN 0 AND 1",
        name="probabilities",
    ),
    schema="derived",
)
