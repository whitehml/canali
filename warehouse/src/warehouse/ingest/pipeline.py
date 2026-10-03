"""Ingest orchestration.

Every endpoint goes through :func:`Ingestor.step`: read the cursor, send it back as ``If-Modified-Since``, store the
payload if the body is new, fold the outcome into the cursor, and apply the transform only when a run opened.

FTC Events is authoritative. FTCScout supplies bulk history for matches, awards and playoff alliance roles, and only
ever fills what FIRST has not.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, select

from warehouse.config import Settings, load_settings
from warehouse.ingest import cursors, transforms, writer
from warehouse.ingest.client import ApiResponse, FtcEventsClient
from warehouse.ingest.ftcscout import FtcScoutClient
from warehouse.ingest.payloads import PayloadStore
from warehouse.rules.model import Component
from warehouse.rules.validate import validate_breakdown
from warehouse.schema import core
from warehouse.schema.types import ELIMINATION_MATCHES

log = structlog.get_logger(__name__)

Apply = Callable[[Connection, ApiResponse], int]

FIRST_ADVANCEMENT_SEASON = 2025


class Endpoint(StrEnum):
    """One of an event's endpoints the live poller asks for."""

    EVENT_TEAMS = "event_teams"
    HYBRID_QUAL = "hybrid_qual"
    HYBRID_PLAYOFF = "hybrid_playoff"
    SCORES_QUAL = "scores_qual"
    SCORES_PLAYOFF = "scores_playoff"
    RANKINGS = "rankings"
    ALLIANCES = "alliances"
    AWARDS = "awards"
    ADVANCEMENT = "advancement"


@dataclass(slots=True)
class StepResult:
    endpoint: str
    outcome: str
    opened: bool
    rows: int = 0


@dataclass(slots=True)
class EventIngestReport:
    season: int
    event_code: str
    event_id: uuid.UUID | None = None
    steps: list[StepResult] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def changed(self) -> bool:
        return any(s.opened for s in self.steps)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"{self.event_code}: skipped ({self.skipped_reason})"
        parts = [f"{s.endpoint.split('/')[-1]}={s.outcome}:{s.rows}" for s in self.steps]
        return f"{self.event_code}: " + " ".join(parts)


