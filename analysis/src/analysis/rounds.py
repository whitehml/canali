"""Round assignment: how many matches each team on an alliance had already played."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from analysis.comparison import AllianceKey
from warehouse.client import MatchRow, Warehouse
from warehouse.tier import tier_for


def event_rounds(rows: Sequence[MatchRow]) -> dict[AllianceKey, int]:
    """The round of each qualification alliance whose teams have all played the same number of earlier matches.

    A surrogate appearance counts as a match played and a no-show does not. An alliance whose teams disagree has no
    round.
    """
    qualification = [row for row in rows if row.level == "QUALIFICATION"]
    by_ordinal: dict[int, list[MatchRow]] = defaultdict(list)
    for row in qualification:
        by_ordinal[row.event_match_ordinal].append(row)

    played: dict[int, int] = defaultdict(int)
    rounds: dict[AllianceKey, int] = {}
    for ordinal in sorted(by_ordinal):
        for row in by_ordinal[ordinal]:
            counts = {played[team] for team in row.rated_teams()}
            if len(counts) == 1:
                rounds[(row.match_id, row.alliance)] = counts.pop()
        for row in by_ordinal[ordinal]:
            for team in row.rated_teams():
                played[team] += 1
    return rounds


def season_rounds(warehouse: Warehouse, season: int) -> dict[AllianceKey, int]:
    """`event_rounds` over every rated event of a season."""
    out: dict[AllianceKey, int] = {}
    for event in warehouse.events(season):
        if tier_for(event.event_type) is not None:
            out.update(event_rounds(warehouse.event_matches(event.event_id)))
    return out
