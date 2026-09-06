"""Generate ``rule_pack_component`` rows from the OpenAPI document.

FIRST publishes a json description of the FTC Events API, including a typed per-season score model,
``ScoreDetailAllianceModel_2020`` through ``_2025``.

The document is at :data:`OPENAPI_URL`. Note the host: it is served by ``ftc-events``, not by the ``ftc-api`` host
the data endpoints use, which redirects every documentation path to a 404.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from warehouse.rules.model import Component, ComponentKind, to_column_name

OPENAPI_URL = "https://ftc-events.firstinspires.org/swagger/v2.0/swagger.json"
"""Where FIRST serves the OpenAPI document. Unauthenticated. ``/api-docs`` on the same host renders it."""

ALLIANCE_MODEL = re.compile(r"^ScoreDetailAllianceModel_(\d{4})$")

_SUBTOTAL_SUFFIX = re.compile(r"(Points|Score)$")
_PER_ROBOT = re.compile(r"^robot[12]")

_DERIVED_NAMES = frozenset(
    {
        "totalPoints",
        "totalPointsNp",
        "adjustPoints",
        "prePenaltyTotal",
        "preFoulTotal",
        "penaltyPointsCommitted",
        "foulPointsCommitted",
    }
)
"""Quantities that are functions of other components in the same payload."""

_NOT_A_COMPONENT = frozenset({"alliance", "team"})
"""Properties of the alliance score model that score nothing.

``alliance`` is the side label. ``team`` is always 0 in a two-team alliance, a remnant of the single-player remote
score models.
"""


def _schemas(document: Mapping[str, Any]) -> Mapping[str, Any]:
    schemas: Mapping[str, Any] = document.get("components", {}).get("schemas", {})
    return schemas


def classify_schema(schema: Mapping[str, Any]) -> ComponentKind:
    """Map a JSON Schema property to one of numeric, boolean, enum, or array."""
    if "$ref" in schema or "enum" in schema or schema.get("allOf"):
        return "enum"
    match schema.get("type"):
        case "boolean":
            return "boolean"
        case "integer" | "number":
            return "numeric"
        case "array":
            return "array"
        case "string":
            return "enum"
    return "enum"


def _build(name: str, kind: ComponentKind) -> Component:
    is_derived = name in _DERIVED_NAMES
    return Component(
        name=name,
        # Per-robot fields live in the alliance blob but describe one robot.
        level="team" if _PER_ROBOT.match(name) else "alliance",
        kind=kind,
        is_subtotal=bool(_SUBTOTAL_SUFFIX.search(name)) and not is_derived,
        is_derived=is_derived,
        column_name=to_column_name(name),
    )


def components_from_openapi(document: Mapping[str, Any], season: int) -> list[Component]:
    """Read a season's components off its published alliance score model."""
    schemas = _schemas(document)
    model_name = f"ScoreDetailAllianceModel_{season}"
    model = schemas.get(model_name)
    if model is None:
        available = ", ".join(sorted(n for n in schemas if ALLIANCE_MODEL.match(n)))
        raise KeyError(f"{model_name} not found. Available: {available}")
    out = [
        _build(name, classify_schema(prop))
        for name, prop in (model.get("properties") or {}).items()
        if name not in _NOT_A_COMPONENT
    ]
    return sorted(out, key=lambda c: c.name)


def available_seasons(document: Mapping[str, Any]) -> list[int]:
    """Seasons the document carries an alliance score model for."""
    return sorted(int(m.group(1)) for name in _schemas(document) if (m := ALLIANCE_MODEL.match(name)) is not None)


def render_components_toml(components: Iterable[Component]) -> str:
    """Emit the ``[[components]]`` block of a pack file."""
    lines: list[str] = []
    for c in components:
        lines.append("[[components]]")
        lines.append(f'name = "{c.name}"')
        lines.append(f'level = "{c.level}"')
        lines.append(f'kind = "{c.kind}"')
        if c.is_subtotal:
            lines.append("is_subtotal = true")
        if c.is_derived:
            lines.append("is_derived = true")
        lines.append("")
    return "\n".join(lines)


def load_openapi_document(path: Path) -> Mapping[str, Any]:
    """Read a saved copy of the OpenAPI document."""
    data: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
