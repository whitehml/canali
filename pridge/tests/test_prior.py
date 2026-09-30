"""Test the prior and its EPA source."""

from __future__ import annotations

import uuid
from typing import cast

import numpy as np
import pytest

from pridge.prior import EpaPrior, EpaPriorSource, MissingPriorError
from warehouse.client import PreEventEpaRow, Warehouse

EVENT = uuid.UUID(int=1)
RUN = uuid.UUID(int=99)


def _prior() -> EpaPrior:
    return EpaPrior(
        event_id=EVENT,
        total={3: 30.0, 1: 10.0, 2: 20.0},
        components={1: {"a": 1.0, "b": 2.0}, 2: {"a": 3.0}, 3: {"a": 5.0, "b": 6.0}},
    )


def _row(team: int, scaled: float, norm: float | None = None, **components: float) -> PreEventEpaRow:
    return PreEventEpaRow(2025, EVENT, 1, team, "epa-x", scaled, norm, components, False)


# ------------------------------------------------------------------------------------------------------- vector


def test_the_vector_follows_the_order_of_the_teams_asked_for() -> None:
    np.testing.assert_array_equal(_prior().vector([2, 3, 1]), [20.0, 30.0, 10.0])
    np.testing.assert_array_equal(_prior().vector([3, 1], "a"), [5.0, 1.0])


@pytest.mark.parametrize(
    ("teams", "component"),
    [([1, 99], None), ([1, 2], "b"), ([1], "missing")],
    ids=["team-without-a-prior", "team-lacking-the-component", "unknown-component"],
)
def test_a_missing_prior_is_refused_and_never_defaulted(teams: list[int], component: str | None) -> None:
    with pytest.raises(MissingPriorError):
        _prior().vector(teams, component)


# ---------------------------------------------------------------------------------------------------- from_rows


def test_the_total_is_the_scaled_rating_and_not_the_percentile() -> None:
    prior = EpaPrior.from_rows([_row(1, 50.0, 900.0, a=20.0, b=30.0), _row(2, 60.0, 400.0, a=25.0, b=35.0)], EVENT)
    assert prior.total == {1: 50.0, 2: 60.0}
    assert prior.components[2] == {"a": 25.0, "b": 35.0}


@pytest.mark.parametrize(
    "rows",
    [[], [_row(1, 50.0), _row(1, 55.0)]],
    ids=["no-rows", "a-team-with-several-rows"],
)
def test_rows_that_do_not_identify_one_prior_are_refused(rows: list[PreEventEpaRow]) -> None:
    with pytest.raises(MissingPriorError):
        EpaPrior.from_rows(rows, EVENT)


# ------------------------------------------------------------------------------------------------------- source


class _Warehouse:
    """The three reads the source makes, served from a table keyed by event and pinned run."""

    def __init__(self, table: dict[tuple[uuid.UUID, uuid.UUID | None], list[PreEventEpaRow]]) -> None:
        self.table = table

    def batch_fit_run(self, model: str, season: int, version: str) -> uuid.UUID | None:
        return RUN if version == "epa-x" else None

    def model_versions(self, model: str) -> list[str]:
        return ["epa-x", "epa-y"]

    def pre_event_epa(
        self, event_id: uuid.UUID, version: str, *, fit_run_id: uuid.UUID | None = None
    ) -> list[PreEventEpaRow]:
        return self.table.get((event_id, fit_run_id), [])


def test_an_unknown_version_is_refused_naming_the_versions_that_have_a_batch_run() -> None:
    warehouse = cast(Warehouse, _Warehouse({}))
    with pytest.raises(MissingPriorError, match="epa-y"):
        EpaPriorSource.for_season(warehouse, 2025, "epa-nope")
    source = EpaPriorSource.for_season(warehouse, 2025, "epa-x")
    assert (source.run, source.prior_version) == (RUN, "epa-x")


def test_an_event_takes_the_pinned_run_and_falls_back_only_when_the_pin_has_nothing() -> None:
    covered, uncovered = uuid.UUID(int=1), uuid.UUID(int=2)
    table = {
        (covered, RUN): [_row(1, 111.0)],
        (covered, None): [_row(1, 999.0)],
        (uncovered, None): [_row(1, 222.0)],
    }
    source = EpaPriorSource.for_season(cast(Warehouse, _Warehouse(table)), 2025, "epa-x")
    assert source.load(covered).total == {1: 111.0}
    assert source.load(uncovered).total == {1: 222.0}
