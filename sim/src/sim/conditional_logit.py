"""Choosing one option among several by weighted traits."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

Floats = npt.NDArray[np.float64]


def choice_probabilities(features: Floats, coefficients: Sequence[float]) -> Floats:
    """Each option's chance of being chosen, from features shaped ``(option, trait)``."""
    eta = np.asarray(features, dtype=np.float64) @ np.asarray(coefficients, dtype=np.float64)
    weights = np.exp(eta - eta.max())
    return np.asarray(weights / weights.sum(), dtype=np.float64)


def fit_coefficients(features: Sequence[Floats], chosen: Sequence[int]) -> Floats:
    """The weights, one per trait, under which the choices that happened were most likely."""
    from scipy import optimize

    if not features:
        raise ValueError("no choices to fit on")
    x = np.vstack(features).astype(np.float64)
    sizes = np.array([len(f) for f in features])
    starts = np.r_[0, np.cumsum(sizes)[:-1]]
    picked = starts + np.asarray(chosen)

    def loss(b: Floats) -> tuple[float, Floats]:
        eta = x @ b
        peak = np.maximum.reduceat(eta, starts)
        weights = np.exp(eta - np.repeat(peak, sizes))
        total = np.add.reduceat(weights, starts)
        p = weights / np.repeat(total, sizes)
        expected = np.add.reduceat(p[:, None] * x, starts)
        nll = -float(np.sum(eta[picked] - np.log(total) - peak))
        return nll, -(x[picked] - expected).sum(axis=0)

    return np.asarray(optimize.minimize(loss, np.zeros(x.shape[1]), jac=True, method="BFGS").x, dtype=np.float64)