class Ingestor:
    def __init__(
        self,
        engine: Engine,
        client: FtcEventsClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.engine = engine
        self.client = client or FtcEventsClient(self.settings)
        self.payloads = PayloadStore(self.settings.payload_root)

    # ---------------------------------------------------------------------------------------------------- step

    def step(
        self,
        conn: Connection,
        key: cursors.CursorKey,
        response_fn: Callable[[str | None], ApiResponse],
        apply: Apply,
        *,
        scope_token: str | None = None,
    ) -> StepResult:
        """One endpoint, one conditional fetch, one transform.

        ``scope_token`` is how the transform filters the payload, typically a region code."""
        token = scope_token or ""
        cursor = cursors.read(conn, key)
        response = response_fn(cursor.last_modified if cursor else None)

        payload_hash: str | None = None
        if response.outcome == "changed":
            stored = self.payloads.put(response.body)
            self.payloads.record(conn, stored, endpoint=key.endpoint, last_modified=response.last_modified)
            payload_hash = stored.payload_hash

        rows = 0
        opened = cursors.observe(conn, key, response, payload_hash=payload_hash)
        reapplied = False

        if opened:
            rows = apply(conn, response)
        elif cursor is not None and cursor.payload_hash is not None and cursor.scope_token != token:
            rows = apply(conn, self._replay(key.endpoint, cursor.payload_hash))
            reapplied = True

        if opened or reapplied:
            cursors.set_rows_written(conn, key, rows, scope_token=token)

        log.info(
            "ingest.step",
            endpoint=key.endpoint,
            outcome=response.outcome,
            opened=opened,
            reapplied=reapplied,
            rows=rows,
        )
        return StepResult(key.endpoint, response.outcome, opened or reapplied, rows)

    def _replay(self, endpoint: str, payload_hash: str) -> ApiResponse:
        """Rebuild a response from the raw store, without touching the network."""
        body = self.payloads.get(payload_hash)
        return ApiResponse(endpoint, 200, "changed", body, None, json.loads(body))

    # -------------------------------------------------------------------------------------------------- season

    def ingest_season_events(self, season: int, region_code: str | None = None) -> StepResult:
        """The season's event list.

        The ``regionCode`` query parameter returns the worldwide list regardless, so a region is filtered here,
        after the fetch.
        """
        endpoint = f"/{season}/events"
        key = cursors.CursorKey(endpoint=endpoint, season=season)

        def apply(conn: Connection, response: ApiResponse) -> int:
            rows = transforms.event_rows(season, response.data, self.settings.default_timezone)
            if region_code:
                rows = [r for r in rows if r["region_code"] == region_code]
            writer.upsert_seasons(conn, [_season_row(season)])
            return writer.upsert_events(conn, rows)

        with self.engine.begin() as conn:
            return self.step(
                conn,
                key,
                lambda lm: self.client.fetch(endpoint, if_modified_since=lm),
                apply,
                scope_token=region_code,
            )

    def ingest_season_teams(self, season: int, region_code: str | None = None) -> StepResult:
        endpoint = f"/{season}/teams"
        key = cursors.CursorKey(endpoint=endpoint, season=season)

        def apply(conn: Connection, response: ApiResponse) -> int:
            writer.upsert_teams(conn, transforms.team_rows(response.data))
            rows = transforms.team_season_rows(season, response.data)
            if region_code:
                rows = [r for r in rows if r["home_region"] == region_code]
            return writer.upsert_team_seasons(conn, rows)

        with self.engine.begin() as conn:
            return self.step(
                conn,
                key,
                lambda lm: self.client.season_teams(season, if_modified_since=lm),
                apply,
                scope_token=region_code,
            )

    # ------------------------------------------------------------------------------------------------ backfill

    def backfill_season(
        self,
        season: int,
        *,
        windows: Sequence[tuple[str, str]] | None = None,
        region_code: str | None = None,
    ) -> dict[str, int]:
        """Bulk match history for one season from FTCScout.

        Events must already be in the fact base. FTCScout does not publish FIRST's ``eventId``, so the event list
        comes from FTC Events and FTCScout supplies only the matches. Paged by date window, ``eventsSearch`` taking
        a start and an end but no offset.
        """
        totals = {"events": 0, "matched": 0, "unknown_event": 0, "matches": 0, "conflicts": 0}
        windows = windows or season_windows(season)

        with FtcScoutClient(self.settings) as scout:
            for start, end in windows:
                events = scout.events_with_matches(season, start, end)
                totals["events"] += len(events)

                with self.engine.begin() as conn:
                    known = _event_ids_by_code(conn, season)
                    for event in events:
                        event_id = known.get(str(event["code"]).upper())
                        if event_id is None:
                            totals["unknown_event"] += 1
                            continue
                        if region_code and event.get("regionCode") != region_code:
                            continue
                        totals["matched"] += 1
                        timezone = _event_timezone(conn, event_id, self.settings.default_timezone)
                        rows = transforms.scout_match_rows(event_id, timezone, event.get("matches") or [])
                        written, conflicts = writer.write_matches_non_authoritative(conn, rows)
                        totals["matches"] += written
                        totals["conflicts"] += conflicts

                log.info("backfill.window", season=season, start=start, end=end, events=len(events), **totals)
        return totals

    def backfill_alliance_roles(
        self,
        season: int,
        *,
        windows: Sequence[tuple[str, str]] | None = None,
        region_code: str | None = None,
    ) -> dict[str, int]:
        """Playoff alliance roles for one season, from FTCScout.

        The role is the per-match convenience alongside the published selection in ``core.playoff_alliance``, and it
        is the only alliance record held for the seasons before that table was written.
        """
        totals = {
            "events": 0,
            "matched": 0,
            "unknown_event": 0,
            "roles_seen": 0,
            "updated": 0,
            "unmatched": 0,
            "disagreed": 0,
        }
        windows = windows or season_windows(season)

        with FtcScoutClient(self.settings) as scout:
            for start, end in windows:
                events = scout.alliance_roles(season, start, end)
                totals["events"] += len(events)

                with self.engine.begin() as conn:
                    known = _event_ids_by_code(conn, season)
                    for event in events:
                        event_id = known.get(str(event["code"]).upper())
                        if event_id is None:
                            totals["unknown_event"] += 1
                            continue
                        if region_code and event.get("regionCode") != region_code:
                            continue
                        totals["matched"] += 1
                        rows = transforms.scout_alliance_role_rows(event_id, event.get("matches") or [])
                        totals["roles_seen"] += len(rows)
                        for name, value in writer.update_alliance_roles(conn, event_id, rows).items():
                            totals[name] += value

                log.info("alliance_roles.window", season=season, start=start, **totals)
        log.info("alliance_roles.done", season=season, **totals)
        return totals

    def backfill_breakdowns(
        self,
        season: int,
        *,
        limit: int | None = None,
        progress_every: int = 100,
    ) -> dict[str, int]:
        """Component-level history from ``/scores`` for a season.

        Resumable, each event-level pair having its own cursor.
        """
        totals = {"events": 0, "fetched": 0, "attached": 0, "empty": 0, "failed": 0}
        failures: list[str] = []

        with self.engine.connect() as conn:
            codes = list(
                conn.execute(
                    select(core.event.c.code)
                    .where(
                        core.event.c.season == season,
                        core.event.c.event_id.in_(select(core.match.c.event_id).distinct()),
                    )
                    .order_by(core.event.c.date_start, core.event.c.code)
                ).scalars()
            )
            components = _components_for(conn, season)
        if limit:
            codes = codes[:limit]
        if not components:
            log.warning("backfill.no_rule_pack", season=season)

        for index, code in enumerate(codes, start=1):
            totals["events"] += 1
            try:
                with self.engine.begin() as conn:
                    event_id = conn.execute(
                        select(core.event.c.event_id).where(core.event.c.season == season, core.event.c.code == code)
                    ).scalar()
                    if event_id is None:
                        continue
                    for level in ("qual", "playoff"):
                        result = self._step_scores(
                            conn,
                            season,
                            code,
                            uuid.UUID(str(event_id)),
                            level=level,
                            components=components,
                        )
                        totals["fetched"] += 1
                        if result.outcome == "empty":
                            totals["empty"] += 1
                        totals["attached"] += result.rows
            except Exception as exc:
                totals["failed"] += 1
                failures.append(f"{code}: {exc}")
                log.warning("backfill.event_failed", season=season, event_code=code, error=str(exc))

            if progress_every and index % progress_every == 0:
                log.info(
                    "backfill.breakdowns.progress",
                    season=season,
                    done=index,
                    of=len(codes),
                    attached=totals["attached"],
                )

        if failures:
            log.warning("backfill.failures", season=season, count=len(failures), first=failures[:5])
        return totals

    def backfill_awards(
        self, season: int, *, region_code: str | None = None, limit: int | None = None
    ) -> dict[str, int]:
        """Awards for a season, optionally scoped to one region.

        One call per event. Awards lag until an event closes."""
        totals = {"events": 0, "fetched": 0, "awards": 0, "empty": 0, "failed": 0}

        with self.engine.connect() as conn:
            query = select(core.event.c.code, core.event.c.event_id).where(core.event.c.season == season)
            if region_code:
                query = query.where(core.event.c.region_code == region_code)
            targets = conn.execute(query.order_by(core.event.c.date_start)).all()
        if limit:
            targets = targets[:limit]

        for code, event_id in targets:
            totals["events"] += 1
            try:
                with self.engine.begin() as conn:
                    result = self._step_awards(conn, season, code, uuid.UUID(str(event_id)))
                totals["fetched"] += 1
                totals["awards"] += result.rows
                if result.outcome == "empty":
                    totals["empty"] += 1
            except Exception as exc:
                totals["failed"] += 1
                log.warning("backfill.awards_failed", event_code=code, error=str(exc))
        return totals

    def backfill_alliances(
        self, season: int, *, region_code: str | None = None, limit: int | None = None
    ) -> dict[str, int]:
        """Seated alliances and the selection for a season, over events with a playoff match.

        Two calls per event, resumable through their cursors."""
        totals = {"events": 0, "alliances": 0, "picks": 0, "empty": 0, "failed": 0}

        with self.engine.connect() as conn:
            query = select(core.event.c.code, core.event.c.event_id).where(
                core.event.c.season == season,
                core.event.c.event_id.in_(
                    select(core.match.c.event_id).where(core.match.c.level.in_(ELIMINATION_MATCHES)).distinct()
                ),
            )
            if region_code:
                query = query.where(core.event.c.region_code == region_code)
            targets = conn.execute(query.order_by(core.event.c.date_start, core.event.c.code)).all()
        if limit:
            targets = targets[:limit]

        for code, event_id in targets:
            totals["events"] += 1
            try:
                with self.engine.begin() as conn:
                    seated = self._step_alliances(conn, season, code, uuid.UUID(str(event_id)))
                    picks = self._step_alliance_selection(conn, season, code, uuid.UUID(str(event_id)))
                totals["alliances"] += seated.rows
                totals["picks"] += picks.rows
                if seated.outcome == "empty":
                    totals["empty"] += 1
            except Exception as exc:
                totals["failed"] += 1
                log.warning("backfill.alliances_failed", event_code=code, error=str(exc))

        log.info("alliances.done", season=season, **totals)
        return totals

    def backfill_scout_awards(self, season: int) -> dict[str, int]:
        """Worldwide awards for one season from FTCScout, in one call."""
        totals = {"events_seen": 0, "events_matched": 0, "events_unknown": 0, "awards": 0, "conflicts": 0}

        with FtcScoutClient(self.settings) as scout:
            events = scout.season_awards(season)
        totals["events_seen"] = len(events)

        with self.engine.connect() as conn:
            known = _event_ids_by_code(conn, season)

        for event in events:
            code = str(event.get("code") or "").upper()
            event_id = known.get(code)
            if event_id is None:
                totals["events_unknown"] += 1
                log.warning("scout_awards.unknown_event", season=season, event_code=code)
                continue
            rows = transforms.scout_award_rows(event_id, event.get("awards") or [])
            if not rows:
                continue
            with self.engine.begin() as conn:
                written, conflicts = writer.write_awards_non_authoritative(conn, event_id, rows)
            totals["events_matched"] += 1
            totals["awards"] += written
            totals["conflicts"] += conflicts

        log.info("scout_awards.done", season=season, **totals)
        return totals

    # --------------------------------------------------------------------------------------------------- event

    def ingest_event(self, season: int, event_code: str) -> EventIngestReport:
        """One event, all endpoints, every row traceable to a stored payload."""
        report = EventIngestReport(season=season, event_code=event_code)

        with self.engine.begin() as conn:
            event_id = self._resolve_event(conn, season, event_code, report)
            if event_id is None:
                return report
            report.event_id = event_id
            components = _components_for(conn, season)

        steps: list[StepResult] = []
        with self.engine.begin() as conn:
            steps.append(self._step_event_teams(conn, season, event_code, event_id))
        # Matches before scores: a breakdown attaches to the match in its slot, so the match must exist first.
        for level in ("qual", "playoff"):
            with self.engine.begin() as conn:
                steps.append(self._step_matches(conn, season, event_code, event_id, level))
        for level in ("qual", "playoff"):
            with self.engine.begin() as conn:
                steps.append(self._step_scores(conn, season, event_code, event_id, level=level, components=components))
        with self.engine.begin() as conn:
            steps.append(self._step_rankings(conn, season, event_code, event_id))
            steps.append(self._step_awards(conn, season, event_code, event_id))
        # Alliances before the selection: a pick's alliance number is read back from the seated alliances.
        with self.engine.begin() as conn:
            steps.append(self._step_alliances(conn, season, event_code, event_id))
            steps.append(self._step_alliance_selection(conn, season, event_code, event_id))

        if season >= FIRST_ADVANCEMENT_SEASON:
            with self.engine.begin() as conn:
                steps.append(self._step_advancement_points(conn, season, event_code, event_id))
                steps.append(self._step_advancement_slots(conn, season, event_code, event_id))

        report.steps = steps
        return report

    def ingest_endpoint(
        self, season: int, event_code: str, event_id: uuid.UUID, endpoint: Endpoint
    ) -> list[StepResult]:
        """Fetch one of an event's endpoints."""
        if endpoint is Endpoint.ADVANCEMENT and season < FIRST_ADVANCEMENT_SEASON:
            return []
        args = (season, event_code, event_id)
        steps: dict[Endpoint, tuple[Callable[[Connection], StepResult], ...]] = {
            Endpoint.EVENT_TEAMS: (lambda c: self._step_event_teams(c, *args),),
            Endpoint.HYBRID_QUAL: (lambda c: self._step_matches(c, *args, "qual"),),
            Endpoint.HYBRID_PLAYOFF: (lambda c: self._step_matches(c, *args, "playoff"),),
            Endpoint.SCORES_QUAL: (
                lambda c: self._step_scores(c, *args, level="qual", components=_components_for(c, season)),
            ),
            Endpoint.SCORES_PLAYOFF: (
                lambda c: self._step_scores(c, *args, level="playoff", components=_components_for(c, season)),
            ),
            Endpoint.RANKINGS: (lambda c: self._step_rankings(c, *args),),
            Endpoint.ALLIANCES: (
                lambda c: self._step_alliances(c, *args),
                lambda c: self._step_alliance_selection(c, *args),
            ),
            Endpoint.AWARDS: (lambda c: self._step_awards(c, *args),),
            Endpoint.ADVANCEMENT: (
                lambda c: self._step_advancement_points(c, *args),
                lambda c: self._step_advancement_slots(c, *args),
            ),
        }
        results = []
        for step in steps[endpoint]:
            with self.engine.begin() as conn:
                results.append(step(conn))
        return results

    # --------------------------------------------------------------------------------------------- event steps

    def _resolve_event(
        self,
        conn: Connection,
        season: int,
        event_code: str,
        report: EventIngestReport,
    ) -> uuid.UUID | None:
        stored = conn.execute(
            select(core.event.c.event_id).where(core.event.c.season == season, core.event.c.code == event_code)
        ).scalar()
        if stored is not None:
            return uuid.UUID(str(stored))

        response = self.client.season_events(season, eventCode=event_code)
        raw = [e for e in (response.data or {}).get("events", []) if e.get("code", "").upper() == event_code.upper()]
        if not raw:
            report.skipped_reason = "not found"
            return None
        if transforms.is_excluded_event(raw[0]):
            report.skipped_reason = "remote or hybrid event"
            log.info("ingest.event.excluded", event_code=event_code, season=season)
            return None

        writer.upsert_seasons(conn, [_season_row(season)])
        rows = transforms.event_rows(season, {"events": raw}, self.settings.default_timezone)
        writer.upsert_events(conn, rows)
        return uuid.UUID(str(rows[0]["event_id"]))

    def _step_event_teams(self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID) -> StepResult:
        endpoint = f"/{season}/teams?eventCode={event_code}"
        key = cursors.CursorKey(endpoint=endpoint, season=season, event_id=event_id)

        def apply(c: Connection, response: ApiResponse) -> int:
            writer.upsert_teams(c, transforms.team_rows(response.data))
            writer.upsert_team_seasons(c, transforms.team_season_rows(season, response.data))
            return writer.write_event_teams(c, event_id, transforms.event_team_numbers(response.data))

        return self.step(conn, key, lambda lm: self.client.event_teams(season, event_code, if_modified_since=lm), apply)

    def _step_matches(
        self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID, level: str
    ) -> StepResult:
        endpoint = f"/{season}/schedule/{event_code}/{level}/hybrid"
        key = cursors.CursorKey(endpoint=endpoint, season=season, event_id=event_id)
        timezone = _event_timezone(conn, event_id, self.settings.default_timezone)

        def apply(c: Connection, response: ApiResponse) -> int:
            rows = transforms.hybrid_match_rows(event_id, timezone, response.data)
            written, replays = writer.write_matches(c, rows)
            if replays:
                log.warning("ingest.replays", event_code=event_code, level=level, count=replays)
            return written

        return self.step(
            conn,
            key,
            lambda lm: self.client.hybrid_schedule(season, event_code, level, if_modified_since=lm),
            apply,
        )

    def _step_scores(
        self,
        conn: Connection,
        season: int,
        event_code: str,
        event_id: uuid.UUID,
        *,
        level: str,
        components: Sequence[Component],
    ) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/scores/{event_code}/{level}", season=season, event_id=event_id)

        def apply(c: Connection, response: ApiResponse) -> int:
            rows = list(transforms.score_breakdowns(response.data))
            for lvl, series, number, side, breakdown in rows:
                validate_breakdown(breakdown, components).raise_if_invalid(
                    f"{event_code} {lvl}-{series}-{number} {side}"
                )
            return writer.write_breakdowns(c, event_id, rows)

        return self.step(
            conn,
            key,
            lambda lm: self.client.scores(season, event_code, level, if_modified_since=lm),
            apply,
        )

    def _step_rankings(self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/rankings/{event_code}", season=season, event_id=event_id)
        return self.step(
            conn,
            key,
            lambda lm: self.client.rankings(season, event_code, if_modified_since=lm),
            lambda c, r: writer.write_rankings(c, event_id, transforms.ranking_rows(event_id, r.data)),
        )

    def _step_awards(self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/awards/{event_code}", season=season, event_id=event_id)
        return self.step(
            conn,
            key,
            lambda lm: self.client.awards(season, event_code, if_modified_since=lm),
            lambda c, r: writer.write_awards(c, event_id, transforms.award_rows(event_id, r.data)),
        )

    def _step_alliances(self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/alliances/{event_code}", season=season, event_id=event_id)
        return self.step(
            conn,
            key,
            lambda lm: self.client.alliances(season, event_code, if_modified_since=lm),
            lambda c, r: writer.write_playoff_alliances(
                c, event_id, transforms.playoff_alliance_rows(event_id, r.data)
            ),
        )

    def _step_alliance_selection(
        self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID
    ) -> StepResult:
        key = cursors.CursorKey(
            endpoint=f"/{season}/alliances/{event_code}/selection", season=season, event_id=event_id
        )
        return self.step(
            conn,
            key,
            lambda lm: self.client.alliance_selection(season, event_code, if_modified_since=lm),
            lambda c, r: writer.write_alliance_picks(c, event_id, transforms.alliance_pick_rows(event_id, r.data)),
        )

    def _step_advancement_points(
        self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID
    ) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/advancement/{event_code}/points", season=season, event_id=event_id)
        return self.step(
            conn,
            key,
            lambda lm: self.client.advancement_points(season, event_code, if_modified_since=lm),
            lambda c, r: writer.write_advancement_points(
                c, event_id, transforms.advancement_points_rows(event_id, r.data)
            ),
        )

    def _step_advancement_slots(
        self, conn: Connection, season: int, event_code: str, event_id: uuid.UUID
    ) -> StepResult:
        key = cursors.CursorKey(endpoint=f"/{season}/advancement/{event_code}", season=season, event_id=event_id)
        return self.step(
            conn,
            key,
            lambda lm: self.client.advancement_slots(season, event_code, if_modified_since=lm),
            lambda c, r: self._write_advancement(c, event_id, r.data),
        )

    @staticmethod
    def _write_advancement(conn: Connection, event_id: uuid.UUID, data: Any) -> int:
        """One fetch fills both tables, the header carrying the event's slot quota that nothing else records."""
        writer.write_event_advancement(conn, transforms.event_advancement_row(event_id, data))
        return writer.write_advancement_slots(conn, event_id, transforms.advancement_slot_rows(event_id, data))


# ------------------------------------------------------------------------------------------------------ helpers


def season_windows(season: int) -> list[tuple[str, str]]:
    """Month-wide windows spanning an FTC season, August to the following August."""
    out: list[tuple[str, str]] = []
    year, month = season, 8
    for _ in range(13):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        out.append((f"{year}-{month:02d}-01", f"{next_year}-{next_month:02d}-01"))
        year, month = next_year, next_month
    return out


def _season_row(season: int) -> dict[str, Any]:
    """A placeholder season, so an event has its parent. The game name arrives with the rule pack."""
    return {"season": season, "name": f"{season}-{str(season + 1)[2:]}", "game": ""}


def _event_ids_by_code(conn: Connection, season: int) -> dict[str, uuid.UUID]:
    rows = conn.execute(select(core.event.c.code, core.event.c.event_id).where(core.event.c.season == season)).all()
    return {str(code).upper(): uuid.UUID(str(event_id)) for code, event_id in rows}


def _event_timezone(conn: Connection, event_id: uuid.UUID, default: str) -> str:
    tz = conn.execute(select(core.event.c.timezone).where(core.event.c.event_id == event_id)).scalar()
    return str(tz) if tz else default


def _components_for(conn: Connection, season: int) -> list[Component]:
    """Read the pack from the database"""
    rows = conn.execute(
        select(
            core.rule_pack_component.c.name,
            core.rule_pack_component.c.level,
            core.rule_pack_component.c.kind,
            core.rule_pack_component.c.is_subtotal,
            core.rule_pack_component.c.is_derived,
            core.rule_pack_component.c.column_name,
        ).where(core.rule_pack_component.c.season == season)
    ).mappings()
    return [Component.model_validate(dict(r)) for r in rows]
