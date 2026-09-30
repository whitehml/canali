"""Test the design matrix builder."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import numpy as np
import pytest

from pridge import design
from pridge.design import DesignError
from pridge.testing.synthetic import match_row
from warehouse.client import MatchRow


def _rows() -> list[MatchRow]:
    """Two matches over teams 10, 20, 30 and 40, each team playing twice."""
    return [
        match_row(1, "RED", (30, 10), 100.0),
        match_row(1, "BLUE", (20, 40), 80.0),
        match_row(2, "RED", (40, 10), 60.0),
        match_row(2, "BLUE", (30, 20), 90.0),
    ]


# ---------------------------------------------------------------------------------------------------------- shape


def test_columns_are_the_teams_that_played_in_ascending_order_with_a_one_where_a_team_is_on_the_alliance() -> None:
    expected = np.array([[1, 0, 1, 0], [0, 1, 0, 1], [1, 0, 0, 1], [0, 1, 1, 0]], dtype=float)
    built = design.build(_rows())
    assert built.team_numbers == (10, 20, 30, 40)
    np.testing.assert_array_equal(built.X, expected)
    np.testing.assert_array_equal(built.y, [100.0, 80.0, 60.0, 90.0])

    reversed_build = design.build(list(reversed(_rows())))
    assert reversed_build.team_numbers == built.team_numbers
    np.testing.assert_array_equal(reversed_build.X, expected[::-1])


def test_a_no_show_keeps_its_alliance_row_with_a_single_one_and_gets_no_column() -> None:
    rows = [
        match_row(1, "RED", (11, 22), 70.0, no_shows=(False, True)),
        match_row(1, "BLUE", (33, 44), 50.0),
        match_row(2, "RED", (11, 33), 40.0),
        match_row(2, "BLUE", (44, 55), 30.0),
    ]
    built = design.build(rows)
    assert built.n_rows == 4
    assert built.team_numbers == (11, 33, 44, 55)
    assert built.X[0].sum() == 1.0
    assert built.X[1].sum() == 2.0


def test_only_qualification_rows_count_and_the_cut_is_inclusive() -> None:
    rows = [
        *(
            match_row(k, alliance, teams, 10.0)
            for k in (1, 2, 3)
            for alliance, teams in (("RED", (1, 2)), ("BLUE", (3, 4)))
        ),
        match_row(4, "RED", (1, 3), 99.0, level="PLAYOFF"),
    ]
    assert design.build(rows).n_rows == 6
    cut = design.build(rows, as_of_match=2)
    assert cut.n_rows == 4
    assert cut.as_of_match == 2
    assert design.build(rows, as_of_match=99).as_of_match == 3


# ----------------------------------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "call",
    [
        lambda: design.build([match_row(1, "RED", (1, 2), 5.0, level="PLAYOFF")]),
        lambda: design.build(_rows(), as_of_match=0),
        lambda: design.build(
            [match_row(1, "RED", (1, 2), 5.0), match_row(1, "BLUE", (3, 4), 5.0, event_id=uuid.UUID(int=2))]
        ),
        lambda: design.build([match_row(1, "RED", (1, 2), 5.0, no_shows=(True, True))]),
        lambda: design.build([match_row(1, "RED", (5, 5), 5.0)]),
        lambda: design.build([match_row(1, "RED", (1, 2, 3), 5.0)]),
        lambda: design.build(_rows(), response={}),
    ],
    ids=[
        "no-qualification-rows",
        "cut-before-the-first-match",
        "two-events",
        "every-team-a-no-show",
        "team-twice-on-one-alliance",
        "three-teams-on-one-alliance",
        "response-missing-a-row",
    ],
)
def test_an_input_the_estimator_cannot_use_is_refused(call: Callable[[], object]) -> None:
    with pytest.raises(DesignError):
        call()


# ---------------------------------------------------------------------------------------------------- response


def test_a_response_is_matched_to_rows_by_identity_and_not_by_order() -> None:
    rows = _rows()
    values = {(row.match_id, row.alliance): float(10 * index) for index, row in enumerate(rows)}
    scrambled = dict(reversed(list(values.items())))

    built = design.build(rows, response=scrambled)
    np.testing.assert_array_equal(built.y, [0.0, 10.0, 20.0, 30.0])
    np.testing.assert_array_equal(built.X, design.build(rows).X)
