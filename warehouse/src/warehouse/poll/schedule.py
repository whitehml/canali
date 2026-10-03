"""The poll scheduler: which endpoints of a watched event are due at a given moment.

An event opens at the start of its first day, venue-local, and closes when its playoff final is decided, or
at the end of its last day if no final is seen. After close, the hybrid schedule, awards and advancement are fetched a
fixed number of times at set intervals.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo

from warehouse.ingest.pipeline import Endpoint
from warehouse.poll.cadence import Cadence
from warehouse.poll.watch import WatchedEvent

Level = Literal["qual", "playoff"]

AWARDLESS_TYPES: tuple[str, ...] = ("League Meet",)
ADVANCING_TYPES: tuple[str, ...] = ("Qualifier", "League Tournament", "Super Qualifier", "Championship")


_ON_CHANGE: dict[Level, tuple[Endpoint, ...]] = {
    "qual": (Endpoint.SCORES_QUAL, Endpoint.RANKINGS),
    "playoff": (Endpoint.SCORES_PLAYOFF, Endpoint.ALLIANCES),
}


@dataclass(frozen=True, slots=True)
class EventWindow:
    """A watched event's opening and latest closing instant, from its dates in its venue's time zone."""

    event_id: uuid.UUID
    code: str
    type: str | None
    timezone: ZoneInfo
    opens_at: dt.datetime
    closes_by: dt.datetime

    @classmethod
    def of(cls, event: WatchedEvent, default_timezone: str) -> EventWindow:
        tz = ZoneInfo(event.timezone or default_timezone)
        last_day = event.date_end or event.date_start
        return cls(
            event_id=event.event_id,
            code=event.code,
            type=event.type,
            timezone=tz,
            opens_at=dt.datetime.combine(event.date_start, dt.time(), tz),
            closes_by=dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time(), tz),
        )

    def after_close(self) -> tuple[Endpoint, ...]:
        awards = () if self.type in AWARDLESS_TYPES else (Endpoint.AWARDS,)
        advancement = (Endpoint.ADVANCEMENT,) if self.type in ADVANCING_TYPES else ()
        return (Endpoint.HYBRID_QUAL, Endpoint.HYBRID_PLAYOFF, *awards, *advancement)


@dataclass(slots=True)
class EventState:
    """What the poller has done for one event."""

    teams_fetched: bool = False
    last_hybrid_at: dt.datetime | None = None
    changed: set[Level] = field(default_factory=set)
    closed_at: dt.datetime | None = None
    after_close_attempts: int = 0


def closed_at(now: dt.datetime, window: EventWindow, state: EventState) -> dt.datetime | None:
    if state.closed_at is not None:
        return state.closed_at
    return window.closes_by if now >= window.closes_by else None


def after_close_times(closed: dt.datetime, window: EventWindow, cadence: Cadence) -> list[dt.datetime]:
    """Each offset after close."""
    offsets = [closed + dt.timedelta(seconds=s) for s in cadence.after_close_s]
    local_day = closed.astimezone(window.timezone).date()
    end_of_day = dt.datetime.combine(local_day + dt.timedelta(days=1), dt.time(), window.timezone)
    extra = [end_of_day] if end_of_day > min(offsets) else []
    return sorted(set(offsets + extra))


def due(now: dt.datetime, window: EventWindow, state: EventState, cadence: Cadence) -> list[Endpoint]:
    if now < window.opens_at:
        return []
    out: list[Endpoint] = []
    for level in ("qual", "playoff"):
        if level in state.changed:
            out.extend(_ON_CHANGE[level])

    closed = closed_at(now, window, state)
    if closed is None:
        if not state.teams_fetched:
            out.insert(0, Endpoint.EVENT_TEAMS)
        if state.last_hybrid_at is None or (now - state.last_hybrid_at).total_seconds() >= cadence.hybrid_s:
            out[:0] = [Endpoint.HYBRID_QUAL, Endpoint.HYBRID_PLAYOFF]
        return out

    times = after_close_times(closed, window, cadence)
    if state.after_close_attempts < len(times) and now >= times[state.after_close_attempts]:
        out[:0] = window.after_close()
    return out


def record(state: EventState, fetched: Iterable[Endpoint], now: dt.datetime, window: EventWindow) -> None:
    """Mark endpoints as fetched."""
    fetched = set(fetched)
    if closed_at(now, window, state) is not None and fetched.issuperset(window.after_close()):
        state.after_close_attempts += 1
    if Endpoint.EVENT_TEAMS in fetched:
        state.teams_fetched = True
    if Endpoint.HYBRID_QUAL in fetched or Endpoint.HYBRID_PLAYOFF in fetched:
        state.last_hybrid_at = now
    for level, endpoints in _ON_CHANGE.items():
        if fetched.issuperset(endpoints):
            state.changed.discard(level)


def finished(now: dt.datetime, window: EventWindow, state: EventState, cadence: Cadence) -> bool:
    """Closed, with nothing left to fetch."""
    closed = closed_at(now, window, state)
    if closed is None or state.changed:
        return False
    return state.after_close_attempts >= len(after_close_times(closed, window, cadence))


class PlayoffResult(NamedTuple):
    series: int
    match_number: int
    red: int | None
    blue: int | None


def final_decided(alliances: int, playoff: Iterable[PlayoffResult]) -> bool:
    """Whether a playoff is over."""
    red_won: dict[int, list[bool]] = {}
    for r in sorted(playoff, key=lambda r: (r.series, r.match_number)):
        if r.red is not None and r.blue is not None and r.red != r.blue:
            red_won.setdefault(r.series, []).append(r.red > r.blue)

    if alliances == 2:
        wins = red_won.get(1, [])
        return wins.count(True) >= 2 or wins.count(False) >= 2

    final = 2 * alliances - 2
    first = red_won.get(final)
    if not first:
        return False
    return first[0] or bool(red_won.get(final + 1))
