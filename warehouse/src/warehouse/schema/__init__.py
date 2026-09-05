"""The schema, as SQLAlchemy metadata."""

from __future__ import annotations

from warehouse.schema import core, derived, raw
from warehouse.schema.types import metadata

__all__ = ["core", "derived", "metadata", "raw"]
