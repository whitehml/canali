"""Constraints on the sim output tables."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import IntegrityError

from warehouse.schema import derived

pytestmark = pytest.mark.db


def _event_sim_run(engine: Engine) -> uuid.UUID:
    event_id, sim_run_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO core.season (season, name, game) VALUES (9999, '9999-00', 'SYNTHETIC')"))
        conn.execute(
            text("INSERT INTO core.event (event_id, season, code, name, date_start) VALUES (:i, 9999, 'X', 'X', :d)"),
            {"i": event_id, "d": dt.date(2026, 1, 1)},
        )
        conn.execute(
            insert(derived.event_sim_run).values(
                sim_run_id=sim_run_id,
                event_id=event_id,
                epa_version="epa-test",
                sim_version="sim-test",
                as_of_match=0,
                n_iterations=1,
                achieved_mcse=0.0,
                computed_at_utc=dt.datetime.now(dt.UTC),
            )
        )
    return sim_run_id


def _result(
    sim_run_id: uuid.UUID, team_number: int, p_advance: float, p_first_championship: float
) -> dict[str, object]:
    return {
        "sim_run_id": sim_run_id,
        "team_number": team_number,
        "p_attend": 1.0,
        "p_captain": 0.0,
        "p_selected": 0.0,
        "p_win": 0.0,
        "p_advance": p_advance,
        "p_advance_first_championship": p_first_championship,
    }


def test_a_first_championship_offer_cannot_exceed_any_offer(clean_engine: Engine) -> None:
    sim_run_id = _event_sim_run(clean_engine)
    with clean_engine.begin() as conn:
        conn.execute(insert(derived.event_sim_result).values(_result(sim_run_id, 1, 0.5, 0.2)))

    with pytest.raises(IntegrityError, match="ck_event_sim_result_probabilities"), clean_engine.begin() as conn:
        conn.execute(insert(derived.event_sim_result).values(_result(sim_run_id, 2, 0.5, 0.6)))
