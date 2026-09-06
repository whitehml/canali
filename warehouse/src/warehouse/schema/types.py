"""Shared column types, enums and the naming convention every table is built on.

Enum values are the API's own spellings wherever the API has one.
"""

from __future__ import annotations

from sqlalchemy import Column, Enum, MetaData
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_N_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s",
        "pk": "pk_%(table_name)s",
    }
)

MATCH_LEVELS: tuple[str, ...] = (
    "PRACTICE",
    "QUALIFICATION",
    "SEMIFINAL",
    "FINAL",
    "PLAYOFF",
    "OTHER",
)
"""``FTCEventLevel`` in full: The ``tournamentLevel`` query parameter accepts only ``qual`` and
``playoff``, but responses carry all six. Seasons through CENTERSTAGE use SEMIFINAL/FINAL; INTO THE DEEP and later
use PLAYOFF.
"""

ALLIANCES: tuple[str, ...] = ("RED", "BLUE")

ALLIANCE_ROLES: tuple[str, ...] = ("Captain", "FirstPick", "SecondPick")

ALLIANCE_PICK_ACTIONS: tuple[str, ...] = ("CAPTAIN", "ACCEPT", "DECLINE", "REMOVE")

INGEST_SCOPES: tuple[str, ...] = ("season", "event")

COMPONENT_KINDS: tuple[str, ...] = ("numeric", "boolean", "enum", "array")

COMPONENT_LEVELS: tuple[str, ...] = ("alliance", "team")


def pg_enum(name: str, values: tuple[str, ...]) -> Enum:
    return Enum(*values, name=name, schema="core", create_type=True, validate_strings=True)


match_level = pg_enum("match_level", MATCH_LEVELS)
alliance = pg_enum("alliance", ALLIANCES)
alliance_role = pg_enum("alliance_role", ALLIANCE_ROLES)
alliance_pick_action = pg_enum("alliance_pick_action", ALLIANCE_PICK_ACTIONS)
component_kind = pg_enum("component_kind", COMPONENT_KINDS)
component_level = pg_enum("component_level", COMPONENT_LEVELS)
ingest_scope = Enum(*INGEST_SCOPES, name="ingest_scope", schema="raw", validate_strings=True)


def utc(name: str, **kw: object) -> Column[object]:
    """A UTC instant, derived from venue-local time."""
    return Column(name, TIMESTAMP(timezone=True), **kw)  # type: ignore[arg-type]


def local(name: str, **kw: object) -> Column[object]:
    """A venue wall-clock time. Stores the digits verbatim."""
    return Column(name, TIMESTAMP(timezone=False), **kw)  # type: ignore[arg-type]


def jsonb() -> JSONB:
    """JSONB that stores a Python ``None`` as SQL NULL.

    Without ``none_as_null`` the default serializes it to a JSON ``null``, which no ``IS NULL`` predicate matches.
    """
    return JSONB(none_as_null=True)
