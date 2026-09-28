"""``epa`` command line."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

import structlog
import typer

from epa.constants import MODEL_VERSION, for_season
from epa.evaluate import (
    Prediction,
    early_event_breakdown,
    fit_surface,
    of_tier,
    score_metrics,
)
from epa.fit import fit_carryover, leave_one_season_out, search_layoff, search_schedules
from epa.model import Schedule
from epa.norm import build_norm_map
from epa.pipeline import SeasonData, load_season, persist, replay_live_event, run_season
from epa.scale import LayoffBoost
from warehouse.client import Warehouse
from warehouse.tier import EventTier

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = structlog.get_logger("epa.cli")

ALL_SEASONS = "2022,2023,2024,2025"

SEASONS = Annotated[str, typer.Option(help="Comma-separated, replayed in the order given")]
CLOSED = Annotated[int | None, typer.Option(help="Seasons at or below this have closed and use their own scale")]
COARSE = Annotated[bool, typer.Option(help="Small grid, for a trial run")]


def _echo(message: str) -> None:
    typer.echo(message)


def _seasons(value: str) -> list[int]:
    return [int(s) for s in value.split(",") if s.strip()]


def _closed(season: int, complete_through: int | None) -> bool:
    return complete_through is not None and season <= complete_through


# --------------------------------------------------------------------------------------------------------- rating


@app.command("check")
def check(
    seasons: SEASONS = ALL_SEASONS,
    strict: Annotated[bool, typer.Option(help="Refuse a season that does not fully reconcile")] = False,
) -> None:
    """Verify each season's partition without fitting anything."""
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            report = load_season(warehouse, season, strict=strict).report
            _echo(("OK   " if report.ok else "CHECK") + " " + report.summary())
            for key, total, summed in report.failures[:5]:
                _echo(f"        {key}: no_foul={total:g} vs partition {summed:g}")


@app.command("replay")
def replay(
    seasons: SEASONS = ALL_SEASONS,
    complete_through: CLOSED = None,
    write: Annotated[bool, typer.Option(help="Persist rows to derived.team_epa")] = False,
    match_grain: Annotated[bool, typer.Option(help="Also emit a row per team per match")] = False,
) -> None:
    """Replay seasons in order, carrying ratings across the transitions."""
    previous: dict[int, float] = {}
    second: dict[int, float] = {}
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            run = run_season(
                warehouse,
                season,
                previous_norm=previous,
                second_norm=second,
                season_complete=_closed(season, complete_through),
                match_grain=match_grain,
            )
            result = run.result
            skipped = f", {result.skipped_missing_breakdown} skipped" if result.skipped_missing_breakdown else ""
            provisional = " [PROVISIONAL SCALE]" if run.scale.provisional else ""
            counts = f"{result.matches} matches, {result.teams} teams, {len(result.rows)} rows"
            _echo(f"{season}: {counts}{skipped}{provisional}")
            if write:
                run_id = persist(
                    warehouse,
                    result.rows,
                    season=season,
                    scope=f"season:{season}",
                    notes={
                        "partition": list(run.partition.names),
                        "mu": run.scale.mu,
                        "sigma": run.scale.sigma,
                        "reconciled": run.report.rate,
                    },
                )
                _echo(f"        fit_run {run_id}")
            previous, second = result.final_norm, previous


@app.command("update")
def update(
    season: int,
    event_code: Annotated[str, typer.Argument(help="The event to rate")],
    model_version: Annotated[str, typer.Option(help="The batch run to continue")] = MODEL_VERSION,
    write: Annotated[bool, typer.Option(help="Persist rows to derived.team_epa")] = False,
) -> None:
    """Rate one in-progress event, continuing that version's batch run."""
    with Warehouse() as warehouse:
        events = [event for event in warehouse.events(season) if event.event_code == event_code]
        if not events:
            raise typer.BadParameter(f"no {season} event with code {event_code}")
        event_id = events[0].event_id
        result = replay_live_event(warehouse, season, event_id, model_version=model_version)
        _echo(f"{event_code}: {result.matches} matches, {len(result.rows)} rows")
        if write:
            run_id = persist(
                warehouse,
                result.rows,
                season=season,
                scope=f"event:{event_id}",
                event_id=event_id,
                model_version=model_version,
            )
            _echo(f"        fit_run {run_id}")


