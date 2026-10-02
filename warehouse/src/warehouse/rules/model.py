"""The rule-pack file format parser.

A pack defines which rules and scoring mechanisms a season used.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ComponentKind = Literal["numeric", "boolean", "enum", "array"]
ComponentLevel = Literal["alliance", "team"]
Phase = Literal["auto", "teleop"]

DEFAULT_PARTITION_GROUP = "leaf"
"""The partition the rating models fit unless told otherwise: every finest-grain scoring line item of a season."""

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_IDENT = re.compile(r"[^a-z0-9_]+")


def to_column_name(name: str) -> str:
    """``autoArtifactPoints`` -> ``auto_artifact_points``."""
    snake = _CAMEL_BOUNDARY.sub("_", name)
    snake = _NON_IDENT.sub("_", snake.lower()).strip("_")
    return f"c_{snake}" if not snake or snake[0].isdigit() else snake


class StateScoring(BaseModel):
    """Points a season awards for the state each robot ends a phase in.

    For enums where FIRST publishes the states but not their value.
    """

    model_config = ConfigDict(extra="forbid")

    # Team-level enum components, one per robot.
    states_from: list[str] = Field(min_length=1)
    # Points per state.
    points: dict[str, float] = Field(min_length=1)


class Component(BaseModel):
    """One line item a match breakdown reports."""

    model_config = ConfigDict(extra="forbid")

    name: str
    level: ComponentLevel = "alliance"
    kind: ComponentKind
    is_subtotal: bool = False
    is_derived: bool = False
    partition_group: str | None = None
    phase: Phase | None = None
    column_name: str | None = None
    recovered_from: list[str] = Field(default_factory=list)
    state_scoring: StateScoring | None = None

    @property
    def is_fittable(self) -> bool:
        return self.kind in ("numeric", "boolean") and not self.is_derived

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.recovered_from and self.kind != "numeric":
            raise ValueError(f"{self.name}: only numeric components can be recovered from a sum")
        if self.recovered_from and not self.is_derived:
            raise ValueError(f"{self.name}: a recovered component is an identity, so it is derived and never fitted")
        if self.state_scoring is not None and (self.kind != "numeric" or self.level != "alliance" or self.is_derived):
            raise ValueError(f"{self.name}: a state-scored component is a numeric alliance leaf, not a derived one")
        if self.phase is not None and not (self.is_subtotal and self.kind == "numeric" and self.level == "alliance"):
            raise ValueError(f"{self.name}: only a numeric alliance subtotal belongs to a phase")
        if self.column_name is None:
            object.__setattr__(self, "column_name", to_column_name(self.name))
        return self


class Ranking(BaseModel):
    """How a season distributes ranking points and breaks ties."""

    model_config = ConfigDict(extra="forbid")

    rp_win: int | None = None
    rp_tie: int | None = None
    rp_loss: int = 0
    has_bonus_rp: bool = False
    formula: str | None = None
    tiebreakers: list[str] = Field(default_factory=list)


class AllianceBracket(BaseModel):
    """Each row is a different sized playoff."""

    model_config = ConfigDict(extra="forbid")

    min_teams: int
    max_teams: int
    alliances: int


class Structure(BaseModel):
    """How a season runs an event: alliance size and playoff format."""

    model_config = ConfigDict(extra="forbid")

    alliance_size: int = 2
    playoff_structure: str | None = None
    playoff_implemented: bool = False
    advancement_implemented: bool = False
    # The recommended playoff structure based on event size.
    alliance_brackets: list[AllianceBracket] = Field(default_factory=list)

    @model_validator(mode="after")
    def _brackets_are_ordered_and_disjoint(self) -> Self:
        previous = 0
        for bracket in self.alliance_brackets:
            if bracket.min_teams <= previous or bracket.max_teams < bracket.min_teams:
                raise ValueError(
                    f"alliance_brackets must be ascending and non-overlapping; "
                    f"{bracket.min_teams}-{bracket.max_teams} is not"
                )
            previous = bracket.max_teams
        return self


class RulePack(BaseModel):
    """One season, as its game manual defines it."""

    model_config = ConfigDict(extra="forbid")

    season: int
    """Starting-year convention. 2025 means 2025-26 / DECODE."""

    game: str
    version: str = "1"
    ranking: Ranking = Field(default_factory=Ranking)
    structure: Structure = Field(default_factory=Structure)
    components: list[Component] = Field(default_factory=list)
    source_file: str = ""

    @model_validator(mode="after")
    def _unique_columns(self) -> Self:
        seen: dict[str, str] = {}
        for component in self.components:
            column = component.column_name or ""
            if column in seen:
                raise ValueError(
                    f"{self.season}: components {seen[column]!r} and {component.name!r} both map to column {column!r}"
                )
            seen[column] = component.name
        return self

    @model_validator(mode="after")
    def _recovery_summands_exist(self) -> Self:
        by_name = {c.name: c for c in self.components}
        for component in self.components:
            for summand in component.recovered_from:
                other = by_name.get(summand)
                if other is None:
                    raise ValueError(
                        f"{self.season}: {component.name!r} recovers from {summand!r}, which the pack does not declare"
                    )
                if other.kind != "numeric" or other.level != component.level:
                    raise ValueError(
                        f"{self.season}: {component.name!r} cannot recover from {summand!r}, "
                        f"a {other.level} {other.kind} component"
                    )
        return self

    @model_validator(mode="after")
    def _scoring_states_exist(self) -> Self:
        by_name = {c.name: c for c in self.components}
        for component in self.components:
            if component.state_scoring is None:
                continue
            for source in component.state_scoring.states_from:
                other = by_name.get(source)
                if other is None:
                    raise ValueError(
                        f"{self.season}: {component.name!r} is scored from {source!r}, which the pack does not declare"
                    )
                if other.kind != "enum" or other.level != "team":
                    raise ValueError(
                        f"{self.season}: {component.name!r} cannot be scored from {source!r}, "
                        f"a {other.level} {other.kind} component"
                    )
        return self

    @classmethod
    def from_file(cls, path: Path) -> RulePack:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        data["source_file"] = path.name
        return cls.model_validate(data)

    def fittable(self) -> list[Component]:
        return [c for c in self.components if c.is_fittable]
