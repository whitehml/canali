"""The computed OPR baseline: the least-squares solve, its responses, and the season pass."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select, text

from warehouse.derive.opr import RESPONSES, EventOpr, compute_event_opr, compute_season_opr, write_event_opr
from warehouse.ingest.writer import ensure_teams
from warehouse.schema import core, derived

SEASON = 9999

STRENGTHS = {101: 10.0, 102: 20.0, 103: 30.0, 104: 40.0, 105: 50.0}


def alliance_row(teams: Sequence[int], score: float, auto: float = 0.0, foul: float = 0.0) -> dict[str, Any]:
    return {
        "team_numbers": list(teams),
        "score": score,
        "score_auto": auto,
        "score_no_foul": score - foul,
    }


# ------------------------------------------------------------------------------------------------ the solve


def test_a_separable_design_recovers_the_contributions_that_built_it() -> None:
    teams = sorted(STRENGTHS)
    matches = [alliance_row((a, b), STRENGTHS[a] + STRENGTHS[b]) for i, a in enumerate(teams) for b in teams[i + 1 :]]

    result = compute_event_opr(matches, uuid.uuid4())

    assert result is not None
    assert result.identified
    assert result.observations == len(matches)
    solved = dict(zip(result.teams, result.values["opr_total"], strict=True))
    for team, strength in STRENGTHS.items():
        assert solved[team] == pytest.approx(strength)


def test_each_response_reads_its_own_score() -> None:
    result = compute_event_opr([alliance_row((7,), score=100, auto=30, foul=10)], uuid.uuid4())

    assert result is not None
    assert result.values["opr_total"][0] == pytest.approx(100.0)
    assert result.values["opr_total_np"][0] == pytest.approx(90.0)
    assert result.values["opr_auto"][0] == pytest.approx(30.0)
    assert result.values["opr_teleop"][0] == pytest.approx(60.0)


def test_a_match_with_no_roster_is_dropped_and_an_event_of_them_is_none() -> None:
    rosterless = {"team_numbers": [], "score": 200, "score_auto": 0, "score_no_foul": 200}

    assert compute_event_opr([], uuid.uuid4()) is None
    assert compute_event_opr([rosterless], uuid.uuid4()) is None

    result = compute_event_opr([rosterless, alliance_row((7,), 100)], uuid.uuid4())
    assert result is not None
    assert result.teams == (7,)
    assert result.observations == 1


# ------------------------------------------------------------------------------------------------ the warehouse


def seed_season(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO core.season (season, name, game) VALUES (:s, :n, 'SYNTHETIC') ON CONFLICT (season) DO NOTHING"
        ),
        {"s": SEASON, "n": str(SEASON)},
    )


def seed_event(
    conn: Connection,
    code: str,
    matches: Sequence[tuple[Sequence[int], Sequence[int]]],
    *,
    level: str = "QUALIFICATION",
) -> uuid.UUID:
    """One event whose every match scores each alliance at the sum of its teams' strengths."""
    event_id = uuid.uuid4()
    now = datetime.now(UTC)
    conn.execute(
        core.event.insert(),
        {
            "event_id": event_id,
            "season": SEASON,
            "code": code,
            "name": code,
            "type": "Qualifier",
            "date_start": date(2026, 3, 1),
        },
    )
    ensure_teams(conn, {t for red, blue in matches for t in (*red, *blue)})

    for number, (red, blue) in enumerate(matches, start=1):
        match_id = uuid.uuid4()
        red_score = sum(STRENGTHS[t] for t in red)
        blue_score = sum(STRENGTHS[t] for t in blue)
        conn.execute(
            core.match.insert(),
            {
                "match_id": match_id,
                "event_id": event_id,
                "level": level,
                "series": 0,
                "match_number": number,
                "score_red_final": int(red_score),
                "score_blue_final": int(blue_score),
                "score_red_auto": 0,
                "score_blue_auto": 0,
                "score_red_foul": 0,
                "score_blue_foul": 0,
                "ingested_at_utc": now,
            },
        )
        conn.execute(
            core.match_team.insert(),
            [
                {
                    "match_id": match_id,
                    "station": f"{side.title()}{i}",
                    "alliance": side,
                    "team_number": team,
                }
                for side, teams in (("RED", red), ("BLUE", blue))
                for i, team in enumerate(teams, start=1)
            ],
        )
    return event_id


def opr_rows(conn: Connection, event_id: uuid.UUID) -> dict[int, float]:
    rows = conn.execute(
        select(derived.team_event_opr.c.team_number, derived.team_event_opr.c.opr_total).where(
            derived.team_event_opr.c.event_id == event_id
        )
    ).all()
    return {int(team): float(total) for team, total in rows}


@pytest.mark.db
def test_a_second_write_replaces_the_first(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        seed_season(conn)
        event_id = seed_event(conn, "SYNQ1", [((101, 102), (103, 104))])

    values: dict[str, tuple[float, ...]] = dict.fromkeys(RESPONSES, (1.0, 2.0))
    first = EventOpr(event_id, (101, 102), values, observations=2, rank=2)
    revised = EventOpr(event_id, (101, 102), {**values, "opr_total": (11.0, 22.0)}, observations=4, rank=2)

    with clean_engine.begin() as conn:
        write_event_opr(conn, first)
        write_event_opr(conn, revised)

    with clean_engine.connect() as conn:
        assert opr_rows(conn, event_id) == {101: 11.0, 102: 22.0}


@pytest.mark.db
def test_the_season_pass_counts_what_it_solved_and_what_it_could_not(clean_engine: Engine) -> None:
    separable = [((101, 102), (103, 104)), ((101, 103), (102, 104)), ((101, 104), (102, 103))]
    deficient = [((101, 102), (103, 104)), ((101, 102), (103, 105)), ((101, 102), (104, 105))]

    with clean_engine.begin() as conn:
        seed_season(conn)
        solved = seed_event(conn, "SYNQ1", separable)
        shared = seed_event(conn, "SYNQ2", deficient)
        seed_event(conn, "SYNQ3", [((101, 102), (103, 104))], level="PLAYOFF")

    totals = compute_season_opr(clean_engine, SEASON)

    assert totals == {"events": 3, "solved": 2, "rows": 9, "no_matches": 1, "rank_deficient": 1}
    with clean_engine.connect() as conn:
        assert opr_rows(conn, solved) == pytest.approx({t: STRENGTHS[t] for t in (101, 102, 103, 104)})
        shared_values = opr_rows(conn, shared)
        assert set(shared_values) == {101, 102, 103, 104, 105}
        assert shared_values[101] + shared_values[102] == pytest.approx(STRENGTHS[101] + STRENGTHS[102])
