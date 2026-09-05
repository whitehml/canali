"""The schema, as SQLAlchemy metadata."""

from __future__ import annotations

from warehouse.schema import core, raw
from warehouse.schema.types import metadata

__all__ = ["core", "metadata", "raw"]
