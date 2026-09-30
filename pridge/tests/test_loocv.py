"""Test closed-form leave-one-out cross-validation."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from pridge.design import Matrix, Vector
from pridge.estimator import fit, fit_grid
from pridge.tune import EXPLORATORY_GRID

Case = tuple[Matrix, Vector, Vector]


def _brute_force(X: np.ndarray, y: np.ndarray, beta0: np.ndarray, lam: float) -> np.ndarray:
    """Refit without each row in turn, keeping every column, and score the row left out."""
    errors = []
    for i in range(len(y)):
        keep = np.arange(len(y)) != i
        gram = X[keep].T @ X[keep] + lam * np.eye(X.shape[1])
        beta = np.linalg.solve(gram, X[keep].T @ y[keep] + lam * beta0)
        errors.append(y[i] - X[i] @ beta)
    return np.array(errors)


@pytest.mark.parametrize("kind", ["deficient", "full", "no_show"])
@pytest.mark.parametrize("zero_prior", [False, True], ids=["prior", "zero-prior"])
@pytest.mark.parametrize("lam", [0.1, 1.16, 20.0])
def test_the_closed_form_matches_refitting_without_each_row(
    make_case: Callable[[str], Case], kind: str, zero_prior: bool, lam: float
) -> None:
    X, y, beta0 = make_case(kind)
    beta0 = np.zeros_like(beta0) if zero_prior else beta0
    closed = fit(X, y, beta0, lam).loocv_residuals()
    np.testing.assert_allclose(closed, _brute_force(X, y, beta0, lam), rtol=1e-6, atol=1e-8)


@pytest.mark.parametrize("kind", ["deficient", "full"])
def test_leverage_is_the_diagonal_of_the_hat_matrix_and_below_one(make_case: Callable[[str], Case], kind: str) -> None:
    X, y, beta0 = make_case(kind)
    lam = 1.16
    hat = X @ np.linalg.inv(X.T @ X + lam * np.eye(X.shape[1])) @ X.T
    leverage = fit(X, y, beta0, lam).leverage
    np.testing.assert_allclose(leverage, np.diag(hat), atol=1e-10)
    assert np.all(leverage >= 0.0)
    assert np.all(leverage < 1.0)


def test_the_exploratory_grid_survives_a_rank_deficient_design(make_case: Callable[[str], Case]) -> None:
    # The grid's floor of 1e-6 is what keeps a near-interpolating row from reaching a leverage of 1.
    X, y, beta0 = make_case("deficient")
    _, scores = fit_grid(X, y, beta0, EXPLORATORY_GRID.values())
    assert np.all(np.isfinite(scores))
