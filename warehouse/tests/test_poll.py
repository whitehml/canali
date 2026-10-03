"""The live poller: its schedule, its supervisor and the weekly sweep."""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, text

from warehouse.ingest.pipeline import Endpoint
from warehouse.poll import schedule
from warehouse.poll.cadence import Cadence
from warehouse.poll.loop import supervise
from warehouse.poll.schedule import EventState, EventWindow, PlayoffResult, final_decided
from warehouse.poll.sweep import sweep_due, sweep_targets
from warehouse.poll.watch import WatchedEvent

NY = ZoneInfo("America/New_York")
CADENCE = Cadence(hybrid_s=60, after_close_s=[3600, 86400], restart_s=30, sweep_at=dt.time(6))


def _window(event_type: str = "Qualifier") -> EventWindow:
    event = WatchedEvent(uuid.uuid4(), "X", event_type, dt.date(2026, 1, 10), None, "America/New_York")
    return EventWindow.of(event, "America/New_York")


def _at(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp).replace(tzinfo=NY)


# --------------------------------------------------------------------------------------------------------- final


def _won(series: int, red_wins: bool, match_number: int = 1) -> PlayoffResult:
    return PlayoffResult(series, match_number, 100 if red_wins else 50, 50 if red_wins else 100)


@pytest.mark.parametrize(
    ("alliances", "playoff", "decided"),
    [
        (4, [_won(5, True), _won(6, True)], True),
        (4, [_won(6, False)], False),
        (4, [_won(6, False), _won(7, True)], True),
        (4, [PlayoffResult(6, 1, 80, 80)], False),
        (4, [_won(5, True), PlayoffResult(6, 1, None, None)], False),
        (2, [_won(1, True, 1), _won(1, False, 2)], False),
        (2, [_won(1, True, 1), _won(1, False, 2), _won(1, True, 3)], True),
    ],
)
def test_final_decided_follows_the_decode_bracket(alliances: int, playoff: list[PlayoffResult], decided: bool) -> None:
    assert final_decided(alliances, playoff) is decided


# -------------------------------------------------------------------------------------------------------- close


def test_after_close_fetches_three_times_then_finishes() -> None:
    window = _window()
    now = _at("2026-01-10T15:00")
    state = EventState(teams_fetched=True, closed_at=now)
    fetches = []

    while not schedule.finished(now, window, state, CADENCE):
        due = schedule.due(now, window, state, CADENCE)
        if due:
            fetches.append(now)
            schedule.record(state, due, now, window)
        now += dt.timedelta(minutes=1)

    assert fetches == [_at("2026-01-10T16:00"), _at("2026-01-11T00:00"), _at("2026-01-11T15:00")]
    assert schedule.closed_at(_at("2026-01-11T00:00"), window, EventState()) == _at("2026-01-11T00:00")


@pytest.mark.parametrize(
    ("event_type", "awards", "advancement"),
    [("League Meet", False, False), ("FIRST Championship", True, False), ("Qualifier", True, True)],
)
def test_awards_and_advancement_follow_the_event_type(event_type: str, awards: bool, advancement: bool) -> None:
    after = _window(event_type).after_close()

    assert (Endpoint.AWARDS in after, Endpoint.ADVANCEMENT in after) == (awards, advancement)


# ---------------------------------------------------------------------------------------------------- restart


def test_supervise_starts_a_fresh_poller_after_a_crash() -> None:
    made: list[int] = []
    sleeps: list[float] = []
    stopped: list[bool] = []

    class Poller:
        def __init__(self) -> None:
            made.append(len(made))
            self.n = made[-1]

        def run(self, should_stop: object, sleep: object) -> None:
            if self.n < 2:
                raise RuntimeError("the database went away")
            stopped.append(True)

    restarts = supervise(Poller, lambda: bool(stopped), restart_s=30, sleep=sleeps.append)  # type: ignore[arg-type]

    assert (restarts, made, sleeps) == (2, [0, 1, 2], [30, 30])


# ------------------------------------------------------------------------------------------------------- sweep


def test_the_sweep_is_due_once_each_monday() -> None:
    at = CADENCE.sweep_at

    assert not sweep_due(_at("2026-01-12T05:59"), None, at, NY)
    assert sweep_due(_at("2026-01-12T06:00"), None, at, NY)
    assert not sweep_due(_at("2026-01-12T09:00"), dt.date(2026, 1, 12), at, NY)
    assert not sweep_due(_at("2026-01-13T06:00"), None, at, NY)


@pytest.mark.db
def test_the_sweep_takes_unwatched_rated_events_that_finished_last_week(clean_engine: Engine) -> None:
    events = {
        "SWEPT": ("Qualifier", "2026-01-10"),
        "WATCHED": ("Qualifier", "2026-01-10"),
        "SCRIMMAGE": ("Scrimmage", "2026-01-10"),
        "EIGHT_DAYS": ("Qualifier", "2026-01-04"),
        "TODAY": ("Qualifier", "2026-01-12"),
    }
    ids = {code: uuid.uuid4() for code in events}
    with clean_engine.begin() as conn:
        conn.execute(text("INSERT INTO core.season (season, name, game) VALUES (2025, '2025-26', 'DECODE')"))
        for code, (event_type, day) in events.items():
            conn.execute(
                text(
                    "INSERT INTO core.event (event_id, season, code, name, type, date_start, date_end) "
                    "VALUES (:i, 2025, :c, :c, :t, :d, :d)"
                ),
                {"i": ids[code], "c": code, "t": event_type, "d": day},
            )
        targets = sweep_targets(conn, 2025, [ids["WATCHED"]], dt.date(2026, 1, 12))

    assert targets == ["SWEPT"]