# ----------------------------------------------------------------------------------------------------- evaluation


@app.command("evaluate")
def evaluate(
    seasons: SEASONS = ALL_SEASONS,
    complete_through: CLOSED = None,
) -> None:
    """Next-match error and the per-tier cross-section."""
    previous: dict[int, float] = {}
    second: dict[int, float] = {}
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            run = run_season(
                warehouse,
                season,
                previous_norm=previous,
                second_norm=second,
                season_complete=_closed(season, complete_through),
            )
            predictions = fit_surface(run.result.predictions)
            mse, mae = score_metrics(predictions)
            _echo(f"{season}: MSE {mse:.1f} MAE {mae:.1f} over {len(predictions)} rows")
            for label, (split_mse, rows) in early_event_breakdown(predictions).items():
                _echo(f"        {label}: MSE {split_mse:.1f} over {rows} rows")
            for tier in EventTier:
                of = of_tier(predictions, tier)
                if of:
                    _echo(f"        tier {tier.value}: MSE {score_metrics(of)[0]:.1f} over {len(of)} rows")
            elims = [p for p in run.result.predictions if p.is_elimination]
            if elims:
                _echo(f"        elimination (unscored): MSE {score_metrics(elims)[0]:.1f} over {len(elims)} rows")
            previous, second = run.result.final_norm, previous


# -------------------------------------------------------------------------------------------------------- fitting


@app.command("fit-constants")
def fit_constants(seasons: SEASONS = ALL_SEASONS, coarse: COARSE = False) -> None:
    """Search K and M, and fit carryover with intervals.

    Emits a proposal rather than a deployment: the output is a constants file a human reviews and promotes.
    """
    season_list = _seasons(seasons)
    k_grid = (
        [(0.5, 0.3, 0.15)]
        if coarse
        else [
            (1.4, 1.0, 0.5),
            (1.2, 0.8, 0.4),
            (1.0, 0.7, 0.35),
            (0.85, 0.6, 0.3),
            (0.7, 0.5, 0.35),
            (0.7, 0.5, 0.5),
            (0.7, 0.5, 0.25),
            (0.7, 0.35, 0.35),
            (0.6, 0.4, 0.2),
            (0.5, 0.35, 0.25),
            (0.5, 0.3, 0.15),
            (0.4, 0.25, 0.12),
        ]
    )
    m_grid = (
        [(0.0, 0.15, 0.3)]
        if coarse
        else [
            (0.0, 0.0, 0.0),
            (0.05, 0.05, 0.05),
            (0.1, 0.1, 0.1),
            (0.0, 0.15, 0.3),
            (0.1, 0.25, 0.4),
        ]
    )

    with Warehouse() as warehouse:
        loaded = {season: load_season(warehouse, season) for season in season_list}
        _echo(f"loaded {len(loaded)} seasons; searching {len(k_grid) * len(m_grid)} candidates")

        def replay_with(season: int, k: Schedule, m: Schedule) -> list[Prediction]:
            run = run_season(
                warehouse,
                season,
                data=loaded[season],
                constants_override=replace(for_season(season), k=k, m=m),
                compute_norm=False,
            )
            return fit_surface(run.result.predictions)

        candidates = search_schedules(season_list, replay_with, k_grid=k_grid, m_grid=m_grid)
        _echo("top candidates (pooled MSE, then per season):")
        for candidate in candidates[:5]:
            _echo(f"  {candidate.describe()}")

        _echo("")
        _echo("leave-one-season-out (chosen WITHOUT the held-out season):")
        for result in leave_one_season_out(candidates, season_list):
            _echo(f"  {result.summary()}")

        _echo("")
        _echo("carryover:")
        finals, starts = _carryover_inputs(warehouse, season_list, loaded)
        for line in fit_carryover(finals, starts).report().splitlines():
            _echo(f"  {line}")


