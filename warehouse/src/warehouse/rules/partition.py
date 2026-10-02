"""Which of a season's declared partitions a model fits, resolved onto the columns its breakdown is read by."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from warehouse.rules.model import DEFAULT_PARTITION_GROUP

if TYPE_CHECKING:
    from warehouse.client import Warehouse

# Within this a partition's components sum to its total.
RECONCILE_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class ResolvedPartition:
    """A season's partition: the component names the rule pack declares and their breakdown columns, in step."""

    season: int
    names: tuple[str, ...]
    columns: tuple[str, ...]

    @classmethod
    def total_only(cls, season: int) -> Self:
        return cls(season=season, names=(), columns=())

    @property
    def is_total_only(self) -> bool:
        return not self.names


def select_partition(
    season: int,
    components: Sequence[Mapping[str, object]],
    *,
    group: str | None = None,
) -> tuple[str, ...]:
    """The name of each component in the chosen partition group, sorted.

    With no `group`, a season that declares the default group fits it. A season that declares a single group of
    another name fits that one, and a season that declares none fits the total alone.
    """
    groups: dict[str, list[str]] = {}
    for component in components:
        declared = component.get("partition_group")
        if declared:
            groups.setdefault(str(declared), []).append(str(component["name"]))
    if not groups:
        return ()
    if group is None:
        if DEFAULT_PARTITION_GROUP in groups:
            group = DEFAULT_PARTITION_GROUP
        elif len(groups) == 1:
            group = next(iter(groups))
        else:
            raise ValueError(
                f"season {season} declares {len(groups)} partition groups {sorted(groups)}; "
                f"which decomposition to fit is a decision, not a sort order."
            )
    if group not in groups:
        raise ValueError(f"season {season} declares partition groups {sorted(groups)}, not {group!r}")
    return tuple(sorted(groups[group]))


def resolve_partition(
    season: int,
    components: Sequence[Mapping[str, object]],
    *,
    group: str | None = None,
) -> ResolvedPartition:
    """The chosen partition with each name's column alongside it."""
    names = select_partition(season, components, group=group)
    columns = {str(c["name"]): str(c["column_name"]) for c in components}
    return ResolvedPartition(season=season, names=names, columns=tuple(columns[name] for name in names))


def season_partition(warehouse: Warehouse, season: int, group: str | None = None) -> ResolvedPartition:
    """The partition the season's rule pack declares."""
    return resolve_partition(season, warehouse.fittable_components(season), group=group)
