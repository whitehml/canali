"""Typed accessors over the `pub` views.

Model repositories read through this package and write no SQL by hand. Rows come back as frozen dataclasses.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from warehouse.config import Settings, load_settings
from warehouse.db import make_engine

log = structlog.get_logger(__name__)

_CHUNK = 5000


@dataclass(frozen=True, slots=True)
class EventRow:
    event_id: uuid.UUID
    season: int
    event_code: str
    date_start: date | None
    event_ordinal: int
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class MatchRow:
    """One alliance's view of one match, the rating models' unit of input."""

    match_id: uuid.UUID
    event_id: uuid.UUID
    season: int
    event_code: str
    event_type: str | None
    event_date_start: date | None
    level: str
    series: int
    match_number: int
    event_match_ordinal: int
    alliance: str
    score: float
    opponent_score: float
    score_auto: float
    score_no_foul: float
    team_numbers: tuple[int, ...]
    surrogates: tuple[bool, ...]
    no_shows: tuple[bool, ...]
    dqs: tuple[bool, ...]

    def rated_teams(self) -> tuple[int, ...]:
        """Teams whose rating this match updates: surrogates in, no-shows out."""
        return tuple(team for team, absent in zip(self.team_numbers, self.no_shows, strict=True) if not absent)


@dataclass(frozen=True, slots=True)
class OprRow:
    event_id: uuid.UUID
    season: int
    event_code: str
    event_ordinal: int
    team_number: int
    opr_total: float | None
    opr_total_np: float | None
    opr_auto: float | None
    opr_teleop: float | None


@dataclass(frozen=True, slots=True)
class TeamPridgeRow:
    """One pRidge rating, at one `as_of_match`, for one component."""

    fit_run_id: uuid.UUID
    season: int
    team_number: int
    event_id: uuid.UUID | None
    as_of_match: int
    model_version: str
    component: str
    pridge: float
    lambda_: float | None


@dataclass(frozen=True, slots=True)
class PreEventEpaRow:
    """Beta-zero: what a team carried into an event."""

    season: int
    event_id: uuid.UUID
    event_ordinal: int
    team_number: int
    model_version: str
    epa_scaled: float
    epa_norm: float | None
    components: dict[str, float]
    scale_provisional: bool


def _tuple(value: Any) -> tuple[Any, ...]:
    return tuple(value) if value is not None else ()


def _match_row(r: Any) -> MatchRow:
    return MatchRow(
        match_id=uuid.UUID(str(r["match_id"])),
        event_id=uuid.UUID(str(r["event_id"])),
        season=r["season"],
        event_code=r["event_code"],
        event_type=r["event_type"],
        event_date_start=r["event_date_start"],
        level=r["level"],
        series=r["series"],
        match_number=r["match_number"],
        event_match_ordinal=r["event_match_ordinal"],
        alliance=r["alliance"],
        score=float(r["score"] or 0.0),
        opponent_score=float(r["opponent_score"] or 0.0),
        score_auto=float(r["score_auto"] or 0.0),
        score_no_foul=float(r["score_no_foul"] or 0.0),
        team_numbers=_tuple(r["team_numbers"]),
        surrogates=_tuple(r["surrogates"]),
        no_shows=_tuple(r["no_shows"]),
        dqs=_tuple(r["dqs"]),
    )


class Warehouse:
    """Typed reads and model writes against one database."""

    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self._settings = settings or load_settings()
        self._engine = engine or make_engine(self._settings)

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------------- ordering

    def events(self, season: int) -> list[EventRow]:
        """A season's events in `pub.v_event_sequence` order."""
        sql = text(
            """
            SELECT s.event_id, s.season, s.event_code, s.date_start, s.event_ordinal, e.type AS event_type
            FROM pub.v_event_sequence s
            JOIN core.event e ON e.event_id = s.event_id
            WHERE s.season = :season
            ORDER BY s.event_ordinal
            """
        )
        with self._engine.connect() as conn:
            return [
                EventRow(
                    uuid.UUID(str(r.event_id)),
                    r.season,
                    r.event_code,
                    r.date_start,
                    r.event_ordinal,
                    r.event_type,
                )
                for r in conn.execute(sql, {"season": season})
            ]

    # --------------------------------------------------------------------------------------------------- inputs

    def event_matches(self, event_id: uuid.UUID, *, levels: Sequence[str] = ("QUALIFICATION",)) -> list[MatchRow]:
        """One event's matches, in canonical ordinal order."""
        sql = text(
            """
            SELECT * FROM pub.v_match_rating_input
            WHERE event_id = :event AND level = ANY(:levels)
            ORDER BY event_match_ordinal, alliance
            """
        )
        with self._engine.connect() as conn:
            return [_match_row(r) for r in conn.execute(sql, {"event": event_id, "levels": list(levels)}).mappings()]

    def season_matches(self, season: int, *, levels: Sequence[str] = ("QUALIFICATION",)) -> list[MatchRow]:
        """Every rated match of a season, in event-sequential order."""
        sql = text(
            """
            SELECT m.* FROM pub.v_match_rating_input m
            JOIN pub.v_event_sequence s ON s.event_id = m.event_id
            WHERE m.season = :season AND m.level = ANY(:levels)
            ORDER BY s.event_ordinal, m.event_match_ordinal, m.alliance
            """
        )
        with self._engine.connect() as conn:
            return [_match_row(r) for r in conn.execute(sql, {"season": season, "levels": list(levels)}).mappings()]

    def opr(self, season: int) -> list[OprRow]:
        """The OPR baseline both models are measured against."""
        sql = text(
            """
            SELECT event_id, season, event_code, event_ordinal, team_number,
                   opr_total, opr_total_np, opr_auto, opr_teleop
            FROM pub.v_opr WHERE season = :season ORDER BY event_ordinal, team_number
            """
        )
        with self._engine.connect() as conn:
            return [
                OprRow(
                    uuid.UUID(str(r.event_id)),
                    r.season,
                    r.event_code,
                    r.event_ordinal,
                    r.team_number,
                    r.opr_total,
                    r.opr_total_np,
                    r.opr_auto,
                    r.opr_teleop,
                )
                for r in conn.execute(sql, {"season": season})
            ]

    def component_columns(self, season: int) -> frozenset[str]:
        """Every `column_name` the season's rule pack declares."""
        sql = text("SELECT column_name FROM pub.v_rule_pack_component WHERE season = :season")
        with self._engine.connect() as conn:
            return frozenset(r[0] for r in conn.execute(sql, {"season": season}))

    def fittable_components(self, season: int) -> list[dict[str, Any]]:
        """The season's fittable component declarations, as mappings so no model hardcodes a component name."""
        sql = text(
            """
            SELECT name, column_name, level, kind, is_subtotal, is_derived, partition_group
            FROM pub.v_rule_pack_component
            WHERE season = :season AND fittable ORDER BY name
            """
        )
        with self._engine.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"season": season}).mappings()]

    def breakdowns(
        self, season: int, event_id: uuid.UUID, columns: Sequence[str]
    ) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
        """Typed component values per match and alliance."""
        if not columns:
            return {}
        wanted = self._checked_columns(season, columns)
        sql = text(
            f"SELECT match_id, alliance, {', '.join(wanted)} FROM pub.v_breakdown_{int(season)}"
            " WHERE event_id = :event AND level <> 'PRACTICE'"
        )
        with self._engine.connect() as conn:
            return {
                (uuid.UUID(str(r["match_id"])), r["alliance"]): {c: float(r[c] or 0.0) for c in wanted}
                for r in conn.execute(sql, {"event": event_id}).mappings()
            }

    def season_breakdowns(
        self, season: int, columns: Sequence[str], *, levels: Sequence[str] = ("QUALIFICATION",)
    ) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
        """Typed component values for a whole season, per match and alliance."""
        if not columns:
            return {}
        wanted = self._checked_columns(season, columns)
        sql = text(
            f"SELECT match_id, alliance, {', '.join(wanted)} FROM pub.v_breakdown_{int(season)}"
            " WHERE level = ANY(:levels)"
        )
        with self._engine.connect() as conn:
            return {
                (uuid.UUID(str(r["match_id"])), r["alliance"]): {c: float(r[c] or 0.0) for c in wanted}
                for r in conn.execute(sql, {"levels": list(levels)}).mappings()
            }

    def _checked_columns(self, season: int, columns: Sequence[str]) -> list[str]:
        wanted = sorted(set(columns))
        unknown = [c for c in wanted if c not in self.component_columns(season)]
        if unknown:
            raise ValueError(f"season {season} rule pack declares no such component: {unknown}")
        return wanted

    # -------------------------------------------------------------------------------------------------- ratings

    def model_versions(self, model: str) -> list[str]:
        """The versions of a model that have a completed batch run, newest run first.

        Reports what exists so a caller can be told which versions it might have meant.
        """
        sql = text(
            """
            SELECT model_version, max(finished_at_utc) AS finished
            FROM pub.v_fit_run WHERE model = :model
            GROUP BY model_version ORDER BY finished DESC
            """
        )
        with self._engine.connect() as conn:
            return [str(r.model_version) for r in conn.execute(sql, {"model": model})]

    def match_grain_epa(self, event_id: uuid.UUID, model_version: str) -> dict[int, dict[int, float]]:
        """Each team's rating as it stood after each match of one event, keyed by `as_of_match` then team."""
        sql = text(
            """
            SELECT as_of_match, team_number, epa_scaled
            FROM pub.v_team_epa
            WHERE event_id = :event AND model_version = :version AND tag = 'match'
            ORDER BY as_of_match, team_number
            """
        )
        out: dict[int, dict[int, float]] = {}
        with self._engine.connect() as conn:
            for row in conn.execute(sql, {"event": event_id, "version": model_version}):
                out.setdefault(row.as_of_match, {})[row.team_number] = float(row.epa_scaled)
        return out

    def pre_event_epa(self, event_id: uuid.UUID, model_version: str) -> list[PreEventEpaRow]:
        """What each team carried into this event."""
        sql = text(
            """
            SELECT season, event_id, event_ordinal, team_number, model_version,
                   epa_scaled, epa_norm, components, scale_provisional
            FROM pub.v_team_epa_pre_event
            WHERE event_id = :event AND model_version = :version
            ORDER BY team_number
            """
        )
        with self._engine.connect() as conn:
            return [
                PreEventEpaRow(
                    r.season,
                    uuid.UUID(str(r.event_id)),
                    r.event_ordinal,
                    r.team_number,
                    r.model_version,
                    r.epa_scaled,
                    r.epa_norm,
                    dict(r.components or {}),
                    r.scale_provisional,
                )
                for r in conn.execute(sql, {"event": event_id, "version": model_version})
            ]

    def prior_event_counts(self, event_id: uuid.UUID, model_version: str) -> dict[int, int]:
        """How many events each team of this event had already completed this season.

        Counted from `post_event` rows at a lower `event_ordinal`, so a team appearing for the first time this
        season is absent rather than present at zero.
        """
        sql = text(
            """
            WITH target AS (
                SELECT season, event_ordinal FROM pub.v_event_sequence WHERE event_id = :event
            )
            SELECT t.team_number, count(DISTINCT t.event_id) AS n_prior
            FROM pub.v_team_epa t
            JOIN pub.v_event_sequence s ON s.event_id = t.event_id
            JOIN target ON target.season = s.season
            WHERE t.model_version = :version
              AND t.tag = 'post_event'
              AND t.season = target.season
              AND s.event_ordinal < target.event_ordinal
            GROUP BY t.team_number
            """
        )
        with self._engine.connect() as conn:
            return {
                int(r.team_number): int(r.n_prior)
                for r in conn.execute(sql, {"event": event_id, "version": model_version})
            }

    def team_pridge_rows(
        self,
        event_id: uuid.UUID,
        model_version: str,
        *,
        as_of_match: int | None = None,
        component: str = "total",
    ) -> list[TeamPridgeRow]:
        """pRidge ratings for one event, at one match index or at every stored index."""
        sql = text(
            """
            SELECT fit_run_id, season, team_number, event_id, as_of_match,
                   model_version, component, pridge, lambda_
            FROM pub.v_team_pridge
            WHERE event_id = :event AND model_version = :version AND component = :component
              AND (CAST(:as_of AS int) IS NULL OR as_of_match = :as_of)
            ORDER BY as_of_match, team_number
            """
        )
        params = {"event": event_id, "version": model_version, "component": component, "as_of": as_of_match}
        with self._engine.connect() as conn:
            return [
                TeamPridgeRow(
                    uuid.UUID(str(r.fit_run_id)),
                    r.season,
                    r.team_number,
                    uuid.UUID(str(r.event_id)) if r.event_id else None,
                    r.as_of_match,
                    r.model_version,
                    r.component,
                    r.pridge,
                    r.lambda_,
                )
                for r in conn.execute(sql, params)
            ]

    # --------------------------------------------------------------------------------------------------- writes

    def start_fit_run(
        self,
        *,
        model: str,
        model_version: str,
        scope: str,
        season: int | None = None,
        event_id: uuid.UUID | None = None,
        prior_version: str | None = None,
        notes: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Open a `fit_run`, or reuse the live one for this event and prior version.

        A run is a fitting session, not a fitting event: during a live event every match's incremental refit
        appends to the same run.
        """
        stmt = text(
            """
            INSERT INTO derived.fit_run
                (fit_run_id, model, model_version, prior_version, scope, event_id, season, started_at_utc, notes)
            VALUES (:id, :model, :version, :prior, :scope, :event, :season, now(), CAST(:notes AS jsonb))
            ON CONFLICT (event_id, model, model_version, prior_version) WHERE event_id IS NOT NULL
            DO UPDATE SET started_at_utc = derived.fit_run.started_at_utc
            RETURNING fit_run_id
            """
        )
        with self._engine.begin() as conn:
            returned = conn.execute(
                stmt,
                {
                    "id": uuid.uuid4(),
                    "model": model,
                    "version": model_version,
                    "prior": prior_version,
                    "scope": scope,
                    "event": event_id,
                    "season": season,
                    "notes": json.dumps(notes) if notes else None,
                },
            ).scalar()
        return uuid.UUID(str(returned))

    def finish_fit_run(self, fit_run_id: uuid.UUID) -> int:
        """Finish a run, replacing the completed batch run it supersedes. Returns how many runs it replaced."""
        superseded = text(
            """
            DELETE FROM derived.fit_run old
            USING derived.fit_run new
            WHERE new.fit_run_id = :id
              AND new.event_id IS NULL
              AND old.event_id IS NULL
              AND old.fit_run_id <> new.fit_run_id
              AND old.finished_at_utc IS NOT NULL
              AND old.model = new.model
              AND old.season IS NOT DISTINCT FROM new.season
              AND old.model_version = new.model_version
              AND old.prior_version IS NOT DISTINCT FROM new.prior_version
            """
        )
        with self._engine.begin() as conn:
            replaced = int(conn.execute(superseded, {"id": fit_run_id}).rowcount)
            conn.execute(
                text("UPDATE derived.fit_run SET finished_at_utc = now() WHERE fit_run_id = :id"),
                {"id": fit_run_id},
            )
        if replaced:
            log.info("fit_run.replaced", fit_run_id=str(fit_run_id), replaced=replaced)
        return replaced

    def write_team_epa(self, fit_run_id: uuid.UUID, model_version: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert `team_epa` rows at any tag."""
        if not rows:
            return 0
        stmt = text(
            """
            INSERT INTO derived.team_epa
                (fit_run_id, season, team_number, event_id, tag, as_of_match, model_version,
                 epa_norm, epa_scaled, scale_provisional, components)
            VALUES (:fit_run_id, :season, :team_number, :event_id, :tag, :as_of_match, :model_version,
                    :epa_norm, :epa_scaled, :scale_provisional, CAST(:components AS jsonb))
            ON CONFLICT (fit_run_id, event_id, team_number, as_of_match, tag) DO UPDATE SET
                epa_norm = EXCLUDED.epa_norm,
                epa_scaled = EXCLUDED.epa_scaled,
                scale_provisional = EXCLUDED.scale_provisional,
                components = EXCLUDED.components
            """
        )
        payload = [
            {
                "fit_run_id": fit_run_id,
                "model_version": model_version,
                "components": json.dumps(r.get("components") or {}),
                # A norm needs a whole season's field to rank within, and a partial replay has none, so it is
                # absent rather than a KeyError.
                "epa_norm": r.get("epa_norm"),
                **{k: v for k, v in r.items() if k not in ("components", "epa_norm")},
            }
            for r in rows
        ]
        with self._engine.begin() as conn:
            _execute_chunked(conn, stmt, payload)
        return len(payload)

    def write_team_pridge(self, fit_run_id: uuid.UUID, model_version: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert `team_pridge` rows."""
        if not rows:
            return 0
        stmt = text(
            """
            INSERT INTO derived.team_pridge
                (fit_run_id, season, team_number, event_id, as_of_match, model_version, component, pridge, lambda_)
            VALUES (:fit_run_id, :season, :team_number, :event_id, :as_of_match, :model_version, :component,
                    :pridge, :lambda_)
            ON CONFLICT (fit_run_id, event_id, team_number, component, as_of_match) DO UPDATE SET
                pridge = EXCLUDED.pridge,
                lambda_ = EXCLUDED.lambda_
            """
        )
        payload = [{"fit_run_id": fit_run_id, "model_version": model_version, **dict(r)} for r in rows]
        with self._engine.begin() as conn:
            _execute_chunked(conn, stmt, payload)
        return len(payload)


def _execute_chunked(conn: Connection, stmt: Any, payload: Sequence[Mapping[str, Any]]) -> None:
    for i in range(0, len(payload), _CHUNK):
        conn.execute(stmt, list(payload[i : i + _CHUNK]))
