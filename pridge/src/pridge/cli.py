"""``pridge`` command line."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

import structlog
import typer

from pridge import pipeline
from pridge.constants import MODEL_VERSION
from pridge.evaluate import Prediction, mse_by_index, next_match_predictions
from pridge.prior import EpaPriorSource, MissingPriorError
from pridge.tune import EventSample, observe
from warehouse.client import EventRow, Warehouse
from warehouse.rules.partition import ResolvedPartition
from warehouse.tier import EventTier, tier_for

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = structlog.get_logger("pridge.cli")

ALL_SEASONS = "2022,2023,2024,2025"

SEASONS = Annotated[str, typer.Option(help="Comma-separated")]
PRIOR_VERSION = Annotated[str, typer.Option(help="The prior to regularize toward, today an EPA model version")]
LIMIT = Annotated[int, typer.Option(help="Events to sample per season")]
WRITE = Annotated[bool, typer.Option(help="Persist rows to derived.team_pridge")]


class Group(StrEnum):
    """The events one lambda constant covers."""

    REGULAR = "regular"
    CHAMPIONSHIP = "championship"


GROUP_TIERS: dict[Group, tuple[EventTier, ...]] = {
    Group.REGULAR: (EventTier.REGULAR,),
    Group.CHAMPIONSHIP: (EventTier.RCMP, EventTier.CMP),
}

GROUP = Annotated[Group, typer.Option(help="Which lambda constant's events to measure")]


def _echo(message: str) -> None:
    typer.echo(message)


def _seasons(value: str) -> list[int]:
    return [int(s) for s in value.split(",") if s.strip()]


def _source(warehouse: Warehouse, season: int, prior_version: str) -> EpaPriorSource:
    """The prior source named by `--prior-version`."""
    try:
        return EpaPriorSource.for_season(warehouse, season, prior_version)
    except MissingPriorError as error:
        raise typer.BadParameter(str(error), param_hint="--prior-version") from error


def _event(warehouse: Warehouse, season: int, event_code: str) -> EventRow:
    for event in warehouse.events(season):
        if event.event_code == event_code:
            return event
    raise typer.BadParameter(f"no {season} event with code {event_code}")


def _spread(total: int, limit: int) -> Sequence[int]:
    """Indices spread across `total`, then the rest as backfill for picks that turn out to be unfittable."""
    if limit >= total:
        return range(total)
    spaced = [round(i * total / limit) for i in range(limit)]
    seen = set(spaced)
    return [*spaced, *(i for i in range(total) if i not in seen)]


def _sample(
    warehouse: Warehouse, season: int, source: EpaPriorSource, limit: int, tiers: Sequence[EventTier]
) -> list[EventSample]:
    """Up to `limit` loadable events of the given tiers, spread across the season."""
    events = [event for event in warehouse.events(season) if tier_for(event.event_type) in tiers]
    out: list[EventSample] = []
    for index in _spread(len(events), limit):
        if len(out) >= limit:
            break
        event = events[index]
        try:
            inputs = pipeline.load_event(
                warehouse, event.event_id, season, source, partition=ResolvedPartition.total_only(season)
            )
        except (LookupError, ValueError) as error:
            log.warning("pridge.event_skipped", code=event.event_code, reason=str(error))
            continue
        out.append(EventSample(event.event_code, inputs.rows, inputs.prior))
    return out


# --------------------------------------------------------------------------------------------------------- rating


@app.command("version")
def version() -> None:
    """The model version every rating is stored under."""
    _echo(MODEL_VERSION)


@app.command("fit-event")
def fit_event(
    season: int,
    event_code: Annotated[str, typer.Argument(help="The event to fit")],
    *,
    prior_version: PRIOR_VERSION,
    as_of_match: Annotated[int | None, typer.Option(help="Fit through this match ordinal")] = None,
    write: Annotated[bool, typer.Option(help="Refit at every match index and persist to derived.team_pridge")] = False,
) -> None:
    """Fit one event once and print the result, or with --write refit it at every match index and persist."""
    if write and as_of_match is not None:
        raise typer.BadParameter("a live refit covers every index", param_hint="--as-of-match")
    with Warehouse() as warehouse:
        event = _event(warehouse, season, event_code)
        source = _source(warehouse, season, prior_version)
        try:
            inputs = pipeline.load_event(warehouse, event.event_id, season, source)
        except (LookupError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error

        if write:
            fits = pipeline.run_live(warehouse, inputs)
            _echo(f"{event_code}: refit {len(fits)} match indices, lambda {fits[-1].lam:g}" if fits else "no fits")
            return

        result = pipeline.run_event(inputs, as_of_match=as_of_match)
        _echo(
            f"{event_code}: {len(result.team_numbers)} teams, {result.n_rows} rows through match {result.as_of_match}, "
            f"lambda {result.lam:g}, tr(H) {result.total.effective_dof:.2f}"
        )
        if result.components:
            names = ", ".join(sorted(result.components))
            _echo(f"        components {names}, sum error {result.component_sum_error():.2e}")


@app.command("backfit")
def backfit(season: int, prior_version: PRIOR_VERSION, write: WRITE = False) -> None:
    """Fit every rated event of a season once at its last match.

    --write persists the ratings, replacing the run at this version and prior.
    """
    with Warehouse() as warehouse:
        report = pipeline.run_season(warehouse, season, _source(warehouse, season, prior_version), write=write)
    _echo(f"{season}: {report.fitted} events fitted, {len(report.skipped)} skipped")
    if report.fit_run_id:
        _echo(f"        fit_run {report.fit_run_id}")
    for code, reason in report.skipped:
        _echo(f"        skip {code}: {reason}")


# ----------------------------------------------------------------------------------------------------- evaluation


def _mse(predictions: Sequence[Prediction]) -> float:
    return statistics.fmean(p.error * p.error for p in predictions)


@app.command("evaluate")
def evaluate(prior_version: PRIOR_VERSION, seasons: SEASONS = ALL_SEASONS, limit: LIMIT = 60) -> None:
    """Next-match error over a sample of events, pooled, by tier and by match index."""
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            source = _source(warehouse, season, prior_version)
            predictions: list[Prediction] = []
            for sample in _sample(warehouse, season, source, limit, tuple(EventTier)):
                predictions.extend(next_match_predictions(sample.rows, sample.prior))
            if not predictions:
                _echo(f"{season}: nothing to score")
                continue
            _echo(f"{season}: MSE {_mse(predictions):.1f} over {len(predictions)} rows")
            by_tier: dict[EventTier, list[Prediction]] = defaultdict(list)
            for p in predictions:
                tier = tier_for(p.event_type)
                if tier is not None:
                    by_tier[tier].append(p)
            for tier, rows in by_tier.items():
                _echo(f"        tier {tier.value}: MSE {_mse(rows):.1f} over {len(rows)} rows")
            for k, (mse, n) in list(mse_by_index(predictions).items())[:8]:
                _echo(f"        after match {k}: MSE {mse:.1f} over {n} rows")


# ---------------------------------------------------------------------------------------------------------- lambda


@app.command("derive-lambda")
def derive_lambda(
    prior_version: PRIOR_VERSION,
    group: GROUP,
    seasons: SEASONS = ALL_SEASONS,
    limit: LIMIT = 200,
) -> None:
    """The lambda minimizing leave-one-out error for one constant's events, pooled over seasons."""
    samples: list[EventSample] = []
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            source = _source(warehouse, season, prior_version)
            samples.extend(_sample(warehouse, season, source, limit, GROUP_TIERS[group]))
    if not samples:
        raise typer.BadParameter("no events loaded", param_hint="--group")
    report = observe(samples)
    low, high = report.bootstrap_interval()
    _echo(f"{group.value}: {len(report.event_codes)} events, {report.n} rows scored by leave-one-out error")
    _echo(f"optimum {report.pick():.3f}, 90% interval over events [{low:.3f}, {high:.3f}]")
    _echo(f"shipped {report.shipped:g} costs {report.cost_of(report.shipped):.2%} against it")


@app.command("season-lambda")
def season_lambda(
    season: int,
    *,
    prior_version: PRIOR_VERSION,
    group: GROUP,
    limit: LIMIT = 60,
    tolerance: Annotated[
        float, typer.Option(help="Cost against the season's own optimum before a change is worth it")
    ] = 0.02,
) -> None:
    """Whether this season wants a lambda other than the shipped constant.

    Outputs a reccomendation.
    """
    with Warehouse() as warehouse:
        samples = _sample(warehouse, season, _source(warehouse, season, prior_version), limit, GROUP_TIERS[group])
    if not samples:
        raise typer.BadParameter("no events loaded", param_hint="--group")
    report = observe(samples)
    low, high = report.bootstrap_interval()
    _echo(f"{season} {group.value}: {len(report.event_codes)} events, {report.n} rows scored by leave-one-out error")
    _echo(f"shipped {report.shipped:g}; season optimum {report.pick():.3f}, 90% interval [{low:.3f}, {high:.3f}]")
    change, why = report.verdict(tolerance=tolerance)
    typer.secho(
        f"{'CHANGE WORTH CONSIDERING' if change else 'KEEP'}: {why}",
        fg=typer.colors.YELLOW if change else typer.colors.GREEN,
    )
