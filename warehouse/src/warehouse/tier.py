"""Event tiers.

======  ==============================================================
REGULAR Qualifier, League Meet
RCMP    Super Qualifier, League Tournament, Championship
CMP     FIRST Championship
======  ==============================================================
"""

from __future__ import annotations

from enum import StrEnum


class EventTier(StrEnum):
    """Which rung of the advancement ladder an event sits on."""

    REGULAR = "regular"

    RCMP = "rcmp"
    """Regional championship: Super Qualifier, League Tournament and Championship, all three being
    advancement events played by a field that qualified for them."""

    CMP = "cmp"
    """The world championship."""


_TIER_BY_TYPE: dict[str, EventTier] = {
    "Qualifier": EventTier.REGULAR,
    "League Meet": EventTier.REGULAR,
    "Super Qualifier": EventTier.RCMP,
    "League Tournament": EventTier.RCMP,
    "Championship": EventTier.RCMP,
    "FIRST Championship": EventTier.CMP,
}

RATED_EVENT_TYPES: tuple[str, ...] = tuple(_TIER_BY_TYPE)


def tier_for(event_type: str | None) -> EventTier | None:
    if event_type is None:
        return None
    return _TIER_BY_TYPE.get(event_type)
