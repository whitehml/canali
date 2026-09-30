"""One event fit: the total and each component at one shared lambda.

Component fits share the total's design matrix and lambda, and each regularizes toward its own component prior. The
estimator is linear in the response and the prior for fixed `X` and lambda.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from pridge import design
from pridge.constants import lambda_for
from pridge.design import RowKey
from pridge.estimator import Fit, fit, fit_grid
from pridge.prior import Prior
from pridge.tune import LAMBDA_GRID, LambdaGrid
from warehouse.client import MatchRow

TOTAL = "total"


@dataclass(frozen=True, slots=True)
class EventFit:
    """Everything one `(event, as_of_match)` fit produced."""

    team_numbers: tuple[int, ...]
    as_of_match: int
    lam: float
    n_rows: int
    total: Fit
    components: dict[str, Fit]

    def ratings(self, component: str = TOTAL) -> dict[int, float]:
        source = self.total if component == TOTAL else self.components[component]
        return dict(zip(self.team_numbers, source.beta, strict=True))

    def component_sum_error(self) -> float:
        """Largest absolute gap between the summed component ratings and the total rating."""
        if not self.components:
            return 0.0
        stacked = np.vstack([f.beta for f in self.components.values()])
        return float(np.max(np.abs(stacked.sum(axis=0) - self.total.beta)))


def fit_event(
    rows: Sequence[MatchRow],
    prior: Prior,
    *,
    as_of_match: int | None = None,
    component_responses: Mapping[str, Mapping[RowKey, float]] | None = None,
    lam: float | None = None,
) -> EventFit:
    """Fit one event at one match ordinal, the total and then each named component."""
    total_design = design.build(rows, as_of_match=as_of_match)
    lam = lambda_for(rows[0].event_type) if lam is None else lam
    total = fit(total_design.X, total_design.y, prior.vector(total_design.team_numbers), lam)

    components: dict[str, Fit] = {}
    for name, response in (component_responses or {}).items():
        sub = design.build(rows, as_of_match=as_of_match, response=response)
        components[name] = fit(sub.X, sub.y, prior.vector(sub.team_numbers, name), lam)

    return EventFit(
        team_numbers=total_design.team_numbers,
        as_of_match=total_design.as_of_match,
        lam=lam,
        n_rows=total_design.n_rows,
        total=total,
        components=components,
    )


def fit_event_per_fit(
    rows: Sequence[MatchRow], prior: Prior, *, as_of_match: int | None = None, grid: LambdaGrid = LAMBDA_GRID
) -> EventFit:
    """Fit one event at one match ordinal with the lambda that minimizes its own leave-one-out error."""
    built = design.build(rows, as_of_match=as_of_match)
    best, _ = fit_grid(built.X, built.y, prior.vector(built.team_numbers), grid.values())
    return EventFit(
        team_numbers=built.team_numbers,
        as_of_match=built.as_of_match,
        lam=best.lam,
        n_rows=built.n_rows,
        total=best,
        components={},
    )
