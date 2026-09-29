"""The season replay: which rows it writes, what a match moves, and where a rating starts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from epa.constants import SeasonConstants
from epa.corpus import Alliance, RatedMatch
from epa.model import Schedule
from epa.partition import TOTAL, resolve_partition
from epa.replay import (
    TAG_POST_EVENT,
    TAG_PRE_EVENT,
    TAG_SEASON_START,
    ReplayResult,
    TeamSeed,
    replay_season,
)
from epa.scale import Carryover, LayoffBoost, Scale, carry_forward
from warehouse.rules.model import to_column_name

SEASON = 2025
SEASON_SCALE = Scale(season=SEASON, mu=80.0, sigma=40.0, rows=1000)
INIT_SCALE = Scale(season=SEASON, mu=50.0, sigma=30.0, rows=4000)
CARRY = Carryover(year_one_weight=0.7, mean_reversion=0.4)
CONSTANTS = SeasonConstants(
    season=SEASON, k=Schedule.constant(0.5), m=Schedule.constant(0.0), carryover=CARRY, elim_weight=1.0 / 3.0
)
TOTAL_ONLY = resolve_partition(SEASON, (), [])
FIRST, SECOND = uuid.UUID(int=1), uuid.UUID(int=2)

_PHASE_COMPONENTS = [
    {
        "name": name,
        "column_name": to_column_name(name),
        "level": "alliance",
        "kind": "numeric",
        "is_subtotal": True,
        "is_derived": False,
        "partition_group": "phase",
    }
    for name in ("autoPoints", "teleopPoints")
]
PHASE = resolve_partition(SEASON, ("autoPoints", "teleopPoints"), _PHASE_COMPONENTS)


def _match(
    event_id: uuid.UUID,
    ordinal: int,
    red: tuple[int, ...],
    blue: tuple[int, ...],
    *,
    red_score: float = 100.0,
    blue_score: float = 60.0,
    level: str = "QUALIFICATION",
    day: date | None = None,
) -> RatedMatch:
    return RatedMatch(
        match_id=uuid.uuid5(event_id, str(ordinal)),
        event_id=event_id,
        season=SEASON,
        event_code=f"USPA{event_id.int}",
        event_match_ordinal=ordinal,
        red=Alliance(teams=red, score_no_foul=red_score),
        blue=Alliance(teams=blue, score_no_foul=blue_score),
        level=level,
        event_type="Qualifier",
        event_date=day,
    )


TWO_EVENTS = [
    _match(FIRST, 1, (1, 2), (3, 4), day=date(2025, 10, 4)),
    _match(FIRST, 2, (1, 3), (2, 4), red_score=90.0, blue_score=110.0, day=date(2025, 10, 4)),
    _match(SECOND, 1, (1, 5), (2, 6), red_score=130.0, blue_score=70.0, day=date(2025, 11, 18)),
    _match(SECOND, 2, (1, 6), (2, 5), red_score=80.0, blue_score=95.0, day=date(2025, 11, 18)),
]


def _replay(matches: Sequence[RatedMatch], **overrides: Any) -> ReplayResult:
    arguments: dict[str, Any] = {
        "constants": CONSTANTS,
        "partition": TOTAL_ONLY,
        "season_scale": SEASON_SCALE,
        "init_scale": INIT_SCALE,
    }
    return replay_season(matches, **(arguments | overrides))


def _phase_breakdowns(matches: Sequence[RatedMatch]) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
    """A third of each alliance's score in auto, the rest in teleop."""
    return {
        (match.match_id, alliance): {
            "auto_points": side.score_no_foul / 3.0,
            "teleop_points": side.score_no_foul * 2 / 3,
        }
        for match in matches
        for alliance, side in (("RED", match.red), ("BLUE", match.blue))
    }


def _rating(result: ReplayResult, team: int, tag: str, event_id: uuid.UUID | None = None) -> float:
    (row,) = [r for r in result.rows if (r.team_number, r.tag, r.event_id) == (team, tag, event_id)]
    return row.epa_scaled


# ----------------------------------------------------------------------------------------------------------- rows


def test_only_a_season_start_row_has_no_event_and_no_ordinal() -> None:
    rows = _replay(TWO_EVENTS).rows
    for row in rows:
        assert (row.event_id is None) == (row.as_of_match is None) == (row.tag == TAG_SEASON_START), row
    assert sorted(r.team_number for r in rows if r.tag == TAG_SEASON_START) == [1, 2, 3, 4, 5, 6]
    assert {r.as_of_match for r in rows if r.tag == TAG_PRE_EVENT} == {0}
    assert {(r.event_id, r.as_of_match) for r in rows if r.tag == TAG_POST_EVENT} == {(FIRST, 2), (SECOND, 2)}


# -------------------------------------------------------------------------------------------------------- matches


