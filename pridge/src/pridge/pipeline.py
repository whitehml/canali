"""Running pRidge on real data: read an event from the warehouse, fit it, and save the ratings.

The model reads an event's inputs from the warehouse, fits it, and writes the ratings under
one `fit_run` that records the model and version its prior came from.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from pridge.constants import MODEL_VERSION
from pridge.design import RowKey
from pridge.fit import TOTAL, EventFit, fit_event
from pridge.prior import Prior, PriorSource
from warehouse.client import MatchRow, Warehouse
from warehouse.tier import tier_for

MODEL_NAME = "pridge"

# Above this the components no longer sum to the total, which means the rule pack's partition does not reconcile.
SUM_TOLERANCE = 1e-6

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EventInputs:
    """Everything one event fit reads, fetched once."""

    event_id: uuid.UUID
    season: int
    rows: list[MatchRow]
    prior: Prior
    prior_version: str
    partition: tuple[str, ...]
    component_responses: dict[str, dict[RowKey, float]]


@dataclass(frozen=True, slots=True)
class SeasonReport:
    """What a season sweep did: the run it wrote and the events it could not fit."""

    fit_run_id: uuid.UUID
    fitted: int
    skipped: list[tuple[str, str]]


def season_partition(warehouse: Warehouse, season: int) -> tuple[str, ...]:
    """The rule pack's partition as component columns."""
    groups: dict[str, list[str]] = {}
    for component in warehouse.fittable_components(season):
        group = component.get("partition_group")
        if group:
            groups.setdefault(str(group), []).append(str(component["column_name"]))
    if len(groups) > 1:
        raise ValueError(f"season {season} declares {len(groups)} partition groups {sorted(groups)}, expected one")
    return tuple(sorted(next(iter(groups.values())))) if groups else ()


def load_event(
    warehouse: Warehouse,
    event_id: uuid.UUID,
    season: int,
    source: PriorSource,
    *,
    partition: Sequence[str] | None = None,
) -> EventInputs:
    """Read one event's matches, its prior from `source`, and its component responses.

    `partition` defaults to the rule pack's; an empty one fits the total alone.
    """
    rows = warehouse.event_matches(event_id)
    if not rows:
        raise LookupError(f"event {event_id} has no qualification matches")

    prior = source.load(event_id)

    columns = tuple(partition) if partition is not None else season_partition(warehouse, season)
    responses: dict[str, dict[RowKey, float]] = {}
    if columns:
        responses = {name: {} for name in columns}
        for key, values in warehouse.breakdowns(season, event_id, columns).items():
            for name in columns:
                responses[name][key] = values[name]

    return EventInputs(
        event_id=event_id,
        season=season,
        rows=rows,
        prior=prior,
        prior_version=source.prior_version,
        partition=columns,
        component_responses=responses,
    )


def run_event(inputs: EventInputs, *, as_of_match: int | None = None) -> EventFit:
    """Fit one event."""
    return fit_event(
        inputs.rows,
        inputs.prior,
        as_of_match=as_of_match,
        component_responses=inputs.component_responses or None,
    )


def write_fit(warehouse: Warehouse, fit_run_id: uuid.UUID, inputs: EventInputs, result: EventFit) -> int:
    """Write one fit's ratings, the total and each component, at its match index."""
    sources = [(TOTAL, result.total), *((name, result.components[name]) for name in sorted(result.components))]
    rows = [
        {
            "season": inputs.season,
            "team_number": team,
            "event_id": inputs.event_id,
            "as_of_match": result.as_of_match,
            "component": component,
            "pridge": float(fit.beta[index]),
            "lambda_": result.lam,
        }
        for component, fit in sources
        for index, team in enumerate(result.team_numbers)
    ]
    return warehouse.write_team_pridge(fit_run_id, MODEL_VERSION, rows)


def run_season(warehouse: Warehouse, season: int, source: PriorSource) -> SeasonReport:
    """Fit every rated event of a season once, at its last match, under one run that replaces its predecessor.

    An event that cannot be fitted is reported and skipped.
    """
    partition = season_partition(warehouse, season)
    run_id = warehouse.start_fit_run(
        model=MODEL_NAME,
        model_version=MODEL_VERSION,
        scope="season",
        season=season,
        prior_version=source.prior_version,
        notes={"partition": list(partition)},
    )

    fitted = 0
    skipped: list[tuple[str, str]] = []
    for event in warehouse.events(season):
        if tier_for(event.event_type) is None:
            continue
        try:
            inputs = load_event(warehouse, event.event_id, season, source, partition=partition)
            result = run_event(inputs)
        except (LookupError, ValueError) as error:
            log.warning("pridge.event_skipped", code=event.event_code, reason=str(error))
            skipped.append((event.event_code, str(error)))
            continue
        _warn_if_partition_drifts(event.event_code, result)
        write_fit(warehouse, run_id, inputs, result)
        fitted += 1

    warehouse.finish_fit_run(run_id)
    return SeasonReport(fit_run_id=run_id, fitted=fitted, skipped=skipped)


def run_live(warehouse: Warehouse, inputs: EventInputs, *, from_match: int = 1) -> list[EventFit]:
    """Refit an event at every match index from `from_match`, writing each under the event's live run.

    A live run is one per event, model version and prior version.
    """
    run_id = warehouse.start_fit_run(
        model=MODEL_NAME,
        model_version=MODEL_VERSION,
        scope="event",
        season=inputs.season,
        event_id=inputs.event_id,
        prior_version=inputs.prior_version,
        notes={"partition": list(inputs.partition)},
    )
    ordinals = sorted({row.event_match_ordinal for row in inputs.rows if row.event_match_ordinal >= from_match})
    fits: list[EventFit] = []
    for k in ordinals:
        try:
            result = run_event(inputs, as_of_match=k)
        except ValueError:
            continue
        write_fit(warehouse, run_id, inputs, result)
        fits.append(result)
    warehouse.finish_fit_run(run_id)
    return fits


def _warn_if_partition_drifts(code: str, result: EventFit) -> None:
    error = result.component_sum_error()
    if error > SUM_TOLERANCE:
        log.warning("pridge.partition_drift", code=code, error=error)
