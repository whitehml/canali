"""The poll loop.

The event list is fetched once at start. Each tick asks the scheduler what is due, fetches it through the ingest
pipeline, and feeds back what changed: a hybrid schedule whose payload changed marks its level, and a changed playoff
is checked for a decided final. On Mondays the event list is fetched again, newly published watched events join, and
the weekly sweep ingests one unwatched event per tick.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Callable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Connection, func, select

from warehouse.ingest.pipeline import Endpoint, Ingestor
from warehouse.poll import schedule
from warehouse.poll.cadence import Cadence
from warehouse.poll.schedule import EventState, EventWindow, Level, PlayoffResult
from warehouse.poll.sweep import sweep_due, sweep_targets
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
        self.tz = ZoneInfo(ingestor.settings.default_timezone)
        self.last_swept: dt.date | None = None
        self.sweep_queue: list[str] = []

    def start(self) -> None:
        with self.ingestor.engine.connect() as conn:
            self.season = latest_season(conn)
        self._refresh()
        log.info("poll.start", season=self.season, events=[w.code for w in self.windows.values()])

    def _refresh(self) -> None:
        """Fetch the event list and add any watched event not yet followed."""
        self.ingestor.ingest_season_events(self.season)
        with self.ingestor.engine.connect() as conn:
            watched = self.watch.resolve(conn)
        for event in watched:
            if event.event_id not in self.windows:
                self.windows[event.event_id] = EventWindow.of(event, self.ingestor.settings.default_timezone)
                self.states[event.event_id] = EventState()

    def queue_sweep(self, today: dt.date) -> None:
        with self.ingestor.engine.connect() as conn:
            self.sweep_queue = sweep_targets(conn, self.season, self.windows.keys(), today)
        log.info("sweep.start", events=len(self.sweep_queue))

    def sweep_one(self) -> None:
        code = self.sweep_queue.pop(0)
        try:
            log.info("sweep.event", summary=self.ingestor.ingest_event(self.season, code).summary())
        except Exception as exc:
            log.warning("sweep.event_failed", event_code=code, error=str(exc))
        if not self.sweep_queue:
            log.info("sweep.done")

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

        if sweep_due(now, self.last_swept, self.cadence.sweep_at, self.tz):
            self.last_swept = now.astimezone(self.tz).date()
            self._refresh()
            self.queue_sweep(self.last_swept)
        if self.sweep_queue:
            self.sweep_one()

    def run(
        self,
        should_stop: Callable[[], bool],
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        sleep: Callable[[float], object] = time.sleep,
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


def supervise(
    make_poller: Callable[[], Poller],
    should_stop: Callable[[], bool],
    restart_s: float,
    sleep: Callable[[float], object] = time.sleep,
) -> int:
    """Run a poller until stopped, starting a fresh one after a crash, and return the number of restarts."""
    restarts = 0
    while not should_stop():
        try:
            make_poller().run(should_stop, sleep=sleep)
        except Exception:
            log.exception("poll.crashed", restarts=restarts)
            restarts += 1
            sleep(restart_s)
    return restarts


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
