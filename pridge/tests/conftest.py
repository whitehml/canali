"""Shared design cases for the estimator tests."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from pridge import design
from pridge.design import Matrix, Vector
from pridge.testing.synthetic import match_row, synthetic_event

Case = tuple[Matrix, Vector, Vector]


@pytest.fixture
def make_case() -> Callable[[str], Case]:
    """Build X, y and a prior. `deficient` has fewer rows than teams and `no_show` has a one-team alliance row."""

    def make(kind: str) -> Case:
        event = synthetic_event(n_matches=3 if kind == "deficient" else 24)
        rows = list(event.rows)
        if kind == "no_show":
            first = rows[0]
            rows[0] = match_row(
                first.event_match_ordinal,
                first.alliance,
                first.team_numbers,
                first.score_no_foul,
                no_shows=(False, True),
            )
        built = design.build(rows)
        if kind == "deficient":
            assert np.linalg.matrix_rank(built.X) < built.n_teams
        if kind == "no_show":
            assert built.X[0].sum() == 1.0
        return built.X, built.y, event.prior.vector(built.team_numbers)

    return make
