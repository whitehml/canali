"""Lambda re-derivation.

A constant is the lambda that minimizes leave-one-out squared error, summed over every fit whose design has full column
rank, pooled over a sample of events that share it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from pridge import design
from pridge.constants import lambda_for
from pridge.design import Vector
from pridge.estimator import Gram, fit
from pridge.prior import Prior
from warehouse.client import MatchRow


@dataclass(frozen=True, slots=True)
class LambdaGrid:
    """A log-spaced lambda search grid, ascending."""

    low: float
    high: float
    points: int

    def values(self) -> Vector:
        return np.logspace(np.log10(self.low), np.log10(self.high), self.points)


LAMBDA_GRID = LambdaGrid(low=0.4, high=4.0, points=80)


@dataclass(frozen=True, slots=True)
class EventSample:
    """One event's qualification rows and the prior its fits regularize toward."""

    code: str
    rows: Sequence[MatchRow]
    prior: Prior


@dataclass(frozen=True, slots=True)
class LambdaReport:
    """Leave-one-out squared error across a lambda grid, per event, for the events of one tier constant.

    Held per event because the bootstrap resamples events: fits and alliances within an event share rows and are not
    independent draws.
    """

    lambdas: Vector
    sse_by_event: list[Vector]
    n_by_event: list[int]
    event_codes: list[str]
    shipped: float

    @property
    def n(self) -> int:
        return sum(self.n_by_event)

    @property
    def mse(self) -> Vector:
        return np.sum(self.sse_by_event, axis=0) / max(self.n, 1)

    def relative_cost(self) -> Vector:
        """Each lambda's error as a multiple of the best lambda's."""
        mse = self.mse
        return np.asarray(mse / mse.min())

    def pick(self) -> float:
        """The lambda with the lowest pooled error."""
        return float(self.lambdas[int(np.argmin(self.mse))])

    def cost_of(self, lam: float) -> float:
        """How much worse the nearest grid point to `lam` is than the season's argmin, as a fraction."""
        return float(self.relative_cost()[int(np.argmin(np.abs(self.lambdas - lam)))] - 1.0)

    def bootstrap_interval(self, level: float = 0.9, n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
        """Percentile interval for the argmin, resampling events with replacement."""
        if not self.sse_by_event:
            raise ValueError("no events measured")
        rng = np.random.default_rng(seed)
        stacked = np.array(self.sse_by_event)
        n_events = len(stacked)
        picks = np.empty(n_boot)
        for b in range(n_boot):
            picks[b] = self.lambdas[int(np.argmin(stacked[rng.integers(0, n_events, n_events)].sum(axis=0)))]
        tail = (1.0 - level) / 2.0
        return float(np.percentile(picks, 100 * tail)), float(np.percentile(picks, 100 * (1.0 - tail)))

    def verdict(self, level: float = 0.9, tolerance: float = 0.02) -> tuple[bool, str]:
        """Whether the sample wants a lambda other than the shipped one."""
        cost = self.cost_of(self.shipped)
        low, high = self.bootstrap_interval(level=level)
        if cost > tolerance and not low <= self.shipped <= high:
            return True, (
                f"shipped lambda {self.shipped:g} costs {cost:.2%} against the best, {self.pick():g}, and the "
                f"{level:.0%} interval [{low:g}, {high:g}] excludes it"
            )
        if cost <= tolerance:
            return False, (
                f"shipped lambda {self.shipped:g} is within {cost:.2%} of the best, {self.pick():g}, "
                f"inside the {tolerance:.0%} tolerance"
            )
        return False, (
            f"shipped lambda {self.shipped:g} costs {cost:.2%}, but the {level:.0%} interval [{low:g}, {high:g}] "
            "still contains it"
        )


def observe(
    events: Sequence[EventSample], shipped: float | None = None, grid: LambdaGrid = LAMBDA_GRID
) -> LambdaReport:
    """Leave-one-out squared error at every candidate lambda, event by event.

    `shipped` defaults to the constant the events' tier uses, and the events must all share one.
    """
    constants = {lambda_for(event.rows[0].event_type) for event in events}
    if len(constants) != 1:
        raise ValueError(f"events span {len(constants)} tier constants, expected one")
    shipped = constants.pop() if shipped is None else shipped
    values = grid.values()

    sse_by_event: list[Vector] = []
    n_by_event: list[int] = []
    codes: list[str] = []
    for event in events:
        sse, n = _leave_one_out(event, values)
        if n:
            sse_by_event.append(sse)
            n_by_event.append(n)
            codes.append(event.code)

    if not sse_by_event:
        raise ValueError("no event produced a scored fit")
    return LambdaReport(
        lambdas=values, sse_by_event=sse_by_event, n_by_event=n_by_event, event_codes=codes, shipped=shipped
    )


def derive_lambda(events: Sequence[EventSample], grid: LambdaGrid = LAMBDA_GRID) -> float:
    """The lambda minimizing leave-one-out error over a sample of events that share one tier constant."""
    return observe(events, grid=grid).pick()


def _leave_one_out(event: EventSample, values: Vector) -> tuple[Vector, int]:
    """Leave-one-out squared residuals at every lambda, summed over the event's fits that have full column rank."""
    sse = np.zeros(values.size)
    n = 0
    for k in sorted({row.event_match_ordinal for row in event.rows if row.level == design.QUALIFICATION}):
        try:
            built = design.build(event.rows, as_of_match=k)
        except ValueError:
            continue
        beta0 = event.prior.vector(built.team_numbers)
        if int(np.linalg.matrix_rank(built.X)) != built.n_teams:
            continue
        gram = Gram.build(built.X, built.y)
        for i, lam in enumerate(values):
            residuals = fit(built.X, built.y, beta0, float(lam), gram=gram).loocv_residuals()
            sse[i] += float(residuals @ residuals)
        n += built.n_rows
    return sse, n
