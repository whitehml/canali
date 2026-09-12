"""The season's partition: what it is, that it reconciles, and the response it derives.

Total EPA is the sum of the component EPAs: each component the partition names gets its own series, updated against
that component's alliance value, and the total reconciles by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Not a valid component name, so it cannot collide with anything a rule pack declares.
TOTAL = "__total__"
TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class ResolvedPartition:
    """A season's partition, checked against the rule pack."""

    season: int
    names: tuple[str, ...]
    columns: tuple[str, ...]

    @property
    def is_total_only(self) -> bool:
        return not self.names

    @property
    def series(self) -> tuple[str, ...]:
        """The keys this season's per-component state is held under, by ``column_name``."""
        return self.columns or (TOTAL,)


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


def resolve_partition(
    season: int,
    partition: Sequence[str],
    components: Sequence[Mapping[str, object]],
) -> ResolvedPartition:
    declared = {str(c["name"]): str(c["column_name"]) for c in components}
    missing = [name for name in partition if name not in declared]
    if missing:
        raise ValueError(
            f"season {season} names components the rule pack does not declare as fittable: {missing}. "
            f"Do not proceed with a partial partition."
        )
    return ResolvedPartition(
        season=season,
        names=tuple(partition),
        columns=tuple(declared[name] for name in partition),
    )


def partition_from_pack(season: int, components: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """The season's partition as the rule pack declares it, empty when it declares no group."""
    groups: dict[str, list[str]] = {}
    for component in components:
        group = component.get("partition_group")
        if group:
            groups.setdefault(str(group), []).append(str(component["name"]))
    if not groups:
        return ()
    if len(groups) > 1:
        raise ValueError(
            f"season {season} declares {len(groups)} partition groups {sorted(groups)}; "
            f"which decomposition to fit is a decision, not a sort order."
        )
    return tuple(sorted(next(iter(groups.values()))))


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
        if error <= TOLERANCE:
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
