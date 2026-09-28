"""Folding alliance rows into rated matches, and which appearances are rated."""

from __future__ import annotations

import uuid

from epa.corpus import pair_alliances
from warehouse.client import MatchRow


def _row(
    match_id: uuid.UUID,
    alliance: str,
    teams: tuple[int, ...],
    *,
    no_shows: tuple[bool, ...] | None = None,
    surrogates: tuple[bool, ...] | None = None,
    dqs: tuple[bool, ...] | None = None,
    score_no_foul: float = 50.0,
    ordinal: int = 1,
    level: str = "QUALIFICATION",
) -> MatchRow:
    n = len(teams)
    return MatchRow(
        match_id=match_id,
        event_id=uuid.UUID(int=0),
        season=2025,
        event_code="USPAX",
        event_type="Qualifier",
        event_date_start=None,
        level=level,
        series=0,
        match_number=ordinal,
        event_match_ordinal=ordinal,
        alliance=alliance,
        score=score_no_foul,
        opponent_score=0.0,
        score_auto=0.0,
        score_no_foul=score_no_foul,
        team_numbers=teams,
        surrogates=surrogates or (False,) * n,
        no_shows=no_shows or (False,) * n,
        dqs=dqs or (False,) * n,
    )


def _match(*rows: MatchRow) -> list[object]:
    return list(pair_alliances(rows))


# ---------------------------------------------------------------------------------------------------- appearances


def test_a_surrogate_is_rated() -> None:
    mid = uuid.uuid4()
    match = next(pair_alliances([_row(mid, "RED", (1, 2), surrogates=(True, False)), _row(mid, "BLUE", (3, 4))]))
    assert match.red.teams == (1, 2)


def test_a_no_show_is_not_rated() -> None:
    mid = uuid.uuid4()
    match = next(pair_alliances([_row(mid, "RED", (1, 2), no_shows=(False, True)), _row(mid, "BLUE", (3, 4))]))
    assert match.red.teams == (1,)


def test_a_disqualified_team_is_rated() -> None:
    mid = uuid.uuid4()
    match = next(pair_alliances([_row(mid, "RED", (1, 2), dqs=(True, False)), _row(mid, "BLUE", (3, 4))]))
    assert match.red.teams == (1, 2)


def test_a_match_missing_a_side_is_skipped() -> None:
    assert _match(_row(uuid.uuid4(), "RED", (1, 2))) == []


def test_a_side_with_no_rateable_team_drops_the_match() -> None:
    mid = uuid.uuid4()
    assert _match(_row(mid, "RED", (1, 2), no_shows=(True, True)), _row(mid, "BLUE", (3, 4))) == []


# -------------------------------------------------------------------------------------------------------- pairing


def test_alliances_are_paired_whichever_row_comes_first() -> None:
    mid = uuid.uuid4()
    match = next(
        pair_alliances([_row(mid, "BLUE", (3, 4), score_no_foul=30.0), _row(mid, "RED", (1, 2), score_no_foul=60.0)])
    )
    assert (match.red.teams, match.red.score_no_foul, match.blue.score_no_foul) == ((1, 2), 60.0, 30.0)


def test_stream_order_survives_interleaved_matches() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(a, "RED", (1, 2), ordinal=1),
        _row(b, "RED", (5, 6), ordinal=2),
        _row(a, "BLUE", (3, 4), ordinal=1),
        _row(b, "BLUE", (7, 8), ordinal=2),
    ]
    assert [match.event_match_ordinal for match in pair_alliances(rows)] == [1, 2]


# --------------------------------------------------------------------------------------------------------- levels


def test_a_match_knows_whether_it_is_an_elimination() -> None:
    for level, expected in [("QUALIFICATION", False), ("SEMIFINAL", True), ("FINAL", True), ("PLAYOFF", True)]:
        mid = uuid.uuid4()
        match = next(pair_alliances([_row(mid, "RED", (1, 2), level=level), _row(mid, "BLUE", (3, 4), level=level)]))
        assert match.is_elimination is expected, level
