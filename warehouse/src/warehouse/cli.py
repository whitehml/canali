"""``warehouse`` command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import structlog
import typer
from sqlalchemy import text

from warehouse import migrate, views
from warehouse.config import load_settings
from warehouse.db import make_engine
from warehouse.rules import generate, loader

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
db_app = typer.Typer(no_args_is_help=True, help="Local database and migrations.")
rules_app = typer.Typer(no_args_is_help=True, help="Rule packs.")
ingest_app = typer.Typer(no_args_is_help=True, help="Ingest from FTC Events and FTCScout.")
derive_app = typer.Typer(no_args_is_help=True, help="Quantities the warehouse computes.")
ops_app = typer.Typer(no_args_is_help=True, help="Fit runs and model versions.")
app.add_typer(db_app, name="db")
app.add_typer(rules_app, name="rules")
app.add_typer(ingest_app, name="ingest")
app.add_typer(derive_app, name="derive")
app.add_typer(ops_app, name="ops")

log = structlog.get_logger("warehouse.cli")


def _echo(message: str) -> None:
    typer.echo(message)


def _seasons(value: str) -> list[int]:
    return [int(s) for s in value.split(",") if s.strip()]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _totals(season: int, totals: dict[str, int]) -> str:
    return f"{season}: " + " ".join(f"{k}={v}" for k, v in totals.items())


SEASONS = Annotated[str, typer.Option(help="Comma-separated")]
REGION = Annotated[str | None, typer.Option(help="Restrict to one region, e.g. USPA")]

# ------------------------------------------------------------------------------------------------------------ db


@db_app.command("start")
def db_start() -> None:
    """Start a local Postgres, no root required, and print its URL."""
    from warehouse.localdb import start

    _echo(start())


@db_app.command("stop")
def db_stop() -> None:
    from warehouse.localdb import stop

    _echo("stopped" if stop() else "not running")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Run migrations. Every migration ends by rebuilding views/."""
    migrate.upgrade(revision=revision)
    _echo(f"upgraded to {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: str = "-1") -> None:
    migrate.downgrade(revision=revision)
    _echo(f"downgraded to {revision}")


@db_app.command("current")
def db_current() -> None:
    migrate.current()


@db_app.command("revision")
def db_revision(message: str, autogenerate: bool = True) -> None:
    migrate.revision(message, autogenerate=autogenerate)


@db_app.command("rebuild-views")
def db_rebuild_views() -> None:
    """Drop every view and re-run views/. Idempotent by construction."""
    engine = make_engine()
    with engine.begin() as conn:
        applied = views.rebuild(conn)
    _echo(f"rebuilt {len(applied)} view files")


# --------------------------------------------------------------------------------------------------------- rules


@rules_app.command("load")
def rules_load(packs_dir: Path | None = None) -> None:
    """Load rule_packs/*.toml into core.rule_pack*, then rebuild views."""
    engine = make_engine()
    with engine.begin() as conn:
        counts = loader.load_from_disk(conn, packs_dir)
        views.rebuild(conn)
    for season, count in sorted(counts.items()):
        _echo(f"{season}: {count} components")


@rules_app.command("check")
def rules_check(packs_dir: Path | None = None) -> None:
    """Parse and validate every pack file without touching the database."""
    for pack in loader.discover(packs_dir):
        _echo(f"{pack.season} {pack.game}: {len(pack.components)} components, {len(pack.fittable())} fittable")


@rules_app.command("generate")
def rules_generate(
    season: int,
    openapi: Annotated[Path | None, typer.Option(help="A saved copy of the document")] = None,
    openapi_url: Annotated[str, typer.Option(help="Where to fetch it when no copy is given")] = generate.OPENAPI_URL,
    out: Annotated[Path | None, typer.Option(help="Write TOML here instead of stdout")] = None,
) -> None:
    """Generate a pack's [[components]] block from the OpenAPI document."""
    if openapi is not None:
        document = generate.load_openapi_document(openapi)
    else:
        import httpx

        response = httpx.get(openapi_url, timeout=30.0)
        response.raise_for_status()
        document = response.json()

    available = generate.available_seasons(document)
    if season not in available:
        raise typer.BadParameter(f"the document describes {available}, not {season}")

    components = generate.components_from_openapi(document, season)
    rendered = generate.render_components_toml(components)
    if out is not None:
        out.write_text(rendered, encoding="utf-8")
        _echo(f"{len(components)} components -> {out}")
    else:
        _echo(rendered)


# -------------------------------------------------------------------------------------------- ingest: FTC Events


@ingest_app.command("event")
def ingest_event(season: int, event_code: str) -> None:
    """Ingest one event, all endpoints, fully traceable to payloads."""
    from warehouse.ingest.pipeline import Ingestor

    _echo(Ingestor(make_engine()).ingest_event(season, event_code).summary())


@ingest_app.command("season-events")
def ingest_season_events(season: int, region_code: REGION = None) -> None:
    from warehouse.ingest.pipeline import Ingestor

    result = Ingestor(make_engine()).ingest_season_events(season, region_code)
    _echo(f"{result.endpoint}: {result.outcome} rows={result.rows}")


@ingest_app.command("season-teams")
def ingest_season_teams(season: int, region_code: REGION = None) -> None:
    from warehouse.ingest.pipeline import Ingestor

    result = Ingestor(make_engine()).ingest_season_teams(season, region_code)
    _echo(f"{result.endpoint}: {result.outcome} rows={result.rows}")


@ingest_app.command("region")
def ingest_region(
    season: int,
    region_code: str = "USPA",
    limit: Annotated[int | None, typer.Option(help="Stop after N events")] = None,
) -> None:
    """One region's season, every endpoint: the vertical slice."""
    from sqlalchemy import select

    from warehouse.ingest.pipeline import Ingestor
    from warehouse.schema import core

    engine = make_engine()
    ingestor = Ingestor(engine)
    ingestor.ingest_season_events(season, region_code)
    ingestor.ingest_season_teams(season, region_code)

    with engine.connect() as conn:
        codes = list(
            conn.execute(
                select(core.event.c.code)
                .where(core.event.c.season == season, core.event.c.region_code == region_code)
                .order_by(core.event.c.date_start)
            ).scalars()
        )
    for code in codes[:limit] if limit else codes:
        _echo(ingestor.ingest_event(season, code).summary())


@ingest_app.command("breakdowns")
def ingest_breakdowns(
    seasons: SEASONS = "2022,2023,2024,2025",
    limit: Annotated[int | None, typer.Option(help="Stop after N events per season")] = None,
) -> None:
    """Component-level history from FTC Events /scores. Resumable; a re-run costs a 304 per event."""
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_breakdowns(season, limit=limit)))


