"""Beta-zero: the vector the ridge penalty pulls toward.

A `Prior` is anything that can name a value per team, for the total and for each component.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from pridge.design import Vector
from warehouse.client import PreEventEpaRow


class MissingPriorError(LookupError):
    """No usable prior for a team the fit needs."""


class Prior(Protocol):
    """What a fit needs from a prior, in the units of the response.

    `prior_version` labels the source and is recorded on the fit run, so two priors must never share one.
    `provisional` is true when the values rest on an unsettled input.
    """

    @property
    def prior_version(self) -> str: ...

    @property
    def provisional(self) -> bool: ...

    def vector(self, teams: Sequence[int], component: str | None = None) -> Vector:
        """The prior in the order of `teams`, for the total or for one component."""
        ...


@dataclass(frozen=True, slots=True)
class EpaPrior:
    """Pre-event EPA.

    `prior_version` is the EPA model version. `provisional` is true when any team started from a borrowed initial scale.
    """

    event_id: uuid.UUID
    prior_version: str
    total: dict[int, float]
    components: dict[int, dict[str, float]]
    provisional: bool

    @classmethod
    def from_rows(cls, rows: Sequence[PreEventEpaRow], event_id: uuid.UUID, prior_version: str) -> EpaPrior:
        """Build from `Warehouse.pre_event_epa` output for one pinned run."""
        if not rows:
            raise MissingPriorError(f"no pre-event EPA for event {event_id} at EPA version {prior_version!r}")

        teams = [row.team_number for row in rows]
        duplicated = sorted({team for team in teams if teams.count(team) > 1})
        if duplicated:
            raise MissingPriorError(f"team(s) {duplicated} have several EPA rows at event {event_id}, pin one run")

        return cls(
            event_id=event_id,
            prior_version=prior_version,
            total={row.team_number: float(row.epa_scaled) for row in rows},
            components={
                row.team_number: {name: float(value) for name, value in row.components.items()} for row in rows
            },
            provisional=any(row.scale_provisional for row in rows),
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
