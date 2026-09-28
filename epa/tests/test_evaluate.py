"""Next-match evaluation: the fit surface."""

from __future__ import annotations

from epa.evaluate import Prediction, fit_surface
from warehouse.tier import RATED_EVENT_TYPES


def _prediction(event_type: str | None, *, elimination: bool = False) -> Prediction:
    return Prediction(
        season=2025,
        team_numbers=(1, 2),
        predicted=100.0,
        actual=90.0,
        matches_played=5,
        event_type=event_type,
        is_elimination=elimination,
    )


def test_the_fit_surface_drops_the_bracket_and_untiered_rows() -> None:
    rated = [_prediction(event_type) for event_type in RATED_EVENT_TYPES]
    excluded = [_prediction("Championship", elimination=True), _prediction("Off-Season"), _prediction(None)]
    assert fit_surface(rated + excluded) == rated
