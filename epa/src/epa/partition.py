"""
Total EPA is the sum of the component EPAs: each component the partition names gets its own series, updated against
that component's alliance value, and the total is reconstructed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from warehouse.rules.partition import RECONCILE_TOLERANCE, ResolvedPartition

# Not a valid component name, so it cannot collide with anything a rule pack declares.
TOTAL = "__total__"


def series(partition: ResolvedPartition) -> tuple[str, ...]:
    """The keys a season's per-component state is held under, by ``column_name``."""
    return partition.columns or (TOTAL,)


@dataclass(frozen=True, slots=True)
class PartitionReport:
    """The outcome of checking a partition against real matches."""

    season: int
    names: tuple[str, ...]
    rows: int
    reconciled: int
    worst_absolute_error: float
    failures: tuple[tuple[Any, float, float], ...] = ()

    @property
    def ok(self) -> bool:
        return self.rows > 0 and self.reconciled == self.rows

    @property
    def rate(self) -> float:
        return self.reconciled / self.rows if self.rows else 0.0

    @property
    def failed_keys(self) -> frozenset[Any]:
        return frozenset(key for key, _, _ in self.failures)

    def summary(self) -> str:
        if self.rows == 0:
            return f"season {self.season}: no rows to check"
        return (
            f"season {self.season}: {self.rate:.4%} of {self.rows} alliance-rows reconcile under {list(self.names)}; "
            f"worst |error| {self.worst_absolute_error:.6g}; {len(self.failures)} failing rows"
        )


def response_values(
    partition: ResolvedPartition,
    score_no_foul: float,
    component_values: Mapping[str, float] | None,
) -> dict[str, float]:
    if partition.is_total_only:
        return {TOTAL: score_no_foul}
    if component_values is None:
        raise ValueError(
            f"season {partition.season} fits a component model but no breakdown was supplied for this row; "
            f"a missing breakdown must not be treated as zeros"
        )
    return {column: float(component_values[column]) for column in partition.columns}


def assert_partition(
    partition: ResolvedPartition,
    rows: Sequence[tuple[Any, float, Mapping[str, float]]],
) -> PartitionReport:
    """Check that the partition reconstructs the no-foul total, row by row."""
    if partition.is_total_only:
        return PartitionReport(
            season=partition.season,
            names=(),
            rows=len(rows),
            reconciled=len(rows),
            worst_absolute_error=0.0,
        )

    reconciled = 0
    worst = 0.0
    failures: list[tuple[Any, float, float]] = []
    for key, total, values in rows:
        summed = sum(float(values[column]) for column in partition.columns)
        error = abs(summed - total)
        worst = max(worst, error)
        if error <= RECONCILE_TOLERANCE:
            reconciled += 1
        else:
            failures.append((key, total, summed))

    return PartitionReport(
        season=partition.season,
        names=partition.names,
        rows=len(rows),
        reconciled=reconciled,
        worst_absolute_error=worst,
        failures=tuple(failures),
    )
