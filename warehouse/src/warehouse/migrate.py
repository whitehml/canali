"""Programmatic access to the migration harness.

Resolves ``alembic.ini`` and ``migrations/`` from this file, so it runs from any working directory, and takes a
``Settings``, so it can target a database whose URL is absent from the environment.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from warehouse.config import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def alembic_config(settings: Settings | None = None) -> Config:
    settings = settings or load_settings()
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["url"] = settings.database_url
    return cfg


def upgrade(settings: Settings | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


def downgrade(settings: Settings | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(settings), revision)


def current(settings: Settings | None = None) -> None:
    command.current(alembic_config(settings), verbose=True)


def revision(message: str, settings: Settings | None = None, autogenerate: bool = True) -> None:
    command.revision(alembic_config(settings), message=message, autogenerate=autogenerate)
