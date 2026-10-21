"""When an event's Monte Carlo has run long enough."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import numpy.typing as npt

from sim.event import Slots

TOLERANCE = 0.01
MIN_ITERATIONS = 1000


def tracked_places(field: Iterable[int], n_alliances: int, slots: Slots | None) -> int:
    """Advancement order positions that can carry a slot: every slot plus the teams passed over for having already
    advanced, or the alliance count when the event offers none."""
    if slots is None:
        return n_alliances
    return slots.count + len(slots.already_advanced.intersection(field))


class Convergence:
    """Running counts of each team's advancement order positions."""

    def __init__(self, places: int) -> None:
        self.places = places
        self.iterations = 0
        self._counts: dict[int, npt.NDArray[np.int64]] = {}

    def add(self, order: Sequence[int]) -> None:
        """Count one iteration's advancement order, best first."""
        self.iterations += 1
        for position, team in enumerate(order[: self.places]):
            counts = self._counts.setdefault(team, np.zeros(self.places, dtype=np.int64))
            counts[position:] += 1

    def probabilities(self) -> dict[int, npt.NDArray[np.float64]]:
        """Each team's chance of finishing at or above each tracked position."""
        return {team: counts / self.iterations for team, counts in self._counts.items()}

    def counts(self) -> dict[str, list[int]]:
        """Raw cumulative counts for a JSON checkpoint."""
        return {str(team): counts.tolist() for team, counts in self._counts.items()}

    def restore_counts(self, iterations: int, counts: dict[str, list[int]]) -> None:
        self.iterations = iterations
        self._counts = {int(team): np.array(values, dtype=np.int64) for team, values in counts.items()}

    def standard_error(self) -> float:
        """The largest standard error among the tracked chances."""
        if self.iterations == 0:
            return float("inf")
        if not self._counts:
            return 0.0
        p = np.stack(list(self._counts.values())) / self.iterations
        return float(np.sqrt(np.max(p * (1.0 - p)) / self.iterations))

    def converged(self, *, tolerance: float = TOLERANCE, min_iterations: int = MIN_ITERATIONS) -> bool:
        return self.iterations >= min_iterations and self.standard_error() < tolerance
