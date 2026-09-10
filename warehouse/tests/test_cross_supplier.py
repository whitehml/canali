"""The cross-supplier rules: FTC Events outranks FTCScout."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select, text

from warehouse.ingest import writer
from warehouse.schema import core
from warehouse.schema.raw import ingest_conflict

pytestmark = pytest.mark.db

SEASON = 9999
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def seed_event(conn: Connection, code: str = "SYNQ1") -> uuid.UUID:
    event_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO core.season (season, name, game) VALUES (:s, :n, 'SYNTHETIC') ON CONFLICT (season) DO NOTHING"
        ),
        {"s": SEASON, "n": str(SEASON)},
    )
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
    return event_id


def station_rows(teams: Sequence[int], *, role: str | None = None) -> list[dict[str, Any]]:
    """Two red then two blue, the station order FTC Events publishes."""
    return [
        {
            "station": f"{'Red' if i < 2 else 'Blue'}{i % 2 + 1}",
            "alliance": "RED" if i < 2 else "BLUE",
            "team_number": team,
            "surrogate": False,
            "no_show": False,
            "dq": False,
            **({"alliance_role": role} if role else {}),
        }
        for i, team in enumerate(teams)
    ]


def match_row(event_id: uuid.UUID, number: int, *, red: int, blue: int, teams: Sequence[int]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "level": "QUALIFICATION",
        "series": 0,
        "match_number": number,
        "score_red_final": red,
        "score_blue_final": blue,
        "score_red_auto": 0,
        "score_blue_auto": 0,
        "score_red_foul": 0,
        "score_blue_foul": 0,
        "teams": station_rows(teams),
    }


def conflicts(conn: Connection, table_name: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(select(ingest_conflict).where(ingest_conflict.c.table_name == table_name)).mappings()
    ]


def stored_scores(conn: Connection, event_id: uuid.UUID) -> list[tuple[int, int]]:
    return [
        (r.score_red_final, r.score_blue_final)
        for r in conn.execute(
            select(core.match.c.score_red_final, core.match.c.score_blue_final)
            .where(core.match.c.event_id == event_id)
            .order_by(core.match.c.match_number)
        )
    ]


# --------------------------------------------------------------------------------------------------- matches


def test_a_match_disagreement_is_logged_and_the_first_supplier_stands(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        writer.write_matches(conn, [match_row(event_id, 1, red=100, blue=90, teams=(101, 102, 103, 104))])

        written, count = writer.write_matches_non_authoritative(
            conn, [match_row(event_id, 1, red=111, blue=90, teams=(101, 102, 103, 104))]
        )

        assert (written, count) == (0, 1)
        assert stored_scores(conn, event_id) == [(100, 90)]

        logged = conflicts(conn, "core.match")
        assert len(logged) == 1
        assert logged[0]["kind"] == "source_disagreement"
        assert logged[0]["source"] == "ftcscout"
        assert logged[0]["stored"]["score_red_final"] == 100
        assert logged[0]["incoming"]["score_red_final"] == 111


def test_a_match_only_the_second_supplier_has_is_written_rather_than_flagged(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        writer.write_matches(conn, [match_row(event_id, 1, red=100, blue=90, teams=(101, 102, 103, 104))])

        written, count = writer.write_matches_non_authoritative(
            conn, [match_row(event_id, 2, red=80, blue=70, teams=(101, 103, 102, 104))]
        )

        assert (written, count) == (1, 0)
        assert stored_scores(conn, event_id) == [(100, 90), (80, 70)]
        assert conflicts(conn, "core.match") == []


def test_a_match_both_suppliers_agree_on_writes_nothing(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        row = match_row(event_id, 1, red=100, blue=90, teams=(101, 102, 103, 104))
        writer.write_matches(conn, [row])

        assert writer.write_matches_non_authoritative(conn, [row]) == (0, 0)
        assert stored_scores(conn, event_id) == [(100, 90)]
        assert conflicts(conn, "core.match") == []


# ---------------------------------------------------------------------------------------------------- awards


def award_row(event_id: uuid.UUID, code: int, team: int, *, source: str) -> dict[str, Any]:
    return {"event_id": event_id, "award_code": code, "series": 1, "team_number": team, "source": source}


def stored_awards(conn: Connection, event_id: uuid.UUID) -> dict[tuple[int, int], int]:
    return {
        (r.award_code, r.series): r.team_number
        for r in conn.execute(select(core.award).where(core.award.c.event_id == event_id))
    }


def test_a_disagreeing_award_winner_is_logged_and_first_events_row_stands(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        writer.ensure_teams(conn, (101, 102))
        writer.write_awards(conn, event_id, [award_row(event_id, 12, 101, source="ftc_events")])

        written, count = writer.write_awards_non_authoritative(
            conn, event_id, [award_row(event_id, 12, 102, source="ftcscout")]
        )

        assert (written, count) == (0, 1)
        assert stored_awards(conn, event_id) == {(12, 1): 101}

        logged = conflicts(conn, "core.award")
        assert len(logged) == 1
        assert logged[0]["key"] == {"event_id": str(event_id), "award_code": 12, "series": 1}
        assert (logged[0]["stored"], logged[0]["incoming"]) == ({"team_number": 101}, {"team_number": 102})


def test_an_award_slot_only_ftcscout_publishes_is_written_rather_than_flagged(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        covered = seed_event(conn, "SYNQ1")
        uncovered = seed_event(conn, "SYNQ2")
        writer.ensure_teams(conn, (101, 102, 103))
        writer.write_awards(conn, covered, [award_row(covered, 12, 101, source="ftc_events")])

        assert writer.write_awards_non_authoritative(
            conn, covered, [award_row(covered, 13, 102, source="ftcscout")]
        ) == (1, 0)
        assert writer.write_awards_non_authoritative(
            conn, uncovered, [award_row(uncovered, 12, 103, source="ftcscout")]
        ) == (1, 0)

        assert stored_awards(conn, covered) == {(12, 1): 101, (13, 1): 102}
        assert stored_awards(conn, uncovered) == {(12, 1): 103}
        assert conflicts(conn, "core.award") == []


# ---------------------------------------------------------------------------------------------- alliance_role


def role_row(number: int, station: str, team: int, role: str) -> dict[str, Any]:
    return {
        "level": "SEMIFINAL",
        "series": 1,
        "match_number": number,
        "station": station,
        "team_number": team,
        "alliance_role": role,
    }


def stored_roles(conn: Connection, event_id: uuid.UUID) -> dict[str, str | None]:
    return {
        r.station: r.alliance_role
        for r in conn.execute(
            select(core.match_team.c.station, core.match_team.c.alliance_role)
            .select_from(core.match_team.join(core.match, core.match.c.match_id == core.match_team.c.match_id))
            .where(core.match.c.event_id == event_id)
        )
    }


def seed_playoff_match(conn: Connection, event_id: uuid.UUID, teams: Sequence[int], *, role: str | None) -> None:
    row = match_row(event_id, 1, red=100, blue=90, teams=teams)
    row["level"] = "SEMIFINAL"
    row["series"] = 1
    row["teams"] = station_rows(teams, role=role) if role else station_rows(teams)
    writer.write_matches(conn, [row])


def test_a_matching_slot_takes_the_role_ftcscout_publishes(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        seed_playoff_match(conn, event_id, (101, 102, 103, 104), role=None)

        totals = writer.update_alliance_roles(
            conn,
            event_id,
            [role_row(1, "Red1", 101, "Captain"), role_row(1, "Red2", 102, "FirstPick")],
        )

        assert totals == {"updated": 2, "unmatched": 0, "disagreed": 0}
        assert stored_roles(conn, event_id) == {
            "Red1": "Captain",
            "Red2": "FirstPick",
            "Blue1": None,
            "Blue2": None,
        }


def test_a_slot_the_suppliers_disagree_about_is_counted_and_left_alone(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = seed_event(conn)
        seed_playoff_match(conn, event_id, (101, 102, 103, 104), role="Captain")

        totals = writer.update_alliance_roles(
            conn,
            event_id,
            [
                role_row(1, "Red1", 999, "Captain"),
                role_row(1, "Red2", 102, "SecondPick"),
                role_row(2, "Red1", 101, "Captain"),
            ],
        )

        assert totals == {"updated": 0, "unmatched": 1, "disagreed": 2}
        assert set(stored_roles(conn, event_id).values()) == {"Captain"}
