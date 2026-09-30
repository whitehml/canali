"""The two models as forecasts over the same alliance rows.

Both predict the alliances of each qualification match after the first from the ratings held at the match before it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import pairwise

from analysis.comparison import Forecast
from epa.pipeline import MODEL_NAME as EPA_MODEL
from pridge import evaluate as pridge_evaluate
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
    qualification = [row for row in rows if row.level == "QUALIFICATION"]
    ordinals = sorted({row.event_match_ordinal for row in qualification})
    by_ordinal: dict[int, dict[uuid.UUID, list[MatchRow]]] = defaultdict(lambda: defaultdict(list))
    for row in qualification:
        by_ordinal[row.event_match_ordinal][row.match_id].append(row)

    held = dict(pre_event)
    pending = sorted(match_grain.items())
    cursor = 0
    out: list[Forecast] = []
    for k, successor in pairwise(ordinals):
        while cursor < len(pending) and pending[cursor][0] <= k:
            held.update(pending[cursor][1])
            cursor += 1
        for pair in by_ordinal[successor].values():
            if len(pair) != 2 or not all(_rated(row, held) for row in pair):
                continue
            out.extend(
                Forecast(
                    match_id=row.match_id,
                    alliance=row.alliance,
                    season=row.season,
                    event_id=row.event_id,
                    event_type=row.event_type,
                    as_of_match=k,
                    predicted=sum(held[team] for team in row.rated_teams()),
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