def _carryover_inputs(
    warehouse: Warehouse, seasons: list[int], loaded: dict[int, SeasonData]
) -> tuple[dict[int, dict[int, float]], dict[int, dict[int, float]]]:
    """Final ratings per season, and each season's early-season scoring rate, both as percentiles."""
    finals: dict[int, dict[int, float]] = {}
    starts: dict[int, dict[int, float]] = {}
    previous: dict[int, float] = {}
    second: dict[int, float] = {}
    for season in seasons:
        run = run_season(warehouse, season, data=loaded[season], previous_norm=previous, second_norm=second)
        finals[season] = run.result.final_norm
        early: dict[int, list[float]] = {}
        for prediction in fit_surface(run.result.predictions):
            if prediction.matches_played < 4:
                for team in prediction.team_numbers:
                    early.setdefault(team, []).append(prediction.actual / 2.0)
        rates = {team: sum(values) / len(values) for team, values in early.items()}
        try:
            start_map = build_norm_map(list(rates.values())) if rates else None
        except ValueError:
            start_map = None
        starts[season] = {team: start_map(rate) for team, rate in rates.items()} if start_map else {}
        previous, second = run.result.final_norm, previous
    return finals, starts


@app.command("fit-layoff")
def fit_layoff(seasons: SEASONS = ALL_SEASONS, coarse: COARSE = False) -> None:
    """Search the layoff boost against next-match error.

    The grid always contains zero, so the report says what the term is worth as well as where its optimum is.
    """
    season_list = _seasons(seasons)
    sigma_grid = [0.0, 0.09, 0.18] if coarse else [0.0, 0.03, 0.045, 0.06, 0.075, 0.09, 0.105, 0.12, 0.15, 0.18, 0.24]
    cap_grid = [90.0] if coarse else [60.0, 90.0, 120.0]

    with Warehouse() as warehouse:
        loaded = {season: load_season(warehouse, season) for season in season_list}
        _echo(f"loaded {len(loaded)} seasons; searching {len(sigma_grid) * len(cap_grid)} candidates")

        def replay_with(season: int, boost: LayoffBoost) -> list[Prediction]:
            run = run_season(
                warehouse,
                season,
                data=loaded[season],
                constants_override=replace(for_season(season), layoff_boost=boost),
                compute_norm=False,
            )
            return fit_surface(run.result.predictions)

        candidates = search_layoff(season_list, replay_with, sigma_grid=sigma_grid, cap_grid=cap_grid)
        _echo("top candidates (pooled MSE, then per season):")
        for candidate in candidates[:8]:
            _echo(f"  {candidate.describe()}")

        off = next(c for c in candidates if c.boost.sigma_per_30d == 0.0)
        best = candidates[0]
        change = 100.0 * (best.pooled_mse - off.pooled_mse) / off.pooled_mse
        _echo("")
        _echo(
            f"term off: pooled {off.pooled_mse:.2f} signed {off.pooled_signed:+.2f}; "
            f"best: pooled {best.pooled_mse:.2f} signed {best.pooled_signed:+.2f} ({change:+.2f}%)"
        )

        _echo("")
        _echo("leave-one-season-out (chosen WITHOUT the held-out season):")
        for result in leave_one_season_out(candidates, season_list):
            _echo(f"  {result.summary()}")


@app.command("constants")
def show_constants(season: int) -> None:
    """What the engine is currently using."""
    constants = for_season(season)
    _echo(f"season {season} | model_version {MODEL_VERSION}")
    _echo(f"  K            {constants.k.values} at {constants.k.breakpoints}")
    _echo(f"  M            {constants.m.values} at {constants.m.breakpoints}")
    _echo(f"  carryover    {constants.carryover}")
    _echo(f"  layoff       {constants.layoff_boost or 'OFF'}")
    _echo(f"  elim weight  {constants.elim_weight:g}")
    _echo(f"  init window  {constants.init_window} alliance-rows")