@ingest_app.command("awards")
def ingest_awards(seasons: SEASONS = "2022,2023,2024,2025", region_code: REGION = "USPA") -> None:
    """Backfill awards from FTC Events, one call per event."""
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_awards(season, region_code=region_code)))


@ingest_app.command("alliances")
def ingest_alliances(
    seasons: SEASONS = "2022,2023,2024,2025",
    region_code: REGION = None,
    limit: Annotated[int | None, typer.Option(help="Stop after N events per season")] = None,
) -> None:
    """Seated alliances and the selection order from FTC Events, for events with a playoff match."""
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_alliances(season, region_code=region_code, limit=limit)))


@ingest_app.command("sweep")
def ingest_sweep() -> None:
    """The weekly sweep."""
    import datetime as dt

    from warehouse.ingest.pipeline import Ingestor
    from warehouse.poll.cadence import Cadence
    from warehouse.poll.loop import Poller
    from warehouse.poll.watch import WatchList

    poller = Poller(Ingestor(make_engine()), WatchList.from_file(), Cadence.from_file())
    poller.start()
    poller.queue_sweep(dt.datetime.now(poller.tz).date())
    swept = len(poller.sweep_queue)
    while poller.sweep_queue:
        poller.sweep_one()
    _echo(f"swept {_plural(swept, 'event')}")


@ingest_app.command("status")
def ingest_status(season: int | None = None) -> None:
    """Cursor health, read through the published view like everything else."""
    sql = (
        "SELECT scope, endpoint, last_status, check_count, run_count, empty_count, last_checked_at_utc"
        " FROM pub.v_ingest_cursor"
        + (" WHERE season = :season" if season is not None else "")
        + " ORDER BY last_checked_at_utc DESC LIMIT 50"
    )
    engine = make_engine()
    with engine.connect() as conn:
        for row in conn.execute(text(sql), {"season": season} if season is not None else {}):
            _echo(json.dumps({k: str(v) for k, v in row._mapping.items()}))
    engine.dispose()


# ---------------------------------------------------------------------------------------------- ingest: FTCScout


@ingest_app.command("scout-awards")
def ingest_scout_awards(seasons: SEASONS = "2022,2023,2024,2025") -> None:
    """Worldwide awards from FTCScout, one call per season.

    FTC Events outranks it: an empty slot is filled, and a disagreeing winner is logged to raw.ingest_conflict.
    """
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_scout_awards(season)))


