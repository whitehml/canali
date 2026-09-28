"""Event tiers: the advancement ladder over FIRST's event types."""

from __future__ import annotations

import pytest

from warehouse.tier import tier_for


@pytest.mark.parametrize("event_type", [None, "Off-Season", "Premier", "Scrimmage"])
def test_an_unclassified_type_gets_no_tier_rather_than_a_default(event_type: str | None) -> None:
    assert tier_for(event_type) is None
