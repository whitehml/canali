"""The event-sequential match stream.

Events are consumed in ``pub.v_event_sequence`` order and matches within an event in published ordinal order.

Inclusion rules:

===========================  ==========================================
Superseded replays           excluded, ``v_match_rating_input`` filters them
PRACTICE, scrimmages         excluded by the same view
Remote and hybrid events     not in the corpus at all, filtered at ingest
Surrogate appearances        included, real matches against real opponents
No-shows                     excluded, the team did not play
Breakdown disagrees with     excluded, the warehouse does not return the match
the official score
Playoffs                     included, damped by ``SeasonConstants.elim_weight``
DQ'd teams                   included, the score is real
===========================  ==========================================
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from warehouse.client import MatchRow, Warehouse

QUALIFICATION_MATCHES: tuple[str, ...] = ("QUALIFICATION",)
ELIMINATION_MATCHES: tuple[str, ...] = ("SEMIFINAL", "FINAL", "PLAYOFF")
RATED_MATCHES: tuple[str, ...] = QUALIFICATION_MATCHES + ELIMINATION_MATCHES


@dataclass(frozen=True, slots=True)
class Alliance:
    """One side of a match, reduced to what the update rule needs."""

    teams: tuple[int, ...]
    score_no_foul: float


@dataclass(frozen=True, slots=True)
class RatedMatch:
    """A match both alliances of which are present and rateable."""

    match_id: uuid.UUID
    event_id: uuid.UUID
    season: int
    event_code: str
    event_match_ordinal: int
    red: Alliance
    blue: Alliance

    level: str = "QUALIFICATION"
    event_type: str | None = None
    event_date: date | None = None

    @property
    def is_elimination(self) -> bool:
        return self.level in ELIMINATION_MATCHES

    def sides(self) -> tuple[tuple[Alliance, Alliance], tuple[Alliance, Alliance]]:
        """``(own, opponent)`` for each alliance, in a fixed order."""
        return ((self.red, self.blue), (self.blue, self.red))


def _alliance(row: MatchRow) -> Alliance:
    return Alliance(teams=row.rated_teams(), score_no_foul=row.score_no_foul)


def pair_alliances(rows: Sequence[MatchRow]) -> Iterator[RatedMatch]:
    """Fold alliance-grained rows into matches, preserving stream order."""
    pending: dict[uuid.UUID, MatchRow] = {}
    for row in rows:
        other = pending.pop(row.match_id, None)
        if other is None:
            pending[row.match_id] = row
            continue
        red, blue = (other, row) if other.alliance == "RED" else (row, other)
        red_side, blue_side = _alliance(red), _alliance(blue)
        if not red_side.teams or not blue_side.teams:
            continue
        yield RatedMatch(
            match_id=red.match_id,
            event_id=red.event_id,
            season=red.season,
            event_code=red.event_code,
            event_match_ordinal=red.event_match_ordinal,
            red=red_side,
            blue=blue_side,
            level=red.level,
            event_type=red.event_type,
            event_date=red.event_date_start,
        )


def season_stream(
    warehouse: Warehouse,
    season: int,
    *,
    levels: Sequence[str] = RATED_MATCHES,
) -> Iterator[RatedMatch]:
    """Every rated match of a season, event-sequential."""
    yield from pair_alliances(warehouse.season_matches(season, levels=levels))


def event_stream(
    warehouse: Warehouse,
    event_id: uuid.UUID,
    *,
    levels: Sequence[str] = RATED_MATCHES,
) -> Iterator[RatedMatch]:
    """One event's rated matches."""
    yield from pair_alliances(warehouse.event_matches(event_id, levels=levels))


def season_breakdowns(
    warehouse: Warehouse,
    season: int,
    columns: Sequence[str],
    *,
    levels: Sequence[str] = RATED_MATCHES,
) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
    """Component values for a whole season, keyed by match and alliance."""
    return warehouse.season_breakdowns(season, columns, levels=levels)
