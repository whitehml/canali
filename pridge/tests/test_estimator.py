"""Test the estimator: the fit itself, the inputs it refuses, and the grid search."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from pridge.design import Matrix, Vector
from pridge.estimator import Gram, SingularFitError, fit, fit_grid

Case = tuple[Matrix, Vector, Vector]

# ------------------------------------------------------------------------------------------------------------- fit


@pytest.mark.parametrize("kind", ["deficient", "full"])
def test_the_fit_solves_the_defining_equation(make_case: Callable[[str], Case], kind: str) -> None:
    X, y, beta0 = make_case(kind)
    gram = Gram.build(X, y)
    for lam in (0.01, 0.3, 1.16, 4.0, 60.0, 500.0):
        direct = np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ y + lam * beta0)
        np.testing.assert_allclose(fit(X, y, beta0, lam, gram=gram).beta, direct, rtol=1e-8, atol=1e-8)


def test_a_non_positive_lambda_is_refused(make_case: Callable[[str], Case]) -> None:
    X, y, beta0 = make_case("full")
    for lam in (0.0, -1.0):
        with pytest.raises(SingularFitError):
            fit(X, y, beta0, lam)
        with pytest.raises(SingularFitError):
            Gram.build(X, y).inverse(lam)
        with pytest.raises(SingularFitError):
            fit_grid(X, y, beta0, np.array([lam, 1.0]))


@pytest.mark.parametrize(
    "call",
    [
        lambda X, y, b: Gram.build(X[0], y),
        lambda X, y, b: Gram.build(X, y[:-1]),
        lambda X, y, b: fit(X, y, b[:-1], 1.0),
        lambda X, y, b: fit_grid(X, y, b, np.array([])),
    ],
    ids=["x-not-2d", "y-wrong-length", "beta0-wrong-shape", "empty-grid"],
)
def test_malformed_inputs_are_refused(make_case: Callable[[str], Case], call: Callable[..., object]) -> None:
    X, y, beta0 = make_case("full")
    with pytest.raises(ValueError):
        call(X, y, beta0)


# ------------------------------------------------------------------------------------------------------------ grid


def test_the_grid_search_returns_the_lowest_score_whatever_the_grid_order(make_case: Callable[[str], Case]) -> None:
    X, y, beta0 = make_case("full")
    shuffled = np.array([3.0, 0.3, 1.0, 10.0, 0.1])
    best, scores = fit_grid(X, y, beta0, shuffled)

    ascending = np.sort(shuffled)
    expected = [fit(X, y, beta0, float(lam)).loocv_mse() for lam in ascending]
    np.testing.assert_allclose(scores, expected)
    assert best.lam == ascending[int(np.argmin(expected))]

    again, again_scores = fit_grid(X, y, beta0, ascending)
    assert again.lam == best.lam
    np.testing.assert_allclose(again_scores, scores)
