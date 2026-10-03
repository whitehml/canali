"""The poll loop.

The event list is fetched once at start. Each tick asks the scheduler what is due, fetches it through the ingest
pipeline, and feeds back what changed: a hybrid schedule whose payload changed marks its level, and a changed playoff
is checked for a decided final.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Callable

import structlog
from sqlalchemy import Connection, func, select

from warehouse.ingest.pipeline import Endpoint, Ingestor
from warehouse.poll import schedule
from warehouse.poll.cadence import Cadence
from warehouse.poll.schedule import EventState, EventWindow, Level, PlayoffResult
from warehouse.poll.watch import WatchList, latest_season
from warehouse.schema import core
from warehouse.schema.types import ELIMINATION_MATCHES

log = structlog.get_logger(__name__)

_HYBRID: dict[Endpoint, Level] = {Endpoint.HYBRID_QUAL: "qual", Endpoint.HYBRID_PLAYOFF: "playoff"}


class Poller:
    """Polls the watched events of the latest season until each is finished."""

    def __init__(self, ingestor: Ingestor, watch: WatchList, cadence: Cadence) -> None:
        self.ingestor = ingestor
        self.watch = watch
        self.cadence = cadence
        self.season = 0
        self.windows: dict[uuid.UUID, EventWindow] = {}
        self.states: dict[uuid.UUID, EventState] = {}

    def start(self) -> None:
        with self.ingestor.engine.connect() as conn:
            self.season = latest_season(conn)
        self.ingestor.ingest_season_events(self.season)
        with self.ingestor.engine.connect() as conn:
            watched = self.watch.resolve(conn)
        default_tz = self.ingestor.settings.default_timezone
        self.windows = {e.event_id: EventWindow.of(e, default_tz) for e in watched}
        self.states = {event_id: EventState() for event_id in self.windows}
        log.info("poll.start", season=self.season, events=[w.code for w in self.windows.values()])

    def tick(self, now: dt.datetime) -> None:
        for event_id, window in self.windows.items():
            state = self.states[event_id]
            if schedule.finished(now, window, state, self.cadence):
                continue
            fetched = [e for e in schedule.due(now, window, state, self.cadence) if self._fetch(window, state, e)]
            schedule.record(state, fetched, now, window)
            if state.closed_at is None and {Endpoint.HYBRID_PLAYOFF, Endpoint.ALLIANCES} & set(fetched):
                with self.ingestor.engine.connect() as conn:
                    if _final_decided(conn, event_id):
                        state.closed_at = now
                        log.info("poll.closed", event_code=window.code)

    def run(
        self,
        should_stop: Callable[[], bool],
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        sleep: Callable[[float], None] = time.sleep,
        tick_s: float = 1.0,
    ) -> None:
        self.start()
        while not should_stop():
            self.tick(clock())
            sleep(tick_s)

    def _fetch(self, window: EventWindow, state: EventState, endpoint: Endpoint) -> bool:
        try:
            results = self.ingestor.ingest_endpoint(self.season, window.code, window.event_id, endpoint)
        except Exception as exc:
            log.warning("poll.fetch_failed", event_code=window.code, endpoint=endpoint.value, error=str(exc))
            return False
        level = _HYBRID.get(endpoint)
        if level is not None and any(r.opened for r in results):
            state.changed.add(level)
        return True


def _final_decided(conn: Connection, event_id: uuid.UUID) -> bool:
    m = core.match.c
    alliances = conn.execute(
        select(func.count()).select_from(core.playoff_alliance).where(core.playoff_alliance.c.event_id == event_id)
    ).scalar_one()
    rows = conn.execute(
        select(m.series, m.match_number, m.score_red_final, m.score_blue_final).where(
            m.event_id == event_id, m.level.in_(ELIMINATION_MATCHES)
        )
    )
    return schedule.final_decided(alliances, (PlayoffResult(*r) for r in rows))
