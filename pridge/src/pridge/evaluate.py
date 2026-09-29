"""Next-match prediction.

Fit an event at match k, predict the alliances of match k+1.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from pridge.constants import FIXED_LAMBDA
from pridge.fit import fit_event
from pridge.prior import Prior
from warehouse.client import MatchRow


@dataclass(frozen=True, slots=True)
class Prediction:
    """One held-out alliance row: what was predicted from the fit at `as_of_match`, and what happened."""

    season: int
    event_id: uuid.UUID
    event_type: str | None
    match_id: uuid.UUID
    alliance: str
    team_numbers: tuple[int, ...]
    as_of_match: int
    predicted: float
    opponent_predicted: float
    actual: float

    @property
    def error(self) -> float:
        return self.predicted - self.actual

    @property
    def predicted_margin(self) -> float:
        return self.predicted - self.opponent_predicted


def next_match_predictions(rows: Sequence[MatchRow], prior: Prior, *, lam: float = FIXED_LAMBDA) -> list[Prediction]:
    """Predict every qualification match of one event after the first, from the fit at the match before it."""
    qualification = [row for row in rows if row.level == "QUALIFICATION"]
    ordinals = sorted({row.event_match_ordinal for row in qualification})
    out: list[Prediction] = []

    for k, successor in pairwise(ordinals):
        history = [row for row in qualification if row.event_match_ordinal <= k]
        try:
            fitted = fit_event(history, prior, as_of_match=k, lam=lam)
        except ValueError:
            continue
        ratings = fitted.ratings()

        by_match: dict[uuid.UUID, list[MatchRow]] = defaultdict(list)
        for row in qualification:
            if row.event_match_ordinal == successor:
                by_match[row.match_id].append(row)

        for pair in by_match.values():
            if len(pair) != 2 or not all(_rated(row, ratings) for row in pair):
                continue
            hats = [sum(ratings[team] for team in row.rated_teams()) for row in pair]
            for row, hat, opponent_hat in zip(pair, hats, reversed(hats), strict=True):
                out.append(
                    Prediction(
                        season=row.season,
                        event_id=row.event_id,
                        event_type=row.event_type,
                        match_id=row.match_id,
                        alliance=row.alliance,
                        team_numbers=row.rated_teams(),
                        as_of_match=k,
                        predicted=hat,
                        opponent_predicted=opponent_hat,
                        actual=row.score_no_foul,
                    )
                )
    return out


def mse_by_index(predictions: Sequence[Prediction]) -> dict[int, tuple[float, int]]:
    """Mean squared error and prediction count at each fit index."""
    errors: dict[int, list[float]] = defaultdict(list)
    for p in predictions:
        errors[p.as_of_match].append(p.error * p.error)
    return {k: (statistics.fmean(values), len(values)) for k, values in sorted(errors.items())}


def _rated(row: MatchRow, ratings: dict[int, float]) -> bool:
    teams = row.rated_teams()
    return bool(teams) and all(team in ratings for team in teams)
