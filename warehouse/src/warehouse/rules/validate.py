"""Validate a ``match_breakdown`` payload against its season's pack.

Shape only. An unknown key is a warning, but a declared component with the wrong type is an error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from warehouse.rules.model import Component

IGNORED_KEYS = frozenset({"team"})
"""Keys every score model carries that are not components.

``team`` is always 0 in a two-team alliance, a remnant of the single-player remote score models. Listed here so it is
not reported as undeclared.
"""


class BreakdownValidationError(ValueError):
    """A breakdown disagrees with the types its pack declares."""


@dataclass(slots=True)
class ValidationResult:
    """What one breakdown's shape check found."""

    errors: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self, context: str) -> None:
        if self.errors:
            raise BreakdownValidationError(f"{context}: " + "; ".join(self.errors))


def _kind_ok(kind: str, value: Any) -> bool:
    match kind:
        case "numeric":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "enum":
            return isinstance(value, str)
        case "array":
            return isinstance(value, list)
    return False


def validate_breakdown(breakdown: Mapping[str, Any], components: Sequence[Component]) -> ValidationResult:
    """Check a breakdown's values against the kinds its pack declares."""
    result = ValidationResult()
    declared = {c.name: c for c in components}

    for name, component in declared.items():
        value = breakdown.get(name)
        if value is None:
            continue
        if not _kind_ok(component.kind, value):
            result.errors.append(f"{name}: expected {component.kind}, got {type(value).__name__}")

    result.unknown_keys = sorted(set(breakdown) - set(declared) - IGNORED_KEYS)
    return result
