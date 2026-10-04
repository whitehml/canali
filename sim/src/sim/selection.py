"""Alliance selection."""

from __future__ import annotations

from warehouse.rules.model import Structure


def alliance_count(structure: Structure, n_teams: int) -> int:
    """Number of playoff alliances for a field, from the rule pack's recommended brackets."""
    for bracket in structure.alliance_brackets:
        if bracket.min_teams <= n_teams <= bracket.max_teams:
            return bracket.alliances
    raise ValueError(f"no alliance bracket covers a {n_teams}-team field")
