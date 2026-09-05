"""Alembic environment.

Two things beyond the Alembic default:

1. ``include_schemas=True``, so autogenerate sees all four namespaces rather than only ``public``.
2. Every migration is bracketed by a view rebuild: views are dropped before it runs and the ``views/`` directory is
   re-run afterwards, in the same transaction.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, Engine, text

from warehouse import views as view_builder
from warehouse.config import load_settings
from warehouse.db import SCHEMAS, make_engine
from warehouse.schema import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    # Views are files, not migrations. Never let autogenerate touch them.
    if type_ == "table" and getattr(obj, "schema", None) not in SCHEMAS:
        return False
    return True


def _ensure_schemas(connection: Connection) -> None:
    for schema in SCHEMAS:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        _ensure_schemas(connection)
        view_builder.drop_all_views(connection)
        context.run_migrations()
        view_builder.rebuild(connection)


def run_migrations_offline() -> None:
    context.configure(
        url=load_settings().database_url,
        target_metadata=target_metadata,
        include_schemas=True,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if isinstance(connectable, Connection):
        _run(connectable)
        return

    url = config.attributes.get("url") or load_settings().database_url
    engine = make_engine_for(url)
    try:
        with engine.connect() as connection:
            _run(connection)
            connection.commit()
    finally:
        engine.dispose()


def make_engine_for(url: str) -> Engine:
    from warehouse.config import Settings

    return make_engine(Settings(database_url=url))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
