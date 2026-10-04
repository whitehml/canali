"""The double-elimination playoff."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

from warehouse.rules.model import Structure

DOUBLE_ELIMINATION = "double_elimination_scaled"
MAX_REPLAYS = 100

Alliance = tuple[int, ...]
FinalFormat = Literal["best_of_three", "double_elimination"]


class PlayMatch(Protocol):
    """Play one match and return the red and blue scores."""

    def __call__(self, red: Alliance, blue: Alliance, rng: np.random.Generator) -> tuple[float, float]: ...


@dataclass(frozen=True, slots=True)
class Bracket:
    """A playoff bracket for one alliance count.

    Attributes:
        matches: Red and blue entrants of each match before the final, in play order.
        final: Red and blue entrants of the final.
        final_format: Best of three, or a second final when the lower-bracket winner takes the first.
        places: Finishing place of the alliance.
    """

    matches: tuple[tuple[str, str], ...]
    final: tuple[str, str]
    final_format: FinalFormat
    places: Mapping[str, int]


BRACKETS: Mapping[int, Bracket] = {
    2: Bracket(matches=(), final=("A1", "A2"), final_format="best_of_three", places={}),
    4: Bracket(
        matches=(("A1", "A4"), ("A2", "A3"), ("L1", "L2"), ("W1", "W2"), ("L4", "W3")),
        final=("W4", "W5"),
        final_format="double_elimination",
        places={"L3": 4, "L5": 3},
    ),
    6: Bracket(
        matches=(
            ("A4", "A5"),
            ("A3", "A6"),
            ("A1", "W1"),
            ("A2", "W2"),
            ("L3", "L2"),
            ("L4", "L1"),
            ("W3", "W4"),
            ("W6", "W5"),
            ("L7", "W8"),
        ),
        final=("W7", "W9"),
        final_format="double_elimination",
        places={"L5": 5, "L6": 5, "L8": 4, "L9": 3},
    ),
    8: Bracket(
        matches=(
            ("A1", "A8"),
            ("A4", "A5"),
            ("A2", "A7"),
            ("A3", "A6"),
            ("L1", "L2"),
            ("L3", "L4"),
            ("W1", "W2"),
            ("W3", "W4"),
            ("L7", "W6"),
            ("L8", "W5"),
            ("W7", "W8"),
            ("W10", "W9"),
            ("L11", "W12"),
        ),
        final=("W11", "W13"),
        final_format="double_elimination",
        places={"L5": 7, "L6": 7, "L9": 5, "L10": 5, "L12": 4, "L13": 3},
    ),
}


def _decide(red: int, blue: int, alliances: Sequence[Alliance], play: PlayMatch, rng: np.random.Generator) -> bool:
    """True when red wins."""
    for _ in range(MAX_REPLAYS):
        red_score, blue_score = play(alliances[red - 1], alliances[blue - 1], rng)
        if red_score != blue_score:
            return red_score > blue_score
    raise RuntimeError(f"alliances {red} and {blue} tied {MAX_REPLAYS} times in a row")


def playoff(
    structure: Structure, alliances: Sequence[Alliance], play: PlayMatch, rng: np.random.Generator
) -> tuple[int, ...]:
    """Play the bracket; returns each alliance's finishing place, in seed order."""
    if not structure.playoff_implemented or structure.playoff_structure != DOUBLE_ELIMINATION:
        raise NotImplementedError(f"playoff structure {structure.playoff_structure!r} is not implemented")
    bracket = BRACKETS.get(len(alliances))
    if bracket is None:
        raise ValueError(f"no bracket for {len(alliances)} alliances")

    seed: dict[str, int] = {f"A{s}": s for s in range(1, len(alliances) + 1)}
    for n, (red_ref, blue_ref) in enumerate(bracket.matches, start=1):
        red, blue = seed[red_ref], seed[blue_ref]
        red_won = _decide(red, blue, alliances, play, rng)
        seed[f"W{n}"], seed[f"L{n}"] = (red, blue) if red_won else (blue, red)

    red, blue = seed[bracket.final[0]], seed[bracket.final[1]]
    if bracket.final_format == "best_of_three":
        wins = {red: 0, blue: 0}
        while max(wins.values()) < 2:
            wins[red if _decide(red, blue, alliances, play, rng) else blue] += 1
        champion = red if wins[red] == 2 else blue
    elif _decide(red, blue, alliances, play, rng):
        champion = red
    else:
        champion = red if _decide(red, blue, alliances, play, rng) else blue

    places = {champion: 1, (blue if champion == red else red): 2}
    places |= {seed[ref]: place for ref, place in bracket.places.items()}
    return tuple(places[s] for s in range(1, len(alliances) + 1))
