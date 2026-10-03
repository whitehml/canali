"""Design matrix construction from `warehouse.client.MatchRow`.

One row per alliance appearance in a qualification match, one column per team that played, a 1 where the team is on the
alliance. The response is the alliance's non-foul score. A no-show leaves its alliance row in place with a single 1, so
every row sums to 1 or 2.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from warehouse.client import MatchRow
from warehouse.schema.types import QUALIFICATION_MATCHES

type Matrix = NDArray[np.float64]
type Vector = NDArray[np.float64]


RowKey = tuple[uuid.UUID, str]


class DesignError(ValueError):
    """A row violates an invariant the estimator relies on."""


@dataclass(frozen=True, slots=True)
class Design:
    """A design matrix, its response, and the team ordering that makes the columns interpretable.

    Columns are sorted by team number.
    """

    X: Matrix
    y: Vector
    team_numbers: tuple[int, ...]
    row_keys: tuple[RowKey, ...]
    as_of_match: int

    @property
    def n_rows(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_teams(self) -> int:
        return int(self.X.shape[1])


def build(
    rows: Iterable[MatchRow],
    as_of_match: int | None = None,
    response: Mapping[RowKey, float] | None = None,
) -> Design:
    """Build `X` and `y` for one event, optionally truncated to a match ordinal.

    `as_of_match` is inclusive and counts `event_match_ordinal`. `response` replaces `y` with a value per
    `(match_id, alliance)`.
    """
    selected = [
        row
        for row in rows
        if row.level in QUALIFICATION_MATCHES and (as_of_match is None or row.event_match_ordinal <= as_of_match)
    ]
    if not selected:
        raise DesignError("no qualification rows in range")

    events = {row.event_id for row in selected}
    if len(events) > 1:
        raise DesignError(f"a fit covers one event, got {len(events)}")

    teams = sorted({team for row in selected for team in row.rated_teams()})
    if not teams:
        raise DesignError("every team in range was a no-show")
    column_of = {team: index for index, team in enumerate(teams)}

    X = np.zeros((len(selected), len(teams)), dtype=np.float64)
    y = np.empty(len(selected), dtype=np.float64)

    for index, row in enumerate(selected):
        present = row.rated_teams()
        _check_row(row, present)
        for team in present:
            X[index, column_of[team]] = 1.0
        key = (row.match_id, row.alliance)
        if response is None:
            y[index] = row.score_no_foul
        elif key in response:
            y[index] = response[key]
        else:
            raise DesignError(f"no component response for {key}")

    return Design(
        X=X,
        y=y,
        team_numbers=tuple(teams),
        row_keys=tuple((row.match_id, row.alliance) for row in selected),
        as_of_match=max(row.event_match_ordinal for row in selected),
    )


def _check_row(row: MatchRow, present: tuple[int, ...]) -> None:
    if len(set(present)) != len(present):
        raise DesignError(f"match {row.match_id} {row.alliance}: a team appears twice on one alliance")
    if not 1 <= len(present) <= 2:
        raise DesignError(f"match {row.match_id} {row.alliance}: {len(present)} teams present, expected 1 or 2")
