"""Next-match prediction.

Everything here predicts the next match a team plays from state before it.
"""

from __future__ import annotations

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

    event_ordinal: int = 0
    event_type: str | None = None
    is_elimination: bool = False

    @property
    def error(self) -> float:
        return self.predicted - self.actual

    @property
    def predicted_margin(self) -> float:
        return self.predicted - self.opponent_predicted


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


def early_event_breakdown(predictions: Sequence[Prediction], *, first_n: int = 3) -> dict[str, tuple[float, int]]:
    """Error split by matches played."""
    early = [p for p in predictions if p.matches_played < first_n]
    later = [p for p in predictions if p.matches_played >= first_n]
    return {
        f"first_{first_n}": (score_metrics(early)[0], len(early)),
        "rest": (score_metrics(later)[0], len(later)),
    }
