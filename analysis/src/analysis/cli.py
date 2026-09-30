"""``analysis`` command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
import typer

from analysis import comparison
from analysis.comparison import AllianceKey, Forecast
from analysis.forecasts import season_forecasts
from analysis.report import EpaRun, Provenance, Section, provenance, render
from analysis.rounds import season_rounds
from warehouse.client import Warehouse

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
log = structlog.get_logger("analysis.cli")

ALL_SEASONS = "2022,2023,2024,2025"
PAIRS = (("pridge", "epa"), ("pridge", "opr"), ("epa", "opr"))
NAMES = {"pridge": "pRidge", "epa": "EPA", "opr": "OPR"}


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
            for name, rows in season_forecasts(warehouse, season, epa_version, identified_only=identified_only).items():
                forecasts[name].extend(rows)
            rounds.update(season_rounds(warehouse, season))
    if not runs:
        raise typer.BadParameter(f"no season has a completed {epa_version} batch run", param_hint="--epa-version")

    shared = comparison.shared_rows(forecasts)
    sections = [
        Section(f"{NAMES[a]} against {NAMES[b]}, by match index", "match index", comparison.by_match(shared, a, b))
        for a, b in PAIRS
    ] + [
        Section(f"{NAMES[a]} against {NAMES[b]}, by round", "round", comparison.by_round(shared, rounds, a, b))
        for a, b in PAIRS
    ]
    report = render(Provenance(epa_version, pridge_version, tuple(runs)), shared, sections)
    if out is None:
        typer.echo(report)
    else:
        out.write_text(report)
        typer.echo(f"wrote {out}")
