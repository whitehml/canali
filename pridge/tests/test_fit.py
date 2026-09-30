"""Test the per-event fit."""

from __future__ import annotations

import pytest

from pridge.constants import lambda_for
from pridge.fit import fit_event
from pridge.testing.synthetic import synthetic_event


def test_component_ratings_sum_to_the_total_rating() -> None:
    event = synthetic_event()
    result = fit_event(event.rows, event.prior, component_responses=event.component_responses)
    assert set(result.components) == set(event.component_responses)
    assert result.component_sum_error() < 1e-8


@pytest.mark.parametrize("event_type", ["Qualifier", "League Tournament", "FIRST Championship"])
def test_lambda_is_resolved_from_the_event_type_unless_given(event_type: str) -> None:
    event = synthetic_event(event_type=event_type)
    assert fit_event(event.rows, event.prior).lam == lambda_for(event_type)
    assert fit_event(event.rows, event.prior, lam=3.0).lam == 3.0


def test_an_event_type_with_no_tier_has_no_lambda() -> None:
    event = synthetic_event(event_type="Off-Season")
    with pytest.raises(ValueError, match="Off-Season"):
        fit_event(event.rows, event.prior)
