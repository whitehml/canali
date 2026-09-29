"""The pRidge estimator and leave-one-out cross-validation.

    OLS    : beta = (X'X)^-1 X'y
    Ridge  : beta = (X'X + lam I)^-1 X'y
    pRidge : beta = (X'X + lam I)^-1 (X'y + lam beta0)

Every function is pure in `(X, y, beta0, lam)`. `X'X` does not depend on lambda
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pridge.design import Matrix, Vector


class SingularFitError(ValueError):
    """A fit is asked for at a lambda that does not regularize it, or a row is interpolated."""


@dataclass(frozen=True, slots=True)
class Gram:
    """The lambda-independent half of a fit for one design and response.

    Holds the eigendecomposition of `X'X` and `X'y`. It is reusable across lambdas, not across responses.
    """

    eigvals: Vector
    eigvecs: Matrix
    xty: Vector

    @classmethod
    def build(cls, X: Matrix, y: Vector) -> Gram:
        if X.ndim != 2:
            raise ValueError(f"X must be 2-D, got shape {X.shape}")
        if y.shape != (X.shape[0],):
            raise ValueError(f"y must have shape ({X.shape[0]},), got {y.shape}")
        eigvals, eigvecs = np.linalg.eigh(X.T @ X)
        return cls(eigvals=eigvals, eigvecs=eigvecs, xty=X.T @ y)

    def inverse(self, lam: float) -> Matrix:
        """`(X'X + lam I)^-1`, from the cached decomposition."""
        if lam <= 0.0:
            raise SingularFitError(f"lambda must be strictly positive, got {lam}")
        return (self.eigvecs * (1.0 / (self.eigvals + lam))) @ self.eigvecs.T


@dataclass(frozen=True, slots=True)
class Fit:
    """One pRidge fit at one lambda.

    `beta` follows the design's team order. `residuals` are `y - X beta` on the original response scale. `leverage` is
    the diagonal of the hat matrix `X (X'X + lam I)^-1 X'`.
    """

    beta: Vector
    lam: float
    residuals: Vector
    leverage: Vector

    @property
    def effective_dof(self) -> float:
        """`tr(H)`, the degrees of freedom the shrunk fit spends."""
        return float(self.leverage.sum())

    def loocv_residuals(self) -> Vector:
        """Leave-one-out residuals, `e_i / (1 - h_ii)`.

        Exact for pRidge because beta0 is fixed across folds.
        """
        denominator = 1.0 - self.leverage
        if np.any(denominator <= 0.0):
            raise SingularFitError("leverage reached 1")
        return self.residuals / denominator

    def loocv_mse(self) -> float:
        """Mean squared leave-one-out error."""
        e = self.loocv_residuals()
        return float(e @ e) / e.size


def fit(X: Matrix, y: Vector, beta0: Vector, lam: float, gram: Gram | None = None) -> Fit:
    """Fit pRidge at one lambda, reusing `gram` when given."""
    if beta0.shape != (X.shape[1],):
        raise ValueError(f"beta0 must have shape ({X.shape[1]},), got {beta0.shape}")
    gram = gram if gram is not None else Gram.build(X, y)

    a_inv = gram.inverse(lam)
    beta = a_inv @ (gram.xty + lam * beta0)
    leverage = np.einsum("ij,ij->i", X @ a_inv, X)

    return Fit(beta=beta, lam=lam, residuals=y - X @ beta, leverage=leverage)


def fit_grid(X: Matrix, y: Vector, beta0: Vector, grid: Vector) -> tuple[Fit, list[float]]:
    """Fit across a lambda grid and return the fit with the lowest leave-one-out error and every grid score.

    Scores follow ascending lambda. A tie goes to the smaller lambda.
    """
    if grid.size == 0:
        raise ValueError("lambda grid is empty")
    if np.any(grid <= 0.0):
        raise SingularFitError(f"lambda grid must be strictly positive, got min {grid.min()}")

    gram = Gram.build(X, y)
    scores: list[float] = []
    best: Fit | None = None
    best_score = np.inf
    for lam in np.sort(grid):
        candidate = fit(X, y, beta0, float(lam), gram=gram)
        score = candidate.loocv_mse()
        scores.append(score)
        if score < best_score:
            best_score, best = score, candidate

    if best is None:
        raise SingularFitError("no lambda in the grid produced a finite leave-one-out score")
    return best, scores
