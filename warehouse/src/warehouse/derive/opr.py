"""Computed OPR.

The least-squares solution to the team-alliance matrix.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.dialects.postgresql import insert

from warehouse.schema import core, derived

log = structlog.get_logger(__name__)

RESPONSES: tuple[str, ...] = ("opr_total", "opr_total_np", "opr_auto", "opr_teleop")


@dataclass(frozen=True)
class EventOpr:
    event_id: uuid.UUID
    teams: tuple[int, ...]
    values: dict[str, tuple[float, ...]]
    observations: int
    rank: int

    @property
    def identified(self) -> bool:
        """Whether the design has full column rank."""
        return self.rank >= len(self.teams)

    def rows(self) -> list[dict[str, Any]]:
        return [
            {"team_number": team, **{column: self.values[column][i] for column in RESPONSES}}
            for i, team in enumerate(self.teams)
        ]


def compute_event_opr(matches: Sequence[Mapping[str, Any]], event_id: uuid.UUID) -> EventOpr | None:
    """Solve one event from rows shaped like ``pub.v_match_rating_input``, or None if none carry a roster."""
    import numpy as np

    usable = [m for m in matches if m.get("team_numbers")]
    teams = sorted({int(t) for m in usable for t in m["team_numbers"]})
    if not teams:
        return None
    index = {team: i for i, team in enumerate(teams)}

    design = np.zeros((len(usable), len(teams)), dtype=float)
    for row, match in enumerate(usable):
        for team in match["team_numbers"]:
            design[row, index[int(team)]] = 1.0

    targets = np.column_stack([np.array([_response(m, c) for m in usable], dtype=float) for c in RESPONSES])
    solution, _residuals, rank, _singular = np.linalg.lstsq(design, targets, rcond=None)

    return EventOpr(
        event_id=event_id,
        teams=tuple(teams),
        values={column: tuple(float(v) for v in solution[:, i]) for i, column in enumerate(RESPONSES)},
        observations=len(usable),
        rank=int(rank),
    )


def _response(match: Mapping[str, Any], column: str) -> float:
    score = float(match.get("score") or 0.0)
    no_foul = float(match.get("score_no_foul") or 0.0)
    auto = float(match.get("score_auto") or 0.0)
    match column:
        case "opr_total":
            return score
        case "opr_total_np":
            return no_foul
        case "opr_auto":
            return auto
    return no_foul - auto


_MATCH_SQL = text(
    """
    SELECT team_numbers, score, score_auto, score_no_foul
    FROM pub.v_match_rating_input
    WHERE event_id = :event AND level = 'QUALIFICATION'
    """
)


def opr_for_event(conn: Connection, event_id: uuid.UUID) -> EventOpr | None:
    rows = [dict(r) for r in conn.execute(_MATCH_SQL, {"event": event_id}).mappings()]
    return compute_event_opr(rows, event_id)


def write_event_opr(conn: Connection, result: EventOpr) -> int:
    """Upsert one event's solution."""
    from warehouse.ingest.writer import ensure_teams

    rows = result.rows()
    if not rows:
        return 0
    ensure_teams(conn, {r["team_number"] for r in rows})
    now = datetime.now(UTC)
    stmt = insert(derived.team_event_opr).values(
        [{"event_id": result.event_id, "computed_at_utc": now, **row} for row in rows]
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["event_id", "team_number"],
            set_={
                c.name: stmt.excluded[c.name]
                for c in derived.team_event_opr.columns
                if c.name not in ("event_id", "team_number")
            },
        )
    )
    return len(rows)


def compute_season_opr(engine: Engine, season: int) -> dict[str, int]:
    """Solve and store every event in one season, counting events, rows, empty events and rank-deficient fits."""
    totals = {"events": 0, "solved": 0, "rows": 0, "no_matches": 0, "rank_deficient": 0}

    with engine.connect() as conn:
        targets = conn.execute(
            select(core.event.c.event_id, core.event.c.code)
            .where(core.event.c.season == season)
            .order_by(core.event.c.date_start, core.event.c.code)
        ).all()

    for event_id, code in targets:
        totals["events"] += 1
        with engine.begin() as conn:
            result = opr_for_event(conn, uuid.UUID(str(event_id)))
            if result is None:
                totals["no_matches"] += 1
                continue
            totals["rows"] += write_event_opr(conn, result)
        totals["solved"] += 1
        if not result.identified:
            totals["rank_deficient"] += 1
            log.debug("opr.rank_deficient", event_code=code, rank=result.rank, teams=len(result.teams))

    log.info("opr.season_done", season=season, **totals)
    return totals


__all__ = ["EventOpr", "compute_event_opr", "compute_season_opr", "opr_for_event", "write_event_opr"]
