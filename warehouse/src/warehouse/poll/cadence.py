"""Poll timing, read from ``config/cadence.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CADENCE_FILE = Path(__file__).resolve().parents[3] / "config" / "cadence.toml"


class Cadence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hybrid_s: float = Field(gt=0)
    after_close_s: list[float] = Field(min_length=1)
    restart_s: float = Field(gt=0)

    @classmethod
    def from_file(cls, path: Path = CADENCE_FILE) -> Cadence:
        with path.open("rb") as fh:
            return cls.model_validate(tomllib.load(fh))
