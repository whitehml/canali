"""Next-match prediction and calibration.

Everything here predicts the next match a team plays from state before it.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from warehouse.tier import EventTier, tier_for


@dataclass(frozen=True, slots=True)
class Prediction:
    """One held-out alliance-row: what was predicted, and what happened."""

    season: int
    team_numbers: tuple[int, ...]
    predicted: float
    actual: float
    matches_played: int
    opponent_predicted: float = 0.0
    opponent_actual: float = 0.0

    event_ordinal: int = 0
    event_type: str | None = None
    is_elimination: bool = False
    scale_sigma: float = 1.0

    @property
    def error(self) -> float:
        return self.predicted - self.actual

    @property
    def predicted_margin(self) -> float:
        return self.predicted - self.opponent_predicted

    @property
    def normalized_margin(self) -> float:
        """The predicted margin in units of the season's own spread."""
        return self.predicted_margin / (self.scale_sigma or 1.0)

    @property
    def outscored(self) -> bool:
        """Whether this alliance scored more no-foul points, which is not the same as whether it won."""
        return self.actual > self.opponent_actual


def fit_surface(predictions: Sequence[Prediction]) -> list[Prediction]:
    """The rows a constant may be fitted on."""
    return [p for p in predictions if tier_for(p.event_type) is not None and not p.is_elimination]


def of_tier(predictions: Sequence[Prediction], tier: EventTier) -> list[Prediction]:
    """One tier's cross-section, applied to ``fit_surface`` output so it covers the rows the pooled number averaged."""
    return [p for p in predictions if tier_for(p.event_type) is tier]


def score_metrics(predictions: Sequence[Prediction]) -> tuple[float, float]:
    """``(mse, mae)`` for a set of predictions."""
    if not predictions:
        return (float("nan"), float("nan"))
    errors = [p.error for p in predictions]
    return (
        statistics.fmean(e * e for e in errors),
        statistics.fmean(abs(e) for e in errors),
    )


@dataclass(frozen=True, slots=True)
class OutscoreModel:
    """``P(outscore) = sigmoid(intercept + slope * predicted_margin)``.

    Outscore, not win: both sides live in no-foul points, so the two disagree whenever fouls swing a match.
    """

    intercept: float
    slope: float

    def probability(self, predicted_margin: float) -> float:
        z = self.intercept + self.slope * predicted_margin
        # Guard the exponential rather than the probability: overflow here produces a nan that poisons a log-loss.
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-min(z, 700.0)))
        exp_z = math.exp(max(z, -700.0))
        return exp_z / (1.0 + exp_z)

    def probability_of(self, prediction: Prediction) -> float:
        """The probability for one prediction."""
        return self.probability(prediction.normalized_margin)

    def log_loss(self, predictions: Sequence[Prediction]) -> float:
        if not predictions:
            return float("nan")
        total = 0.0
        for p in predictions:
            q = min(max(self.probability_of(p), 1e-12), 1 - 1e-12)
            total -= math.log(q) if p.outscored else math.log(1.0 - q)
        return total / len(predictions)


def fit_outscore_model(
    predictions: Sequence[Prediction], *, iterations: int = 25, tolerance: float = 1e-9
) -> OutscoreModel:
    """Logistic fit by Newton-Raphson on the normalized margin, standardized internally to condition the Hessian."""
    if len(predictions) < 2:
        raise ValueError("an outscore model needs at least two observations")
    margins = [p.normalized_margin for p in predictions]
    outcomes = [1.0 if p.outscored else 0.0 for p in predictions]
    centre = statistics.fmean(margins)
    spread = statistics.stdev(margins) or 1.0
    xs = [(margin - centre) / spread for margin in margins]

    a, b = 0.0, 0.0
    for _ in range(iterations):
        # Gradient and Hessian of the log-likelihood, accumulated in one pass.
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, y in zip(xs, outcomes, strict=True):
            z = a + b * x
            pred = 1.0 / (1.0 + math.exp(-max(min(z, 700.0), -700.0)))
            residual = pred - y
            weight = pred * (1.0 - pred)
            g_a += residual
            g_b += residual * x
            h_aa += weight
            h_ab += weight * x
            h_bb += weight * x * x

        determinant = h_aa * h_bb - h_ab * h_ab
        if abs(determinant) < 1e-12:
            # Perfectly separable or degenerate; stepping further would diverge.
            break
        step_a = (h_bb * g_a - h_ab * g_b) / determinant
        step_b = (h_aa * g_b - h_ab * g_a) / determinant
        a -= step_a
        b -= step_b
        if abs(step_a) < tolerance and abs(step_b) < tolerance:
            break

    slope = b / spread
    return OutscoreModel(intercept=a - slope * centre, slope=slope)


@dataclass(frozen=True, slots=True)
class Calibration:
    """Reliability of the outscore model: does a 70% outscore about 70% of the time?"""

    bins: tuple[tuple[float, float, int], ...]

    @property
    def worst_gap(self) -> float:
        return max((abs(p - o) for p, o, _ in self.bins), default=0.0)

    def summary(self) -> str:
        rows = "; ".join(f"{p:.0%}->{o:.0%} (n={n})" for p, o, n in self.bins)
        return f"worst gap {self.worst_gap:.1%} | {rows}"


def calibrate(model: OutscoreModel, predictions: Sequence[Prediction], *, bins: int = 10) -> Calibration:
    """Group by predicted probability and compare to the observed outscore rate."""
    buckets: dict[int, list[Prediction]] = {}
    for p in predictions:
        q = model.probability_of(p)
        buckets.setdefault(min(int(q * bins), bins - 1), []).append(p)
    out = []
    for index in sorted(buckets):
        group = buckets[index]
        predicted = statistics.fmean(model.probability_of(p) for p in group)
        observed = statistics.fmean(1.0 if p.outscored else 0.0 for p in group)
        out.append((predicted, observed, len(group)))
    return Calibration(bins=tuple(out))


def early_event_breakdown(predictions: Sequence[Prediction], *, first_n: int = 3) -> dict[str, tuple[float, int]]:
    """Error split by matches played."""
    early = [p for p in predictions if p.matches_played < first_n]
    later = [p for p in predictions if p.matches_played >= first_n]
    return {
        f"first_{first_n}": (score_metrics(early)[0], len(early)),
        "rest": (score_metrics(later)[0], len(later)),
    }
