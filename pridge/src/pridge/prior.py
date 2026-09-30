"""Beta-zero: the vector the ridge penalty pulls toward.

A `Prior` is anything that can name a value per team, for the total and for each component. A `PriorSource` is where a
season's event priors come from. `EpaPriorSource` is the only one, reading pre-event EPA.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from pridge.design import Vector
from warehouse.client import PreEventEpaRow, Warehouse

EPA_MODEL = "epa"


class MissingPriorError(LookupError):
    """No usable prior for a team the fit needs."""


class Prior(Protocol):
    """What a fit needs from a prior, in the units of the response."""

    def vector(self, teams: Sequence[int], component: str | None = None) -> Vector:
        """The prior in the order of `teams`, for the total or for one component."""
        ...


class PriorSource(Protocol):
    """Where a season's event priors come from.

    `prior_version` labels the source on every fit run that uses it.
    """

    @property
    def prior_version(self) -> str: ...

    def load(self, event_id: uuid.UUID) -> Prior:
        """The prior for one event, or `MissingPriorError`."""
        ...


@dataclass(frozen=True, slots=True)
class EpaPrior:
    """Pre-event EPA."""

    event_id: uuid.UUID
    total: dict[int, float]
    components: dict[int, dict[str, float]]

    @classmethod
    def from_rows(cls, rows: Sequence[PreEventEpaRow], event_id: uuid.UUID) -> EpaPrior:
        """Build from `Warehouse.pre_event_epa` output for one pinned run."""
        if not rows:
            raise MissingPriorError(f"no pre-event EPA for event {event_id}")

        teams = [row.team_number for row in rows]
        duplicated = sorted({team for team in teams if teams.count(team) > 1})
        if duplicated:
            raise MissingPriorError(f"team(s) {duplicated} have several EPA rows at event {event_id}, pin one run")

        return cls(
            event_id=event_id,
            total={row.team_number: float(row.epa_scaled) for row in rows},
            components={
                row.team_number: {name: float(value) for name, value in row.components.items()} for row in rows
            },
        )

    def vector(self, teams: Sequence[int], component: str | None = None) -> Vector:
        source = self.total if component is None else self._component(component)
        missing = [team for team in teams if team not in source]
        if missing:
            what = "pre-event EPA" if component is None else f"pre-event EPA for component {component!r}"
            raise MissingPriorError(f"no {what} for team(s) {missing} at event {self.event_id}")
        return np.array([source[team] for team in teams], dtype=np.float64)

    def _component(self, component: str) -> dict[int, float]:
        return {team: values[component] for team, values in self.components.items() if component in values}


@dataclass(frozen=True, slots=True)
class EpaPriorSource:
    """Pre-event EPA read from one completed EPA batch run."""

    warehouse: Warehouse
    epa_version: str
    run: uuid.UUID

    @classmethod
    def for_season(cls, warehouse: Warehouse, season: int, epa_version: str) -> EpaPriorSource:
        """Pin the completed batch run at `epa_version`, or fail naming the versions that have one."""
        run = warehouse.batch_fit_run(EPA_MODEL, season, epa_version)
        if run is None:
            available = warehouse.model_versions(EPA_MODEL)
            raise MissingPriorError(
                f"no completed {epa_version} batch run for season {season}; versions with one: {available}"
            )
        return cls(warehouse, epa_version, run)

    @property
    def prior_version(self) -> str:
        return self.epa_version

    def load(self, event_id: uuid.UUID) -> EpaPrior:
        rows = self.warehouse.pre_event_epa(event_id, self.epa_version, fit_run_id=self.run)
        if not rows:
            rows = self.warehouse.pre_event_epa(event_id, self.epa_version)
        return EpaPrior.from_rows(rows, event_id)
