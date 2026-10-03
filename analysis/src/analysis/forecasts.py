"""The two models as forecasts over the same alliance rows.

Both predict the alliances of each qualification match after the first from the ratings held at the match before it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from analysis.comparison import AllianceKey, Forecast
from analysis.rounds import event_rounds
from pridge import design
from pridge import evaluate as pridge_evaluate
from pridge.prior import EpaPriorSource
from warehouse.client import MatchRow, Warehouse
from warehouse.schema.types import QUALIFICATION_MATCHES
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
    qualification = [row for row in rows if row.level in QUALIFICATION_MATCHES]
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


def _rated(row: MatchRow, held: Mapping[int, float]) -> bool:
    teams = row.rated_teams()
    return bool(teams) and all(team in held for team in teams)


@dataclass(frozen=True, slots=True)
class SeasonForecasts:
    """Every model's forecasts for a season, and the round of each alliance."""

    models: dict[str, list[Forecast]]
    rounds: dict[AllianceKey, int]


def season_forecasts(
    warehouse: Warehouse, season: int, epa_version: str, *, identified_only: bool = False
) -> SeasonForecasts:
    """All models' forecasts for every rated event of a season."""
    source = EpaPriorSource.for_season(warehouse, season, epa_version)
    models: dict[str, list[Forecast]] = {"pridge": [], "epa": [], "opr": []}
    rounds: dict[AllianceKey, int] = {}
    for event in warehouse.events(season):
        if tier_for(event.event_type) is None:
            continue
        match_grain = warehouse.match_grain_epa(event.event_id, epa_version, fit_run_id=source.run)
        if not match_grain:
            continue
        rows = warehouse.event_matches(event.event_id)
        try:
            prior = source.load(event.event_id)
        except LookupError:
            continue
        models["pridge"].extend(pridge_forecasts(pridge_evaluate.next_match_predictions(rows, prior)))
        models["epa"].extend(epa_forecasts(rows, prior.total, match_grain))
        models["opr"].extend(opr_forecasts(rows, identified_only=identified_only))
        rounds.update(event_rounds(rows))
    return SeasonForecasts(models, rounds)
