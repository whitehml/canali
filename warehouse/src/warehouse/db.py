"""Engine and session construction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine

from warehouse.config import Settings, load_settings

SCHEMAS: tuple[str, ...] = ("raw", "core", "derived", "pub")


def make_engine(settings: Settings | None = None, **kwargs: Any) -> Engine:
    settings = settings or load_settings()
    return create_engine(settings.database_url, future=True, **kwargs)


@contextmanager
def begin(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn
