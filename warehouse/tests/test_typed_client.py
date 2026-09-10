"""The typed accessors: what the models read, and what they write back."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select, text

from warehouse import views
from warehouse.client import Warehouse
from warehouse.ingest.writer import ensure_teams
from warehouse.rules import loader
from warehouse.rules.model import RulePack
from warehouse.schema import core, derived

pytestmark = pytest.mark.db

SEASON = 9999
PACK = Path(__file__).parent / "fixtures" / "rule_packs" / "9999_synthetic.toml"


def seed_event(conn: Connection, code: str, *, day: int) -> uuid.UUID:
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
            "date_start": date(2026, 3, day),
        },
    )
    return event_id


def seed_match(
    conn: Connection,
    event_id: uuid.UUID,
    *,
    number: int,
    level: str = "QUALIFICATION",
    red: Sequence[int] = (101, 102),
    blue: Sequence[int] = (103, 104),
    no_shows: Sequence[int] = (),
    breakdown: dict[str, Any] | None = None,
) -> uuid.UUID:
    match_id = uuid.uuid4()
    ensure_teams(conn, [*red, *blue])
    conn.execute(
        core.match.insert(),
        {
            "match_id": match_id,
            "event_id": event_id,
            "level": level,
            "series": 0,
            "match_number": number,
            "score_red_final": 100,
            "score_blue_final": 90,
            "score_red_auto": 20,
            "score_blue_auto": 10,
            "score_red_foul": 5,
            "score_blue_foul": 0,
            "ingested_at_utc": datetime.now(UTC),
        },
    )
    conn.execute(
        core.match_team.insert(),
        [
            {
                "match_id": match_id,
                "station": f"{'Red' if side == 'RED' else 'Blue'}{i}",
                "alliance": side,
                "team_number": team,
                "surrogate": i == 2 and side == "RED",
                "no_show": team in no_shows,
            }
            for side, teams in (("RED", red), ("BLUE", blue))
            for i, team in enumerate(teams, start=1)
        ],
    )
    if breakdown is not None:
        conn.execute(
            core.match_breakdown.insert(),
            [{"match_id": match_id, "alliance": side, "breakdown": breakdown} for side in ("RED", "BLUE")],
        )
    return match_id


@pytest.fixture
def wh(clean_engine: Engine) -> Iterator[Warehouse]:
    with clean_engine.begin() as conn:
        loader.load(conn, [RulePack.from_file(PACK)])
        views.rebuild(conn)
    client = Warehouse(engine=clean_engine)
    yield client


def seed_fit_run(
    conn: Connection,
    *,
    model: str = "epa",
    version: str,
    season: int = SEASON,
    prior_version: str | None = None,
    event_id: uuid.UUID | None = None,
    finished: bool = True,
) -> uuid.UUID:
    fit_run_id = uuid.uuid4()
    now = datetime.now(UTC)
    conn.execute(
        derived.fit_run.insert(),
        {
            "fit_run_id": fit_run_id,
            "model": model,
            "model_version": version,
            "prior_version": prior_version,
            "scope": f"event:{event_id}" if event_id else f"season:{season}",
            "event_id": event_id,
            "season": season,
            "started_at_utc": now,
            "finished_at_utc": now if finished else None,
        },
    )
    return fit_run_id


def epa_row(team: int, tag: str, event_id: uuid.UUID | None, as_of: int | None, scaled: float) -> dict[str, Any]:
    return {
        "season": SEASON,
        "team_number": team,
        "event_id": event_id,
        "tag": tag,
        "as_of_match": as_of,
        "epa_scaled": scaled,
        "scale_provisional": False,
        "returning_from_gap": False,
    }


# ----------------------------------------------------------------------------------------------------------- reads


def test_events_come_back_in_sequence_order(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        seed_event(conn, "SYNQ2", day=8)
        seed_event(conn, "SYNQ1", day=1)

    events = wh.events(SEASON)

    assert [e.event_code for e in events] == ["SYNQ1", "SYNQ2"]
    assert [e.event_ordinal for e in events] == [1, 2]
    assert {e.event_type for e in events} == {"Qualifier"}


def test_a_match_row_is_typed_and_names_the_teams_it_rates(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        event_id = seed_event(conn, "SYNQ1", day=1)
        seed_match(conn, event_id, number=1, no_shows=(104,))

    red, blue = wh.event_matches(event_id)
    assert (red.alliance, blue.alliance) == ("RED", "BLUE")
    assert (red.score, red.opponent_score, red.score_auto) == (100.0, 90.0, 20.0)
    assert red.score_no_foul == 100.0
    assert red.season == SEASON and red.event_code == "SYNQ1"
    assert red.rated_teams() == (101, 102)
    assert blue.rated_teams() == (103,)


def test_the_level_allowlist_and_the_season_sweep(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        first = seed_event(conn, "SYNQ1", day=1)
        second = seed_event(conn, "SYNQ2", day=8)
        seed_match(conn, first, number=1)
        seed_match(conn, first, number=2, level="PLAYOFF")
        seed_match(conn, second, number=1)

    assert {m.level for m in wh.event_matches(first)} == {"QUALIFICATION"}
    assert {m.level for m in wh.event_matches(first, levels=("QUALIFICATION", "PLAYOFF"))} == {
        "QUALIFICATION",
        "PLAYOFF",
    }
    assert [m.event_code for m in wh.season_matches(SEASON)] == ["SYNQ1", "SYNQ1", "SYNQ2", "SYNQ2"]


def test_breakdowns_refuse_a_column_the_pack_does_not_declare(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        event_id = seed_event(conn, "SYNQ1", day=1)
        match_id = seed_match(conn, event_id, number=1, breakdown={"autoWidgetPoints": 12})

    assert wh.breakdowns(SEASON, event_id, ["auto_widget_points"]) == {
        (match_id, "RED"): {"auto_widget_points": 12.0},
        (match_id, "BLUE"): {"auto_widget_points": 12.0},
    }
    with pytest.raises(ValueError, match="no such component"):
        wh.breakdowns(SEASON, event_id, ["auto_widget_points", "not_a_component"])


def test_prior_event_counts_look_back_within_the_season_only(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        first = seed_event(conn, "SYNQ1", day=1)
        second = seed_event(conn, "SYNQ2", day=8)
        ensure_teams(conn, (101, 102))
        run = seed_fit_run(conn, version="epa-0.9.0")
        conn.execute(
            derived.team_epa.insert(),
            [
                {"fit_run_id": run, "model_version": "epa-0.9.0", **epa_row(101, "post_event", first, 12, 10.0)},
                {"fit_run_id": run, "model_version": "epa-0.9.0", **epa_row(101, "pre_event", second, 0, 10.0)},
                {"fit_run_id": run, "model_version": "epa-0.9.0", **epa_row(102, "pre_event", second, 0, 10.0)},
            ],
        )

    counts = wh.prior_event_counts(second, "epa-0.9.0")

    assert counts == {101: 1}
    assert wh.prior_event_counts(first, "epa-0.9.0") == {}


# ---------------------------------------------------------------------------------------------------------- writes


def test_a_live_run_is_reused_and_a_different_prior_is_a_different_run(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        event_id = seed_event(conn, "SYNQ1", day=1)

    opened = wh.start_fit_run(
        model="pridge", model_version="pridge-0.3.0", scope="event", event_id=event_id, prior_version="epa-0.9.0"
    )
    reused = wh.start_fit_run(
        model="pridge", model_version="pridge-0.3.0", scope="event", event_id=event_id, prior_version="epa-0.9.0"
    )
    other_prior = wh.start_fit_run(
        model="pridge", model_version="pridge-0.3.0", scope="event", event_id=event_id, prior_version="epa-0.8.0"
    )

    assert opened == reused
    assert other_prior != opened


def test_finishing_a_run_replaces_the_one_it_supersedes(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        ensure_teams(conn, (101,))
        old = seed_fit_run(conn, version="epa-0.9.0")
        conn.execute(
            derived.team_epa.insert(),
            [{"fit_run_id": old, "model_version": "epa-0.9.0", **epa_row(101, "season_start", None, None, 10.0)}],
        )

    fresh = wh.start_fit_run(model="epa", model_version="epa-0.9.0", scope="season", season=SEASON)
    wh.write_team_epa(fresh, "epa-0.9.0", [epa_row(101, "season_start", None, None, 20.0)])

    assert wh.finish_fit_run(fresh) == 1
    with wh.engine.connect() as conn:
        assert conn.execute(select(derived.fit_run.c.fit_run_id)).scalars().all() == [fresh]
        assert conn.execute(select(derived.team_epa.c.epa_scaled)).scalars().all() == [20.0]


def test_a_run_at_another_version_or_prior_replaces_nothing(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        seed_fit_run(conn, version="epa-0.8.0")
        seed_fit_run(conn, model="pridge", version="pridge-0.3.0", prior_version="epa-0.8.0")

    fresh = wh.start_fit_run(model="epa", model_version="epa-0.9.0", scope="season", season=SEASON)
    other_prior = wh.start_fit_run(
        model="pridge", model_version="pridge-0.3.0", scope="season", season=SEASON, prior_version="epa-0.9.0"
    )

    assert wh.finish_fit_run(fresh) == 0
    assert wh.finish_fit_run(other_prior) == 0
    with wh.engine.connect() as conn:
        assert len(conn.execute(select(derived.fit_run.c.fit_run_id)).scalars().all()) == 4


def test_every_tag_is_written_and_a_rewrite_updates_in_place(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        event_id = seed_event(conn, "SYNQ1", day=1)
        ensure_teams(conn, (101,))
    run = wh.start_fit_run(model="epa", model_version="epa-0.9.0", scope="season", season=SEASON)

    written = wh.write_team_epa(
        run,
        "epa-0.9.0",
        [
            epa_row(101, "season_start", None, None, 10.0),
            epa_row(101, "pre_event", event_id, 0, 11.0),
            epa_row(101, "match", event_id, 1, 12.0),
            epa_row(101, "post_event", event_id, 1, 13.0),
        ],
    )
    assert written == 4
    assert wh.write_team_epa(run, "epa-0.9.0", [epa_row(101, "match", event_id, 1, 99.0)]) == 1

    assert wh.match_grain_epa(event_id, "epa-0.9.0") == {1: {101: 99.0}}
    with wh.engine.connect() as conn:
        assert conn.execute(select(derived.team_epa.c.tag)).scalars().all().count("post_event") == 1
        assert len(conn.execute(select(derived.team_epa.c.tag)).scalars().all()) == 4


def test_a_pridge_rating_is_keyed_by_the_event_it_was_fitted_at(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        first = seed_event(conn, "SYNQ1", day=1)
        second = seed_event(conn, "SYNQ2", day=8)
        ensure_teams(conn, (101,))
    run = wh.start_fit_run(
        model="pridge", model_version="pridge-0.3.0", scope="season", season=SEASON, prior_version="epa-0.9.0"
    )

    def row(event_id: uuid.UUID, pridge: float) -> dict[str, Any]:
        return {
            "season": SEASON,
            "team_number": 101,
            "event_id": event_id,
            "as_of_match": 1,
            "component": "total",
            "pridge": pridge,
            "lambda_": 1.32,
        }

    wh.write_team_pridge(run, "pridge-0.3.0", [row(first, 5.0), row(second, 7.0)])

    assert [r.pridge for r in wh.team_pridge_rows(first, "pridge-0.3.0")] == [5.0]
    assert [r.pridge for r in wh.team_pridge_rows(second, "pridge-0.3.0")] == [7.0]


def test_model_versions_lists_completed_batch_runs_newest_first(wh: Warehouse) -> None:
    with wh.engine.begin() as conn:
        event_id = seed_event(conn, "SYNQ1", day=1)
        seed_fit_run(conn, version="epa-0.8.0", season=SEASON - 1)
        seed_fit_run(conn, version="epa-0.7.0", season=SEASON, finished=False)
        seed_fit_run(conn, version="epa-0.6.0", event_id=event_id)
        seed_fit_run(conn, model="pridge", version="pridge-0.3.0", prior_version="epa-0.8.0")
    newest = wh.start_fit_run(model="epa", model_version="epa-0.9.0", scope="season", season=SEASON)
    wh.finish_fit_run(newest)

    assert wh.model_versions("epa") == ["epa-0.9.0", "epa-0.8.0"]
    assert wh.model_versions("pridge") == ["pridge-0.3.0"]
