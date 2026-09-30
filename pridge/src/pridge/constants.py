"""Versioned pRidge constants, derived in `docs/methodology.md`.

`MODEL_VERSION` names a set of ratings that may be compared with one another. Bump it whenever a rating computed now
would not be comparable to one already stored under that name, whether the cause is a constant here or a code path. Do
not bump it for a change of prior source, since every fit run records which prior it used.
"""

from warehouse.tier import EventTier, tier_for

MODEL_VERSION = "pridge-0.4.0"

LAMBDA_REGULAR = 1.28
LAMBDA_CHAMPIONSHIP = 1.93

_LAMBDA_BY_TIER = {
    EventTier.REGULAR: LAMBDA_REGULAR,
    EventTier.RCMP: LAMBDA_CHAMPIONSHIP,
    EventTier.CMP: LAMBDA_CHAMPIONSHIP,
}


def lambda_for(event_type: str | None) -> float:
    """The ridge penalty for an event of this type. A type with no tier is not rated and has none."""
    tier = tier_for(event_type)
    if tier is None:
        raise ValueError(f"no lambda for event type {event_type!r}")
    return _LAMBDA_BY_TIER[tier]
