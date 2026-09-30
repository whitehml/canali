"""Test lambda re-derivation and the report that judges a constant."""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from pridge import design
from pridge.constants import lambda_for
from pridge.prior import EpaPrior, MissingPriorError
from pridge.testing.synthetic import synthetic_event, synthetic_samples
from pridge.tune import EventSample, LambdaGrid, LambdaReport, observe

SMALL_GRID = LambdaGrid(low=0.5, high=4.0, points=3)


def _sample(code: str, n_matches: int = 12, seed: int = 4, event_id: uuid.UUID | None = None) -> EventSample:
    event = synthetic_event(n_matches=n_matches, seed=seed, event_id=event_id or uuid.UUID(int=1))
    return EventSample(code, event.rows, event.prior)


# ------------------------------------------------------------------------------------------------- leave-one-out


def test_leave_one_out_pools_the_residuals_of_full_rank_fits_only() -> None:
    full = _sample("full")
    thin = _sample("thin", n_matches=3, seed=5, event_id=uuid.UUID(int=2))
    assert all(
        np.linalg.matrix_rank(built.X) < built.n_teams
        for built in (design.build(thin.rows, as_of_match=k) for k in (1, 2, 3))
    )

    report = observe([full, thin], grid=SMALL_GRID)
    assert report.event_codes == ["full"]

    expected = np.zeros(SMALL_GRID.points)
    rows = 0
    for k in range(1, 13):
        built = design.build(full.rows, as_of_match=k)
        if np.linalg.matrix_rank(built.X) != built.n_teams:
            continue
        beta0 = full.prior.vector(built.team_numbers)
        for i, lam in enumerate(SMALL_GRID.values()):
            for row in range(built.n_rows):
                keep = np.arange(built.n_rows) != row
                gram = built.X[keep].T @ built.X[keep] + lam * np.eye(built.n_teams)
                beta = np.linalg.solve(gram, built.X[keep].T @ built.y[keep] + lam * beta0)
                expected[i] += (built.y[row] - built.X[row] @ beta) ** 2
        rows += built.n_rows
    assert rows > 0
    np.testing.assert_allclose(report.sse_by_event[0], expected, rtol=1e-8)
    assert report.n_by_event == [rows]


# -------------------------------------------------------------------------------------------------- the constant


def test_the_shipped_constant_comes_from_the_events_tier_and_one_report_covers_one_constant() -> None:
    regular = synthetic_samples(2, event_type="Qualifier")
    assert observe(regular, grid=SMALL_GRID).shipped == lambda_for("Qualifier")
    assert observe(regular, shipped=3.0, grid=SMALL_GRID).shipped == 3.0

    # RCMP and CMP share a constant, so events of both fit in one report.
    championship = [
        *synthetic_samples(1, event_type="League Tournament"),
        *synthetic_samples(1, seed=9, event_type="FIRST Championship"),
    ]
    assert observe(championship, grid=SMALL_GRID).shipped == lambda_for("FIRST Championship")

    with pytest.raises(ValueError):
        observe([*regular, *synthetic_samples(1, event_type="FIRST Championship")], grid=SMALL_GRID)
    with pytest.raises(ValueError):
        observe(synthetic_samples(1, event_type="Off-Season"), grid=SMALL_GRID)


# -------------------------------------------------------------------------------------------------------- report


def _report(*curves: list[float], shipped: float = 1.0, lambdas: tuple[float, ...] = (1.0, 3.0)) -> LambdaReport:
    return LambdaReport(
        lambdas=np.array(lambdas),
        sse_by_event=[np.array(curve) for curve in curves],
        n_by_event=[2] * len(curves),
        event_codes=[f"e{i}" for i in range(len(curves))],
        shipped=shipped,
    )


def test_the_report_picks_the_lowest_error_and_prices_any_other_lambda() -> None:
    report = _report([10.0, 6.0, 8.0], lambdas=(1.0, 2.0, 3.0))
    np.testing.assert_allclose(report.mse, [5.0, 3.0, 4.0])
    assert report.pick() == 2.0
    assert report.relative_cost().min() == 1.0
    assert report.cost_of(2.0) == 0.0
    np.testing.assert_allclose(report.cost_of(2.9), 4.0 / 3.0 - 1.0)


@pytest.mark.parametrize(
    ("curves", "cost_over_tolerance", "interval_excludes_shipped", "change"),
    [
        ([[10.0, 10.05]] * 3, False, False, False),
        ([[12.0, 10.0]] * 4, True, True, True),
        ([[12.0, 10.0]] * 3 + [[10.0, 12.0]] * 2, True, False, False),
    ],
    ids=["within-tolerance", "costly-and-excluded", "costly-but-interval-contains-it"],
)
def test_a_change_is_recommended_only_when_it_is_costly_and_the_interval_excludes_the_constant(
    curves: list[list[float]], cost_over_tolerance: bool, interval_excludes_shipped: bool, change: bool
) -> None:
    report = _report(*curves, shipped=1.0)
    low, high = report.bootstrap_interval()
    assert (report.cost_of(1.0) > 0.02) is cost_over_tolerance
    assert (not low <= 1.0 <= high) is interval_excludes_shipped
    assert report.verdict(tolerance=0.02)[0] is change


# ------------------------------------------------------------------------------------------------------ refusals


def test_nothing_to_measure_is_refused() -> None:
    with pytest.raises(ValueError):
        observe([])
    empty = LambdaReport(np.array([1.0]), [], [], [], shipped=1.0)
    with pytest.raises(ValueError):
        empty.bootstrap_interval()
    with pytest.raises(ValueError):
        observe([_sample("thin", n_matches=3, seed=5)], grid=SMALL_GRID)


def test_a_prior_that_lacks_a_team_is_refused_and_never_skipped() -> None:
    event = synthetic_event(n_matches=12)
    without_one = EpaPrior(event.prior.event_id, {t: v for t, v in event.prior.total.items() if t != 1}, {})
    with pytest.raises(MissingPriorError):
        observe([EventSample("gap", event.rows, without_one)], grid=SMALL_GRID)
