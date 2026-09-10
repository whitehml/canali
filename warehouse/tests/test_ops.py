"""Operator fit-run inspection and version deletion."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, Engine, func, select

from warehouse.ingest.writer import ensure_teams
from warehouse.ops import DropRefusedError, drop_model_version, list_fit_runs, plan_drop
from warehouse.schema import derived

pytestmark = pytest.mark.db

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def seed_run(
    conn: Connection,
    *,
    model: str = "epa",
    version: str,
    season: int,
    prior_version: str | None = None,
    finished: bool = True,
    teams: Sequence[int] = (101, 102),
) -> uuid.UUID:
    """One batch run with one rating row per team, in whichever table the model writes."""
    fit_run_id = uuid.uuid4()
    conn.execute(
        derived.fit_run.insert(),
        {
            "fit_run_id": fit_run_id,
            "model": model,
            "model_version": version,
            "prior_version": prior_version,
            "scope": f"season:{season}",
            "season": season,
            "started_at_utc": NOW,
            "finished_at_utc": NOW if finished else None,
        },
    )
    ensure_teams(conn, teams)
    if model == "epa":
        conn.execute(
            derived.team_epa.insert(),
            [
                {
                    "fit_run_id": fit_run_id,
                    "season": season,
                    "team_number": team,
                    "model_version": version,
                    "tag": "season_start",
                    "epa_scaled": 10.0,
                }
                for team in teams
            ],
        )
    else:
        conn.execute(
            derived.team_pridge.insert(),
            [
                {
                    "fit_run_id": fit_run_id,
                    "season": season,
                    "team_number": team,
                    "as_of_match": 0,
                    "model_version": version,
                    "pridge": 10.0,
                }
                for team in teams
            ],
        )
    return fit_run_id


def rating_rows(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        return (
            int(conn.execute(select(func.count()).select_from(derived.team_epa)).scalar() or 0),
            int(conn.execute(select(func.count()).select_from(derived.team_pridge)).scalar() or 0),
        )


def listed(
    engine: Engine,
    *,
    model: str | None = None,
    season: int | None = None,
    versions: Sequence[str] | None = None,
) -> list[tuple[str, str, int | None]]:
    runs = list_fit_runs(engine, model=model, season=season, versions=versions)
    return [(r.model, r.model_version, r.season) for r in runs]


@pytest.fixture
def seeded(clean_engine: Engine) -> Engine:
    with clean_engine.begin() as conn:
        seed_run(conn, version="epa-0.8.0", season=2024)
        seed_run(conn, version="epa-0.8.0", season=2025)
        seed_run(conn, version="epa-0.9.0", season=2025)
        seed_run(conn, model="pridge", version="pridge-0.3.0", season=2025, prior_version="epa-0.9.0")
    return clean_engine


# ---------------------------------------------------------------------------------------------------- inspection


def test_a_listing_reports_every_run_and_what_it_wrote(seeded: Engine) -> None:
    runs = list_fit_runs(seeded)

    assert len(runs) == 4
    assert {r.rating_rows for r in runs} == {2}
    assert all(r.finished for r in runs)

    pridge = next(r for r in runs if r.model == "pridge")
    assert pridge.prior_version == "epa-0.9.0"
    assert pridge.scope == "season:2025"
    assert pridge.event_code is None


def test_a_listing_narrows_by_model_season_and_version(seeded: Engine) -> None:
    assert listed(seeded, model="pridge") == [("pridge", "pridge-0.3.0", 2025)]
    assert listed(seeded, season=2024) == [("epa", "epa-0.8.0", 2024)]
    assert listed(seeded, versions=["epa-0.8.0"]) == [
        ("epa", "epa-0.8.0", 2024),
        ("epa", "epa-0.8.0", 2025),
    ]
    assert listed(seeded, model="epa", season=2025, versions=["epa-0.9.0"]) == [("epa", "epa-0.9.0", 2025)]


def test_a_plan_counts_what_a_drop_would_take_and_deletes_nothing(seeded: Engine) -> None:
    plan = plan_drop(seeded, "epa", ["epa-0.8.0"])

    assert [(c.season, c.runs, c.rating_rows) for c in plan.per_season] == [(2024, 1, 2), (2025, 1, 2)]
    assert plan.rating_rows == 4
    assert plan.unfinished == ()

    assert len(list_fit_runs(seeded)) == 4
    assert rating_rows(seeded) == (6, 2)


# ---------------------------------------------------------------------------------------------------- retirement


def test_a_drop_takes_its_runs_and_their_ratings_and_nothing_else(seeded: Engine) -> None:
    dropped = drop_model_version(seeded, "epa", ["epa-0.8.0"])

    assert dropped.rating_rows == 4
    assert listed(seeded) == [
        ("epa", "epa-0.9.0", 2025),
        ("pridge", "pridge-0.3.0", 2025),
    ]
    assert rating_rows(seeded) == (2, 2)


def test_a_season_narrows_a_drop_to_that_season(seeded: Engine) -> None:
    drop_model_version(seeded, "epa", ["epa-0.8.0"], season=2025)

    assert listed(seeded, versions=["epa-0.8.0"]) == [("epa", "epa-0.8.0", 2024)]
    assert rating_rows(seeded) == (4, 2)


@pytest.mark.parametrize(
    ("named", "message"),
    [
        (["epa-0.8.*"], "no wildcard"),
        (["epa-0."], "no epa run"),
        (["epa-1.0.0"], "no epa run"),
        (["epa-0.7.0"], "unfinished"),
    ],
)
def test_a_drop_it_cannot_stand_behind_is_refused(seeded: Engine, named: list[str], message: str) -> None:
    with seeded.begin() as conn:
        seed_run(conn, version="epa-0.7.0", season=2025, finished=False)

    with pytest.raises(DropRefusedError, match=message):
        drop_model_version(seeded, "epa", named)

    assert len(list_fit_runs(seeded)) == 5
