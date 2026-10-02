"""Round assignment: which match each team on an alliance is playing, as the models see it."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from analysis.comparison import AllianceKey
from warehouse.client import MatchRow


def event_rounds(rows: Sequence[MatchRow]) -> dict[AllianceKey, int]:
    """The round of each qualification alliance whose teams are all playing their nth match, n being the round.

    A surrogate appearance counts as a match played and a no-show does not.
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
                rounds[(row.match_id, row.alliance)] = counts.pop() + 1
        for row in by_ordinal[ordinal]:
            for team in row.rated_teams():
                played[team] += 1
    return rounds
