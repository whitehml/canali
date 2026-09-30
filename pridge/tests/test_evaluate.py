"""Test next-match prediction."""

from __future__ import annotations

import dataclasses
import uuid
from collections import defaultdict

import numpy as np

from pridge.evaluate import Prediction, next_match_predictions
from pridge.prior import EpaPrior
from pridge.testing.synthetic import DEFAULT_EVENT, match_row, synthetic_event
from warehouse.client import MatchRow


def test_a_prediction_never_depends_on_a_match_played_after_the_fit() -> None:
    event = synthetic_event(n_matches=14)
    cutoff = 8
    later = [
        dataclasses.replace(row, score_no_foul=row.score_no_foul + 1000.0) if row.event_match_ordinal > cutoff else row
        for row in event.rows
    ]

    def by_key(rows: list[MatchRow]) -> dict[tuple[uuid.UUID, str], Prediction]:
        predictions = next_match_predictions(rows, event.prior)
        return {(p.match_id, p.alliance): p for p in predictions if p.as_of_match <= cutoff}

    before, after = by_key(event.rows), by_key(later)
    assert before.keys() == after.keys()
    for key, prediction in before.items():
        np.testing.assert_allclose(prediction.predicted, after[key].predicted)
    # The held-out score itself did change, so the comparison above is not vacuous.
    assert any(before[key].actual != after[key].actual for key in before if before[key].as_of_match == cutoff)


def test_only_matches_after_the_first_with_every_team_already_seen_are_predicted() -> None:
    rows = [
        match_row(1, "RED", (1, 2), 50.0),
        match_row(1, "BLUE", (3, 4), 50.0),
        match_row(2, "RED", (1, 2), 50.0),
        match_row(2, "BLUE", (5, 6), 50.0),
        match_row(3, "RED", (1, 3), 50.0),
        match_row(3, "BLUE", (2, 4), 50.0),
        match_row(4, "RED", (5, 6), 50.0),
        match_row(4, "BLUE", (3, 4), 50.0),
        match_row(5, "RED", (1, 2), 50.0, level="PLAYOFF"),
        match_row(5, "BLUE", (3, 4), 50.0, level="PLAYOFF"),
    ]
    prior = EpaPrior(DEFAULT_EVENT, total=dict.fromkeys(range(1, 7), 25.0), components={})
    predictions = next_match_predictions(rows, prior)

    ordinal = {row.match_id: row.event_match_ordinal for row in rows}
    assert len({(p.match_id, p.alliance) for p in predictions}) == len(predictions)
    # Match 2 has two teams no earlier match involved, so neither alliance is predicted. The playoff match is ignored.
    assert {ordinal[p.match_id] for p in predictions} == {3, 4}
    assert all(p.as_of_match == ordinal[p.match_id] - 1 for p in predictions)


def test_each_alliance_is_paired_with_its_opponent() -> None:
    event = synthetic_event(n_matches=10)
    by_match: dict[uuid.UUID, dict[str, Prediction]] = defaultdict(dict)
    for p in next_match_predictions(event.rows, event.prior):
        by_match[p.match_id][p.alliance] = p
    assert by_match

    for pair in by_match.values():
        red, blue = pair["RED"], pair["BLUE"]
        np.testing.assert_allclose(red.opponent_predicted, blue.predicted)
        np.testing.assert_allclose(blue.opponent_predicted, red.predicted)
        for p in (red, blue):
            np.testing.assert_allclose(p.error, p.predicted - p.actual)
            np.testing.assert_allclose(p.predicted_margin, p.predicted - p.opponent_predicted)
