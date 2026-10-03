"""The test fixture database every other test runs against.

It finds a Postgres server, gives the session a private database on it, migrates that database to head, and hands out
connections.

The session leaves its database behind. Cleanup runs at the start of the next session instead.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from warehouse import migrate
from warehouse.config import VAR_DIR, Settings
from warehouse.db import make_engine
from warehouse.localdb import scratch_database

SCRATCH_PREFIX = "wh_test_"
STALE_AFTER_S = 3600

_SCRATCH_RE = re.compile(rf"^{SCRATCH_PREFIX}(?P<created>\d+)_[0-9a-f]+$")


def _base_url() -> str:
    configured = os.environ.get("WAREHOUSE_DATABASE_URL")
    if configured:
        return configured

    from warehouse.localdb import LocalDbUnavailableError, start

    try:
        return start(VAR_DIR / "pgdata-test", database="warehouse_test_base")
    except LocalDbUnavailableError as exc:
        pytest.skip(f"no database available: {exc}")


def _reap_stale_scratch_databases(base_url: str) -> list[str]:
    """Drop the scratch databases left behind by earlier runs."""
    # Postgres records no creation time for a database, so the name carries the epoch second it was made.
    cutoff = time.time() - STALE_AFTER_S
    engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    reaped: list[str] = []
    try:
        with engine.connect() as conn:
            names = (
                conn.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :prefix"),
                    {"prefix": f"{SCRATCH_PREFIX}%"},
                )
                .scalars()
                .all()
            )
            for name in names:
                match = _SCRATCH_RE.match(name)
                if match is None or int(match.group("created")) > cutoff:
                    continue
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                reaped.append(name)
    finally:
        engine.dispose()
    return reaped


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated, throwaway database, private to this test session."""
    base = _base_url()
    _reap_stale_scratch_databases(base)
    name = f"{SCRATCH_PREFIX}{int(time.time())}_{uuid.uuid4().hex[:8]}"
    url = scratch_database(base, name)
    migrate.upgrade(Settings(database_url=url))
    return url


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = make_engine(Settings(database_url=database_url))
    yield engine
    engine.dispose()


@pytest.fixture
def clean_engine(engine: Engine) -> Iterator[Engine]:
    """An engine whose fact tables are empty. Views survive; only rows go."""
    with engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                    "WHERE schemaname IN ('raw', 'core', 'derived')"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    yield engine