@ingest_app.command("alliance-roles")
def ingest_alliance_roles(seasons: SEASONS = "2022,2023,2024,2025", region_code: REGION = None) -> None:
    """Playoff alliance roles from FTCScout.

    UPDATE-only: it stamps core.match_team.alliance_role on slots already there and never creates one.
    """
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_alliance_roles(season, region_code=region_code)))


@ingest_app.command("backfill")
def ingest_backfill(seasons: SEASONS = "2022,2023,2024,2025", region_code: REGION = None) -> None:
    """Worldwide match backfill from FTCScout.

    Run `ingest season-events` first: FTCScout does not publish FIRST's eventId.
    """
    from warehouse.ingest.pipeline import Ingestor

    ing = Ingestor(make_engine())
    for season in _seasons(seasons):
        _echo(_totals(season, ing.backfill_season(season, region_code=region_code)))


# -------------------------------------------------------------------------------------------------------- derive


@derive_app.command("opr")
def derive_opr(seasons: SEASONS = "2022,2023,2024,2025") -> None:
    """Solve OPR from match data and publish it as pub.v_opr."""
    from warehouse.derive.opr import compute_season_opr

    engine = make_engine()
    for season in _seasons(seasons):
        _echo(_totals(season, compute_season_opr(engine, season)))


# ----------------------------------------------------------------------------------------------------------- ops


@ops_app.command("fit-runs")
def ops_fit_runs(
    model: Annotated[str | None, typer.Option(help="epa or pridge")] = None,
    season: int | None = None,
    version: Annotated[list[str] | None, typer.Option(help="Repeatable")] = None,
) -> None:
    """Every fit run, with its versions, scope, timestamps and rating-row count."""
    from warehouse.ops import list_fit_runs

    engine = make_engine()
    runs = list_fit_runs(engine, model=model, season=season, versions=version)
    for run in runs:
        prior = run.prior_version or "-"
        finished = run.finished_at_utc.isoformat() if run.finished_at_utc else "unfinished"
        _echo(
            f"{run.model} {run.model_version} prior={prior} {run.scope} "
            f"started={run.started_at_utc.isoformat()} finished={finished} rows={run.rating_rows}"
        )
    _echo(_plural(len(runs), "run"))


@ops_app.command("drop-model-version")
def ops_drop_model_version(
    model: Annotated[str, typer.Option(help="epa or pridge")],
    version: Annotated[list[str], typer.Option(help="Named in full; repeatable")],
    season: int | None = None,
    dry_run: Annotated[bool, typer.Option(help="Report what would go and stop")] = False,
    yes: Annotated[bool, typer.Option(help="Skip the confirmation")] = False,
) -> None:
    """Delete a model version's fit runs and, by cascade, their ratings."""
    from warehouse.ops import DropRefusedError, drop_model_version, plan_drop

    engine = make_engine()
    try:
        plan = plan_drop(engine, model, version, season=season)
        for count in plan.per_season:
            _echo(f"{count.season}: {_plural(count.runs, 'run')}, {_plural(count.rating_rows, 'rating row')}")
        _echo(
            f"{_plural(len(plan.runs), 'run')}, {_plural(plan.rating_rows, 'rating row')} at {', '.join(plan.versions)}"
        )
        if dry_run:
            return
        if not yes and not typer.confirm("delete these runs and their ratings?"):
            raise typer.Abort
        drop_model_version(engine, model, version, season=season)
    except DropRefusedError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _echo("dropped")


@app.command("poll")
def poll() -> None:
    """Poll the watched events live until stopped, starting again after a crash."""
    import signal
    import threading

    from warehouse.ingest.pipeline import Ingestor
    from warehouse.poll.cadence import Cadence
    from warehouse.poll.loop import Poller, supervise
    from warehouse.poll.watch import WatchList

    settings = load_settings()
    if not settings.ftc_events_configured():
        raise typer.BadParameter("FTC Events credentials are not set")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())

    ingestor = Ingestor(make_engine(settings), settings=settings)
    watch, cadence = WatchList.from_file(), Cadence.from_file()
    try:
        restarts = supervise(lambda: Poller(ingestor, watch, cadence), stop.is_set, cadence.restart_s, sleep=stop.wait)
    finally:
        ingestor.client.close()
    _echo(f"stopped after {_plural(restarts, 'restart')}")


@app.command("config")
def show_config() -> None:
    settings = load_settings()
    _echo(
        json.dumps(
            {
                "database_url": settings.database_url,
                "payload_root": str(settings.payload_root),
                "ftc_events_configured": settings.ftc_events_configured(),
                "default_timezone": settings.default_timezone,
            },
            indent=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    app()
