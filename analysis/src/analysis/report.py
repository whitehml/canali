"""The head-to-head report: the tables, and the versions and runs that produced them."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from analysis.comparison import ScoreComparison, SharedRows
from epa.pipeline import MODEL_NAME as EPA_MODEL
from pridge.constants import MODEL_VERSION as INSTALLED_PRIDGE_VERSION
from warehouse.client import Warehouse
from warehouse.ops import list_fit_runs

MIN_EVENTS_FOR_INTERVAL = 5


@dataclass(frozen=True, slots=True)
class EpaRun:
    season: int
    fit_run_id: uuid.UUID
    finished_at_utc: datetime


@dataclass(frozen=True, slots=True)
class Provenance:
    """What every number in a report was measured on."""

    epa_version: str
    pridge_version: str
    epa_runs: tuple[EpaRun, ...]


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    axis: str
    comparisons: Mapping[int, ScoreComparison]


def provenance(warehouse: Warehouse, epa_version: str, pridge_version: str, seasons: Sequence[int]) -> Provenance:
    if pridge_version != INSTALLED_PRIDGE_VERSION:
        raise ValueError(f"pRidge is refit from the installed engine, {INSTALLED_PRIDGE_VERSION}, not {pridge_version}")
    runs = []
    for season in seasons:
        run = warehouse.batch_fit_run(EPA_MODEL, season, epa_version)
        if run is None:
            raise LookupError(f"no completed {epa_version} batch run for season {season}")
        finished = {
            r.fit_run_id: r.finished_at_utc
            for r in list_fit_runs(warehouse.engine, model=EPA_MODEL, season=season, versions=[epa_version])
        }[run]
        if finished is None:
            raise LookupError(f"{epa_version} run {run} for season {season} has not finished")
        runs.append(EpaRun(season, run, finished))
    return Provenance(epa_version, pridge_version, tuple(runs))


def render(provenance: Provenance, shared: SharedRows, sections: Sequence[Section]) -> str:
    """The report as Markdown."""
    lines = [
        "# Head-to-head",
        "",
        f"- EPA {provenance.epa_version}",
        *(
            f"  - {r.season}: run {r.fit_run_id}, finished {r.finished_at_utc:%Y-%m-%d %H:%M} UTC"
            for r in provenance.epa_runs
        ),
        f"- pRidge {provenance.pridge_version}, refit with EPA {provenance.epa_version} as prior",
        "- OPR refit on the matches played so far at each index",
        "",
        f"{shared.n} alliance rows predicted by every model. "
        "Dropped to reach them: " + ", ".join(f"{model} {count}" for model, count in shared.dropped.items()) + ".",
    ]
    for section in sections:
        lines += ["", f"## {section.title}", "", *_table(section)]
    return "\n".join(lines) + "\n"


def _table(section: Section) -> list[str]:
    first = next(iter(section.comparisons.values()), None)
    if first is None:
        return ["No rows."]
    lines = [
        f"| {section.axis} | rows | events | {first.a} MSE | {first.b} MSE | difference | 95% interval |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, c in section.comparisons.items():
        interval = (
            f"{c.interval[0]:+.1f} to {c.interval[1]:+.1f}" if c.events >= MIN_EVENTS_FOR_INTERVAL else "too few events"
        )
        lines.append(
            f"| {label} | {c.rows} | {c.events} | {c.mse_a:.1f} | {c.mse_b:.1f} | {c.difference:+.1f} | {interval} |"
        )
    return lines
