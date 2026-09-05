"""Checks if the migration is stale against the schema."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from warehouse.db import SCHEMAS
from warehouse.schema import metadata

pytestmark = pytest.mark.db


def _include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    # Autogenerate sees every table on the server, including the ones Alembic and pgserver put outside the four.
    return not (type_ == "table" and getattr(obj, "schema", None) not in SCHEMAS)


def test_the_migration_head_leaves_autogenerate_nothing_to_write(engine: Engine) -> None:
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "target_metadata": metadata,
                "include_schemas": True,
                "include_object": _include_object,
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        differences = compare_metadata(context, metadata)

    assert differences == [], "\n".join(repr(d) for d in differences)
