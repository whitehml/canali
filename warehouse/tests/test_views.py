"""The published contract in pub."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import Connection, Engine, text

from warehouse import views
from warehouse.views import MANAGED_SCHEMAS

pytestmark = pytest.mark.db

NOW = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def _seed_season(conn: Connection, season: int = 2025) -> None:
    conn.execute(
        text("INSERT INTO core.season (season, name, game) VALUES (:s, :n, 'DECODE') ON CONFLICT DO NOTHING"),
        {"s": season, "n": f"{season}-{(season + 1) % 100:02d}"},
    )


def _seed_event(
    conn: Connection,
    code: str,
    event_type: str = "Qualifier",
    season: int = 2025,
    date_start: str = "2025-11-01",
) -> uuid.UUID:
    _seed_season(conn, season)
    event_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO core.event (event_id, season, code, name, type, date_start) VALUES (:i, :s, :c, :c, :t, :d)"),
        {"i": event_id, "s": season, "c": code, "t": event_type, "d": date_start},
    )
    return event_id


def _seed_match(
    conn: Connection,
    event_id: uuid.UUID,
    *,
    level: str = "QUALIFICATION",
    series: int = 0,
    match_number: int = 1,
    red: int | None = 100,
    blue: int | None = 90,
    teams: tuple[int, int, int, int] = (11, 12, 21, 22),
    dq_red: bool = False,
) -> uuid.UUID:
    match_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO core.match (match_id, event_id, level, series, match_number, "
            "score_red_final, score_blue_final, score_red_auto, score_blue_auto, "
            "score_red_foul, score_blue_foul, ingested_at_utc) "
            "VALUES (:m, :e, :l, :se, :n, :r, :b, 10, 10, 5, 8, :t)"
        ),
        {"m": match_id, "e": event_id, "l": level, "se": series, "n": match_number, "r": red, "b": blue, "t": NOW},
    )
    for team, station, alliance in zip(
        teams, ("Red1", "Red2", "Blue1", "Blue2"), ("RED", "RED", "BLUE", "BLUE"), strict=True
    ):
        conn.execute(text("INSERT INTO core.team (team_number) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": team})
        conn.execute(
            text(
                "INSERT INTO core.match_team (match_id, station, alliance, team_number, dq) VALUES (:m, :s, :a, :t, :d)"
            ),
            {"m": match_id, "s": station, "a": alliance, "t": team, "d": dq_red and alliance == "RED"},
        )
    return match_id


def _seed_breakdown(conn: Connection, match_id: uuid.UUID, red_total: float, blue_total: float) -> None:
    for alliance, total in (("RED", red_total), ("BLUE", blue_total)):
        conn.execute(
            text("INSERT INTO core.match_breakdown (match_id, alliance, breakdown) VALUES (:m, :a, :b)"),
            {"m": match_id, "a": alliance, "b": json.dumps({"totalPoints": total})},
        )


# ------------------------------------------------------------------- the rebuild


def test_the_rebuild_is_idempotent_and_grants_come_last(engine: Engine) -> None:
    with engine.begin() as conn:
        first = views.rebuild(conn)
    with engine.begin() as conn:
        second = views.rebuild(conn)
    assert first == second
    # Grants run over ALL TABLES IN SCHEMA pub, so a view created after them carries none.
    assert first[-1] == "grants.sql"


def test_dropping_views_leaves_the_managed_schemas_empty(engine: Engine) -> None:
    schemas = ", ".join(f"'{s}'" for s in MANAGED_SCHEMAS)
    with engine.begin() as conn:
        views.drop_all_views(conn)
        remaining = conn.execute(text(f"SELECT count(*) FROM pg_views WHERE schemaname IN ({schemas})")).scalar()
        views.rebuild(conn)
    assert remaining == 0


# -------------------------------------------------------------- the rating corpus


@pytest.mark.parametrize(
    ("event_type", "admitted"),
    [
        ("Qualifier", True),
        ("League Meet", True),
        ("League Tournament", True),
        ("Championship", True),
        ("Off-Season", False),
        ("Premier", False),
        ("Scrimmage", False),
        ("Regional Scrimmage", False),
    ],
)
def test_rating_input_admits_a_rated_event_type(clean_engine: Engine, event_type: str, admitted: bool) -> None:
    with clean_engine.begin() as conn:
        event_id = _seed_event(conn, "USPAQ1", event_type)
        _seed_match(conn, event_id)
    with clean_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM pub.v_match_rating_input")).scalar()
    assert bool(rows) is admitted


def test_a_practice_match_is_not_a_rating_row(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = _seed_event(conn, "USPAQ1")
        _seed_match(conn, event_id, level="PRACTICE", match_number=1)
        _seed_match(conn, event_id, level="QUALIFICATION", match_number=2)
    with clean_engine.connect() as conn:
        levels = conn.execute(text("SELECT DISTINCT level::text FROM pub.v_match_rating_input")).scalars().all()
    assert levels == ["QUALIFICATION"]


def test_an_unscored_match_is_not_a_rating_row(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        event_id = _seed_event(conn, "USPAQ1")
        _seed_match(conn, event_id, match_number=1, red=None, blue=None)
        _seed_match(conn, event_id, match_number=2)
    with clean_engine.connect() as conn:
        numbers = conn.execute(text("SELECT DISTINCT match_number FROM pub.v_match_rating_input")).scalars().all()
    assert numbers == [2]


def test_the_event_ordinal_restarts_per_event_and_puts_the_playoffs_last(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        first = _seed_event(conn, "USPAQ1")
        second = _seed_event(conn, "USPAQ2")
        _seed_match(conn, first, level="PLAYOFF", match_number=1)
        _seed_match(conn, first, level="QUALIFICATION", match_number=40)
        _seed_match(conn, first, level="QUALIFICATION", match_number=39)
        _seed_match(conn, second, level="QUALIFICATION", match_number=7)
    with clean_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_code, level::text, match_number, event_match_ordinal "
                "FROM pub.v_match_rating_input WHERE alliance = 'RED' "
                "ORDER BY event_code, event_match_ordinal"
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("USPAQ1", "QUALIFICATION", 39, 1),
        ("USPAQ1", "QUALIFICATION", 40, 2),
        ("USPAQ1", "PLAYOFF", 1, 3),
        ("USPAQ2", "QUALIFICATION", 7, 1),
    ]


def test_a_disqualified_playoff_alliance_keeps_the_score_it_played(clean_engine: Engine) -> None:
    """Decision 26. FIRST zeroes totalPoints for a DQ and leaves score_red_final alone."""
    with clean_engine.begin() as conn:
        event_id = _seed_event(conn, "USPALAQ1")
        match_id = _seed_match(conn, event_id, level="PLAYOFF", red=188, blue=140, dq_red=True)
        _seed_breakdown(conn, match_id, red_total=0, blue_total=140)
    with clean_engine.connect() as conn:
        red = conn.execute(
            text(
                "SELECT score, opponent_score, official_score, official_opponent_score, official_result "
                "FROM pub.v_match_rating_input WHERE alliance = 'RED'"
            )
        ).one()
    assert red.score == 188
    assert red.opponent_score == 140
    assert red.official_score == 0
    assert red.official_opponent_score == 140
    assert red.official_result == "LOSS"


# ------------------------------------------------------------------- the rest of pub


def test_same_day_events_order_by_code(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        _seed_event(conn, "USMOKSSTLNM2", date_start="2025-11-08")
        _seed_event(conn, "USMOKSSTLNM1", date_start="2025-11-08")
    with clean_engine.connect() as conn:
        ordered = (
            conn.execute(text("SELECT event_code FROM pub.v_event_sequence ORDER BY event_ordinal")).scalars().all()
        )
    assert ordered == ["USMOKSSTLNM1", "USMOKSSTLNM2"]


def test_fittable_is_a_numeric_or_boolean_component_that_is_not_derived(clean_engine: Engine) -> None:
    """Decision 13. The column was dropped; the view computes it."""
    rows = [
        ("autoPoints", "numeric", False, True),
        ("movementRp", "boolean", False, True),
        ("totalPoints", "numeric", True, False),
        ("robot1Perch", "enum", False, False),
        ("classifierState", "array", False, False),
    ]
    with clean_engine.begin() as conn:
        _seed_season(conn)
        conn.execute(
            text(
                "INSERT INTO core.rule_pack (season, game, version, source_file, loaded_at_utc) "
                "VALUES (2025, 'DECODE', 'test', 'test.toml', :t)"
            ),
            {"t": NOW},
        )
        for name, kind, is_derived, _ in rows:
            conn.execute(
                text(
                    "INSERT INTO core.rule_pack_component (season, name, level, kind, is_derived, column_name) "
                    "VALUES (2025, :n, 'alliance', :k, :d, :c)"
                ),
                {"n": name, "k": kind, "d": is_derived, "c": name.lower()},
            )
    with clean_engine.connect() as conn:
        served = {
            r.name: r.fittable
            for r in conn.execute(
                text("SELECT name, fittable FROM pub.v_rule_pack_component WHERE season = 2025")
            ).all()
        }
    assert served == {name: expected for name, _, _, expected in rows}
