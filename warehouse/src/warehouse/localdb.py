"""A Postgres instance for local development and tests, without root.

On a developer box the ``pgserver`` package ships Postgres binaries as a wheel. Nothing else in ``src/`` knows the
difference, since everything reaches Postgres over the network protocol.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("var/pgdata")


class LocalDbUnavailableError(RuntimeError):
    """Raised when neither a configured Postgres nor ``pgserver`` is available."""


@functools.cache
def _server(data_dir: str, cleanup_mode: str | None) -> Any:
    try:
        import pgserver
    except ImportError as exc:
        raise LocalDbUnavailableError(
            "no Postgres URL configured and pgserver is not installed; "
            "run `uv sync --group local-db` or set WAREHOUSE_DATABASE_URL"
        ) from exc

    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(path, cleanup_mode=cleanup_mode)


def start(
    data_dir: Path = DEFAULT_DATA_DIR,
    database: str = "warehouse",
    cleanup_mode: str | None = None,
) -> str:
    server = _server(str(data_dir.resolve()), cleanup_mode)
    admin_url = _sqlalchemy_url(server.get_uri())
    ensure_database(admin_url, database)
    return _sqlalchemy_url(server.get_uri(database=database))


def _sqlalchemy_url(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def ensure_database(admin_url: str, database: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        engine.dispose()


def stop(data_dir: Path = DEFAULT_DATA_DIR) -> None:
    server = _server(str(data_dir.resolve()), None)
    server.cleanup()
    _server.cache_clear()


def scratch_database(base_url: str, name: str) -> str:
    """Return a URL for a freshly created empty database on the same server.

    Exists for tests, and nothing in ``src/`` calls it. Isolation needs a private database per session.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return make_url(base_url).set(database=name).render_as_string(hide_password=False)
