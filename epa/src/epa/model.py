"""The EPA update rule.

Elo-shaped, with a learning rate ``K`` and ``M`` both piecewise in matches played.
Learns against own alliance non-foul score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ALLIANCE_SIZE = 2


@dataclass(frozen=True, slots=True)
class Schedule:
    """A parameter that is piecewise constant in matches played.

    ``breakpoints`` are the match counts at which the value changes, ascending.
    ``values[i]`` applies while ``played`` is below ``breakpoints[i]``, and ``values[-1]`` applies thereafter.
    """

    breakpoints: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.values) != len(self.breakpoints) + 1:
            raise ValueError(
                f"values must be one longer than breakpoints; got {len(self.values)} and {len(self.breakpoints)}"
            )
        if list(self.breakpoints) != sorted(self.breakpoints):
            raise ValueError(f"breakpoints must be ascending; got {self.breakpoints}")

    @classmethod
    def constant(cls, value: float) -> Schedule:
        return cls(breakpoints=(), values=(value,))

    def at(self, played: int) -> float:
        for index, edge in enumerate(self.breakpoints):
            if played < edge:
                return self.values[index]
        return self.values[-1]


@dataclass(frozen=True, slots=True)
class MatchUpdate:
    """What one match does to the ratings of the teams on one alliance.

    The alliance learns from its own score against its own prediction. The opponent reaches ``residual`` only through
    ``M``, and not at all at ``M = 0``.
    """

    deltas: dict[int, float]
    predicted: dict[str, float]
    residual: float


def predict(epa: Mapping[int, float], teams: Sequence[int]) -> float:
    return sum(epa[team] for team in teams)


def residual(
    *,
    own_score: float,
    own_predicted: float,
    opponent_score: float,
    opponent_predicted: float,
    m: float,
) -> float:
    weighted = (own_score - own_predicted) - m * (opponent_score - opponent_predicted)
    return weighted / (1.0 + m)


def update_for_match(
    epa: Mapping[int, float],
    *,
    own_teams: Sequence[int],
    opponent_teams: Sequence[int],
    own_score: float,
    opponent_score: float,
    k: float,
    m: float,
) -> MatchUpdate:
    if not own_teams:
        return MatchUpdate(deltas={}, predicted={"own": 0.0, "opponent": 0.0}, residual=0.0)

    own_predicted = predict(epa, own_teams)
    opponent_predicted = predict(epa, opponent_teams) if opponent_teams else 0.0
    error = residual(
        own_score=own_score,
        own_predicted=own_predicted,
        opponent_score=opponent_score,
        opponent_predicted=opponent_predicted,
        m=m,
    )
    delta = (k / ALLIANCE_SIZE) * error
    return MatchUpdate(
        deltas=dict.fromkeys(own_teams, delta),
        predicted={"own": own_predicted, "opponent": opponent_predicted},
        residual=error,
    )