def test_both_alliances_are_predicted_from_the_state_before_the_match() -> None:
    result = _replay(TWO_EVENTS[:2], previous_norm={1: 1800.0, 3: 1600.0})
    start = {r.team_number: r.epa_scaled for r in result.rows if r.tag == TAG_SEASON_START}
    red, blue, next_red, _ = result.predictions
    assert (red.predicted, red.opponent_predicted) == (start[1] + start[2], start[3] + start[4])
    assert (blue.predicted, blue.opponent_predicted) == (start[3] + start[4], start[1] + start[2])
    assert next_red.predicted != start[1] + start[3]


def test_an_elimination_moves_a_rating_by_its_weight() -> None:
    def moved(level: str) -> float:
        result = _replay([_match(FIRST, 1, (1, 2), (3, 4), level=level)])
        return _rating(result, 1, TAG_POST_EVENT, FIRST) - _rating(result, 1, TAG_SEASON_START)

    assert moved("QUALIFICATION") != 0.0
    assert moved("PLAYOFF") == pytest.approx(moved("QUALIFICATION") * CONSTANTS.elim_weight)


def test_an_elimination_does_not_advance_the_learning_rate_schedule() -> None:
    stream = [
        _match(FIRST, 1, (1, 2), (3, 4)),
        _match(FIRST, 2, (1, 2), (3, 4), level="PLAYOFF"),
        _match(FIRST, 3, (1, 2), (3, 4), level="PLAYOFF"),
    ]
    predictions = _replay(stream).predictions
    assert [p.matches_played for p in predictions if p.team_numbers == (1, 2)] == [0, 1, 1]


def test_a_match_without_a_breakdown_is_skipped_rather_than_zero_filled() -> None:
    breakdowns = _phase_breakdowns(TWO_EVENTS[:1])
    skipped = _replay(TWO_EVENTS[:2], partition=PHASE, breakdowns=breakdowns)
    only_first = _replay(TWO_EVENTS[:1], partition=PHASE, breakdowns=breakdowns)
    assert skipped.skipped_missing_breakdown == 1
    assert len(skipped.predictions) == 2
    assert {team: _rating(skipped, team, TAG_POST_EVENT, FIRST) for team in (1, 2, 3, 4)} == {
        team: _rating(only_first, team, TAG_POST_EVENT, FIRST) for team in (1, 2, 3, 4)
    }


def test_a_component_replay_carries_the_same_total_as_a_total_only_one() -> None:
    boost = LayoffBoost(sigma_per_30d=0.15, cap_days=60.0)
    components = _replay(TWO_EVENTS, partition=PHASE, breakdowns=_phase_breakdowns(TWO_EVENTS), layoff_boost=boost)
    total = _replay(TWO_EVENTS, layoff_boost=boost)
    assert [r.epa_scaled for r in components.rows] == pytest.approx([r.epa_scaled for r in total.rows])


# ------------------------------------------------------------------------------------------------------- starting


def test_a_start_is_placed_by_the_init_scale() -> None:
    rookie = carry_forward(previous_norm=None, second_norm=None, carryover=CARRY, init_scale=INIT_SCALE).scaled
    starts = {r.epa_scaled for r in _replay(TWO_EVENTS).rows if r.tag == TAG_SEASON_START}
    assert starts == {rookie}
    assert (
        rookie != carry_forward(previous_norm=None, second_norm=None, carryover=CARRY, init_scale=SEASON_SCALE).scaled
    )


def test_a_row_is_provisional_exactly_when_its_init_scale_was_borrowed() -> None:
    borrowed_start = _replay(TWO_EVENTS, init_scale=replace(INIT_SCALE, provisional=True)).rows
    borrowed_season = _replay(TWO_EVENTS, season_scale=None).rows
    assert all(r.scale_provisional for r in borrowed_start)
    assert not any(r.scale_provisional for r in borrowed_season)


def test_a_layoff_is_credited_on_entering_a_later_event_and_not_the_first() -> None:
    boost = LayoffBoost(sigma_per_30d=0.15, cap_days=60.0)
    result = _replay(TWO_EVENTS, layoff_boost=boost)
    assert _rating(result, 1, TAG_PRE_EVENT, FIRST) == _rating(result, 1, TAG_SEASON_START)
    gained = _rating(result, 1, TAG_PRE_EVENT, SECOND) - _rating(result, 1, TAG_POST_EVENT, FIRST)
    assert gained == pytest.approx(boost.points(45, INIT_SCALE))


def test_a_seeded_team_resumes_its_seed_and_takes_no_season_start_row() -> None:
    result = _replay(TWO_EVENTS[:1], seed={1: TeamSeed(series={TOTAL: 70.0}, played=6)})
    assert sorted(r.team_number for r in result.rows if r.tag == TAG_SEASON_START) == [2, 3, 4]
    assert _rating(result, 1, TAG_PRE_EVENT, FIRST) == 70.0
