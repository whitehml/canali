"""Event tiers, and the ranking-point levels that set bonus RP thresholds.

======  ==============================================================
REGULAR Qualifier, League Meet
RCMP    Super Qualifier, League Tournament, Championship
CMP     FIRST Championship
======  ==============================================================

RP levels follow the game manual and differ from the tiers: Super Qualifiers and League Tournaments use REG.

======  ==============================================================
REG     every type not listed below
RCMP    Championship
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


class RpLevel(StrEnum):
    """Which set of bonus RP thresholds an event plays to."""

    REG = "REG"
    RCMP = "RCMP"
    CMP = "CMP"


_RP_LEVEL_BY_TYPE: dict[str, RpLevel] = {
    "Championship": RpLevel.RCMP,
    "FIRST Championship": RpLevel.CMP,
}


def rp_level_for(event_type: str | None) -> RpLevel:
    return _RP_LEVEL_BY_TYPE.get(event_type or "", RpLevel.REG)
