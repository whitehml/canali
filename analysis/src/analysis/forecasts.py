"""The two models as forecasts over the same alliance rows.

Both predict the alliances of each qualification match after the first from the ratings held at the match before it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise

import numpy as np

from analysis.comparison import Forecast
from epa.pipeline import MODEL_NAME as EPA_MODEL
from pridge import design
from pridge import evaluate as pridge_evaluate
from pridge.pipeline import load_event
from pridge.prior import EpaPriorSource
from warehouse.client import MatchRow, Warehouse
from warehouse.tier import tier_for


def pridge_forecasts(predictions: Sequence[pridge_evaluate.Prediction]) -> list[Forecast]:
    """pRidge next-match predictions as forecasts."""
    return [
        Forecast(
            match_id=p.match_id,
            alliance=p.alliance,
            season=p.season,
            event_id=p.event_id,
            event_type=p.event_type,
            as_of_match=p.as_of_match,
            predicted=p.predicted,
            actual=p.actual,
        )
        for p in predictions
    ]


def epa_forecasts(
    rows: Sequence[MatchRow],
    pre_event: Mapping[int, float],
    match_grain: Mapping[int, Mapping[int, float]],
) -> list[Forecast]:
    """EPA next-match forecasts for one event from its stored ratings."""
    held = dict(pre_event)
    pending = sorted(match_grain.items())
    cursor = 0

    def ratings_at(k: int) -> Mapping[int, float]:
        nonlocal cursor
        while cursor < len(pending) and pending[cursor][0] <= k:
            held.update(pending[cursor][1])
            cursor += 1
        return held

    return _next_match_forecasts(rows, ratings_at)


def opr_forecasts(rows: Sequence[MatchRow], *, identified_only: bool = False) -> list[Forecast]:
    """OPR next-match forecasts for one event, refit on the matches played so far at every index.

    With `identified_only`, an index whose design lacks full column rank is left out.
    """

    def ratings_at(k: int) -> Mapping[int, float] | None:
        try:
            built = design.build(rows, as_of_match=k)
        except design.DesignError:
            return None
        solution, _residuals, rank, _singular = np.linalg.lstsq(built.X, built.y, rcond=None)
        if identified_only and rank < built.n_teams:
            return None
        return dict(zip(built.team_numbers, (float(v) for v in solution), strict=True))

    return _next_match_forecasts(rows, ratings_at)


def _next_match_forecasts(
    rows: Sequence[MatchRow], ratings_at: Callable[[int], Mapping[int, float] | None]
) -> list[Forecast]:
    """Predict every qualification match after the first from `ratings_at` the match before it.

    `ratings_at` is called once per index in ascending order, and may decline an index with None.
    """
    qualification = [row for row in rows if row.level == "QUALIFICATION"]
    ordinals = sorted({row.event_match_ordinal for row in qualification})
    by_ordinal: dict[int, dict[uuid.UUID, list[MatchRow]]] = defaultdict(lambda: defaultdict(list))
    for row in qualification:
        by_ordinal[row.event_match_ordinal][row.match_id].append(row)

    out: list[Forecast] = []
    for k, successor in pairwise(ordinals):
        ratings = ratings_at(k)
        if ratings is None:
            continue
        for pair in by_ordinal[successor].values():
            if len(pair) != 2 or not all(_rated(row, ratings) for row in pair):
                continue
            out.extend(
                Forecast(
                    match_id=row.match_id,
                    alliance=row.alliance,
                    season=row.season,
                    event_id=row.event_id,
                    event_type=row.event_type,
                    as_of_match=k,
                    predicted=sum(ratings[team] for team in row.rated_teams()),
                    actual=row.score_no_foul,
                )
                for row in pair
            )
    return out


def epa_season_forecasts(warehouse: Warehouse, season: int, epa_version: str) -> list[Forecast]:
    """EPA forecasts for every rated event of a season."""
    run = warehouse.batch_fit_run(EPA_MODEL, season, epa_version)
    if run is None:
        raise LookupError(f"no completed {epa_version} batch run for season {season}")
    out: list[Forecast] = []
    for event in warehouse.events(season):
        if tier_for(event.event_type) is None:
            continue
        pre_event = {
            r.team_number: r.epa_scaled for r in warehouse.pre_event_epa(event.event_id, epa_version, fit_run_id=run)
        }
        if not pre_event:
            continue
        match_grain = warehouse.match_grain_epa(event.event_id, epa_version, fit_run_id=run)
        out.extend(epa_forecasts(warehouse.event_matches(event.event_id), pre_event, match_grain))
    return out


def _rated(row: MatchRow, held: Mapping[int, float]) -> bool:
    teams = row.rated_teams()
    return bool(teams) and all(team in held for team in teams)


def season_forecasts(
    warehouse: Warehouse, season: int, epa_version: str, *, identified_only: bool = False
) -> dict[str, list[Forecast]]:
    """All models' forecasts for every rated event of a season.

    pRidge is refit from the installed engine with the named EPA version as its prior, and OPR is refit at every index.
    """
    source = EpaPriorSource.for_season(warehouse, season, epa_version)
    pridge: list[Forecast] = []
    opr: list[Forecast] = []
    for event in warehouse.events(season):
        if tier_for(event.event_type) is None:
            continue
        try:
            inputs = load_event(warehouse, event.event_id, season, source, partition=())
        except (LookupError, ValueError):
            continue
        pridge.extend(pridge_forecasts(pridge_evaluate.next_match_predictions(inputs.rows, inputs.prior)))
        opr.extend(opr_forecasts(inputs.rows, identified_only=identified_only))
    return {
        "pridge": pridge,
        "epa": epa_season_forecasts(warehouse, season, epa_version),
        "opr": opr,
    }
