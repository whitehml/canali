"""Fit-run inspection and version retirement.

Provides operator tools to manage the db and prune old versions.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import Engine, text

log = structlog.get_logger(__name__)

_WILDCARDS = ("%", "_", "*", "?")


class DropRefusedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FitRunRow:
    fit_run_id: uuid.UUID
    model: str
    model_version: str
    prior_version: str | None
    scope: str
    season: int | None
    event_code: str | None
    started_at_utc: datetime
    finished_at_utc: datetime | None
    rating_rows: int

    @property
    def finished(self) -> bool:
        return self.finished_at_utc is not None


@dataclass(frozen=True, slots=True)
class SeasonCount:
    season: int | None
    runs: int
    rating_rows: int


@dataclass(frozen=True, slots=True)
class DropPlan:
    """What a drop would take, per season, before anything is deleted."""

    model: str
    versions: tuple[str, ...]
    season: int | None
    runs: tuple[FitRunRow, ...]

    @property
    def per_season(self) -> tuple[SeasonCount, ...]:
        counts: dict[int | None, list[int]] = {}
        for run in self.runs:
            entry = counts.setdefault(run.season, [0, 0])
            entry[0] += 1
            entry[1] += run.rating_rows
        return tuple(
            SeasonCount(season, runs, rows)
            for season, (runs, rows) in sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0]))
        )

    @property
    def rating_rows(self) -> int:
        return sum(run.rating_rows for run in self.runs)

    @property
    def unfinished(self) -> tuple[FitRunRow, ...]:
        return tuple(run for run in self.runs if not run.finished)


_LIST_SQL = """
    SELECT
        r.fit_run_id,
        r.model,
        r.model_version,
        r.prior_version,
        r.scope,
        r.season,
        e.code AS event_code,
        r.started_at_utc,
        r.finished_at_utc,
        (SELECT count(*) FROM derived.team_epa t WHERE t.fit_run_id = r.fit_run_id)
        + (SELECT count(*) FROM derived.team_pridge p WHERE p.fit_run_id = r.fit_run_id) AS rating_rows
    FROM derived.fit_run r
    LEFT JOIN core.event e ON e.event_id = r.event_id
    WHERE (CAST(:model AS text) IS NULL OR r.model = :model)
      AND (CAST(:season AS int) IS NULL OR r.season = :season)
      AND (CAST(:versions AS text[]) IS NULL OR r.model_version = ANY(CAST(:versions AS text[])))
    ORDER BY r.model, r.season, r.model_version, r.started_at_utc DESC
"""


def list_fit_runs(
    engine: Engine,
    *,
    model: str | None = None,
    season: int | None = None,
    versions: Sequence[str] | None = None,
) -> list[FitRunRow]:
    """Every run matching the filters, newest run first within a version."""
    params: dict[str, Any] = {
        "model": model,
        "season": season,
        "versions": list(versions) if versions else None,
    }
    with engine.connect() as conn:
        rows = conn.execute(text(_LIST_SQL), params).mappings().all()
    return [
        FitRunRow(
            fit_run_id=uuid.UUID(str(r["fit_run_id"])),
            model=r["model"],
            model_version=r["model_version"],
            prior_version=r["prior_version"],
            scope=r["scope"],
            season=r["season"],
            event_code=r["event_code"],
            started_at_utc=r["started_at_utc"],
            finished_at_utc=r["finished_at_utc"],
            rating_rows=int(r["rating_rows"]),
        )
        for r in rows
    ]


def plan_drop(
    engine: Engine,
    model: str,
    versions: Sequence[str],
    *,
    season: int | None = None,
) -> DropPlan:
    """What ``drop_model_version`` would delete. Raises on a version that names nothing."""
    named = tuple(dict.fromkeys(versions))
    if not named:
        raise DropRefusedError("name at least one version")
    for version in named:
        if any(char in version for char in _WILDCARDS):
            raise DropRefusedError(f"{version!r} is not a version: name each one in full, no prefix and no wildcard")

    runs = tuple(list_fit_runs(engine, model=model, season=season, versions=named))
    found = {run.model_version for run in runs}
    missing = [version for version in named if version not in found]
    if missing:
        raise DropRefusedError(f"no {model} run at {', '.join(missing)}")
    return DropPlan(model=model, versions=named, season=season, runs=runs)


def drop_model_version(
    engine: Engine,
    model: str,
    versions: Sequence[str],
    *,
    season: int | None = None,
) -> DropPlan:
    """Delete the runs a plan names, and let the cascade take their ratings.

    Refuses while any of them is unfinished, which is what an event still fitting against the version looks like.
    """
    plan = plan_drop(engine, model, versions, season=season)
    if plan.unfinished:
        scopes = ", ".join(run.scope for run in plan.unfinished)
        raise DropRefusedError(f"unfinished {model} run at {scopes}: a fit may still be writing to it")

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM derived.fit_run WHERE fit_run_id = ANY(:ids)"),
            {"ids": [run.fit_run_id for run in plan.runs]},
        )
    log.info(
        "ops.dropped_model_version",
        model=model,
        versions=list(plan.versions),
        season=season,
        runs=len(plan.runs),
        rating_rows=plan.rating_rows,
    )
    return plan
