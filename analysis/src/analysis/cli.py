"""``analysis`` command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
import typer

from analysis import comparison
from analysis.comparison import AllianceKey, Forecast
from analysis.forecasts import season_forecasts
from analysis.report import NAMES, EpaRun, Provenance, build_sections, provenance, render
from warehouse.client import Warehouse

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = structlog.get_logger("analysis.cli")

ALL_SEASONS = "2022,2023,2024,2025"


def _seasons(value: str) -> list[int]:
    return [int(s) for s in value.split(",") if s.strip()]


@app.command()
def head_to_head(
    *,
    epa_version: Annotated[str, typer.Option(help="The EPA model version")],
    pridge_version: Annotated[str, typer.Option(help="The pRidge version")],
    seasons: Annotated[str, typer.Option(help="Comma-separated")] = ALL_SEASONS,
    identified_only: Annotated[bool, typer.Option(help="Score OPR only where its design has full column rank")] = False,
    out: Annotated[Path | None, typer.Option(help="Write the report here instead of printing it")] = None,
) -> None:
    """Compare pRidge, EPA and OPR on the alliances all three predicted, by match index and by round."""
    runs: list[EpaRun] = []
    forecasts: dict[str, list[Forecast]] = {name: [] for name in NAMES}
    rounds: dict[AllianceKey, int] = {}
    with Warehouse() as warehouse:
        for season in _seasons(seasons):
            try:
                found = provenance(warehouse, epa_version, pridge_version, [season])
            except LookupError as error:
                log.warning("analysis.season_skipped", season=season, reason=str(error))
                continue
            except ValueError as error:
                raise typer.BadParameter(str(error), param_hint="--pridge-version") from error
            runs.extend(found.epa_runs)
            result = season_forecasts(warehouse, season, epa_version, identified_only=identified_only)
            for name, rows in result.models.items():
                forecasts[name].extend(rows)
            rounds.update(result.rounds)
    if not runs:
        raise typer.BadParameter(f"no season has a completed {epa_version} batch run", param_hint="--epa-version")

    shared = comparison.shared_rows(forecasts)
    report = render(Provenance(epa_version, pridge_version, tuple(runs)), shared, build_sections(shared, rounds))
    if out is None:
        typer.echo(report)
    else:
        out.write_text(report)
        typer.echo(f"wrote {out}")
