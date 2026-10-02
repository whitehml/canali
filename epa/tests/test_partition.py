"""The season's partition: what a row responds with, and whether it reconciles."""

from __future__ import annotations

from typing import Any

import pytest

from epa.partition import TOTAL, assert_partition, response_values, series
from warehouse.rules.model import to_column_name
from warehouse.rules.partition import ResolvedPartition, resolve_partition

SEASON = 9999


def _component(name: str, *, group: str | None = None) -> dict[str, Any]:
    """One row in the shape `Warehouse.fittable_components` returns, which is what the partition reads."""
    return {
        "name": name,
        "column_name": to_column_name(name),
        "level": "alliance",
        "kind": "numeric",
        "is_subtotal": True,
        "is_derived": False,
        "partition_group": group,
    }


PHASE = [
    _component("teleopWidgetPoints", group="phase"),
    _component("autoWidgetPoints", group="phase"),
    _component("robot1Sprocket"),
]


def _phase_partition() -> ResolvedPartition:
    return resolve_partition(SEASON, PHASE)


# ------------------------------------------------------------------------------------------------------- response


def test_a_component_partition_refuses_a_row_with_no_breakdown() -> None:
    with pytest.raises(ValueError, match="must not be treated as zeros"):
        response_values(_phase_partition(), 90.0, None)


def test_a_total_only_partition_responds_with_the_no_foul_score() -> None:
    partition = ResolvedPartition.total_only(SEASON)
    assert series(partition) == (TOTAL,)
    assert response_values(partition, 90.0, None) == {TOTAL: 90.0}


# ---------------------------------------------------------------------------------------------------- assertion


def test_a_partition_whose_components_sum_to_the_total_reconciles_every_row() -> None:
    partition = _phase_partition()
    rows = [
        (("USPAX", ordinal, "RED"), 90.0, {"auto_widget_points": 30.0, "teleop_widget_points": 60.0})
        for ordinal in (1, 2, 3)
    ]
    report = assert_partition(partition, rows)
    assert report.ok
    assert (report.rows, report.reconciled) == (3, 3)


def test_a_failing_row_is_named_with_both_totals() -> None:
    partition = _phase_partition()
    components = {"auto_widget_points": 30.0, "teleop_widget_points": 60.0}
    report = assert_partition(
        partition,
        [(("USPAX", 1, "RED"), 90.0, components), (("USPAX", 2, "BLUE"), 100.0, components)],
    )
    assert not report.ok
    assert report.failed_keys == frozenset({("USPAX", 2, "BLUE")})
    assert report.failures == ((("USPAX", 2, "BLUE"), 100.0, 90.0),)
    assert report.worst_absolute_error == 10.0
