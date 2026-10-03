"""The ingest pipeline, against synthetic payloads and a mock transport."""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Connection, Engine, func, select, text

from warehouse.config import Settings
from warehouse.ingest.client import FtcEventsClient
from warehouse.ingest.payloads import PayloadStore
from warehouse.ingest.pipeline import Ingestor
from warehouse.poll.cadence import Cadence
from warehouse.poll.loop import Poller
from warehouse.poll.watch import WatchList
from warehouse.rules import loader
from warehouse.rules.model import RulePack
from warehouse.rules.validate import BreakdownValidationError
from warehouse.schema import core
from warehouse.testing import SyntheticEvent, generate_event

pytestmark = pytest.mark.db

PACK = Path(__file__).parent / "fixtures" / "rule_packs" / "9999_synthetic.toml"

LAST_MODIFIED = "Sun, 15 Mar 2026 19:52:21 GMT"
LATER = "Mon, 16 Mar 2026 08:00:00 GMT"

CACHEABLE = ("/hybrid", "/scores/")


class FakeApi:
    """Serves one synthetic event, honouring If-Modified-Since where the real API does."""

    def __init__(self, event: SyntheticEvent) -> None:
        self.event = event
        self.calls: list[tuple[str, str | None]] = []
        self.last_modified = LAST_MODIFIED

    def body(self, path: str) -> Any:
        e = self.event
        if path.endswith("/events"):
            return e.events
        if "/teams" in path:
            return e.teams
        if "/hybrid" in path:
            return e.hybrid_qual if "/qual/" in path else e.hybrid_playoff
        if "/scores/" in path:
            return e.scores_qual if path.endswith("/qual") else e.scores_playoff
        if "/rankings/" in path:
            return e.rankings
        if "/awards/" in path:
            return e.awards
        if path.endswith("/selection"):
            return e.selection
        if "/alliances/" in path:
            return e.alliances
        if path.endswith("/points"):
            return e.advancement_points
        if "/advancement/" in path:
            return e.advancement
        raise AssertionError(f"the pipeline asked for an endpoint the fixture does not serve: {path}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if_modified_since = request.headers.get("If-Modified-Since")
        self.calls.append((path, if_modified_since))

        if not any(marker in path for marker in CACHEABLE):
            return httpx.Response(200, json=self.body(path))
        if if_modified_since == self.last_modified:
            return httpx.Response(304, headers={"Last-Modified": self.last_modified})
        return httpx.Response(200, json=self.body(path), headers={"Last-Modified": self.last_modified})

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


@pytest.fixture
def synthetic() -> SyntheticEvent:
    return generate_event()


@pytest.fixture
def api(synthetic: SyntheticEvent) -> FakeApi:
    return FakeApi(synthetic)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ftc_events_username="user",
        ftc_events_token="token",
        ftc_events_min_interval_s=0.0,
        payload_root=tmp_path,
    )


@pytest.fixture
def ingestor(clean_engine: Engine, api: FakeApi, settings: Settings) -> Iterator[Ingestor]:
    with clean_engine.begin() as conn:
        loader.load(conn, [RulePack.from_file(PACK)])
    client = FtcEventsClient(settings, transport=httpx.MockTransport(api.handler))
    yield Ingestor(clean_engine, client, settings)
    client.close()


def counts(conn: Connection, *tables: Any) -> dict[str, int]:
    return {t.name: int(conn.execute(select(func.count()).select_from(t)).scalar() or 0) for t in tables}


def scalar(engine: Engine, sql: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


# ------------------------------------------------------------------------------------------------------ one event


def test_one_event_fills_every_table_it_touches(ingestor: Ingestor, synthetic: SyntheticEvent) -> None:
    report = ingestor.ingest_event(synthetic.season, synthetic.code)

    assert report.skipped_reason is None
    assert report.changed
    with ingestor.engine.connect() as conn:
        assert counts(
            conn,
            core.event,
            core.team,
            core.event_team,
            core.match,
            core.match_team,
            core.match_breakdown,
            core.ranking,
            core.award,
            core.playoff_alliance,
            core.playoff_alliance_pick,
            core.advancement_slot,
            core.advancement_points,
            core.event_advancement,
        ) == {
            "event": 1,
            "team": 12,
            "event_team": 12,
            "match": 24,
            "match_team": 96,
            "match_breakdown": 48,
            "ranking": 12,
            "award": 3,
            "playoff_alliance": 2,
            "playoff_alliance_pick": 5,
            "advancement_slot": 4,
            "advancement_points": 12,
            "event_advancement": 1,
        }


def test_every_row_is_traceable_to_a_stored_payload(ingestor: Ingestor, synthetic: SyntheticEvent) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)

    with ingestor.engine.connect() as conn:
        stored = set(conn.execute(text("SELECT payload_hash FROM raw.raw_payload")).scalars())
        cursors = set(conn.execute(text("SELECT payload_hash FROM raw.ingest_run")).scalars())

    assert cursors and cursors <= stored
    store = PayloadStore(ingestor.settings.payload_root)
    assert all(json.loads(store.get(payload_hash)) for payload_hash in stored)


def test_a_second_pass_over_an_unchanged_event_writes_nothing(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)
    ingested_at = scalar(ingestor.engine, "SELECT max(ingested_at_utc) FROM core.match")

    report = ingestor.ingest_event(synthetic.season, synthetic.code)

    assert not report.changed
    assert [s.rows for s in report.steps] == [0] * len(report.steps)
    assert scalar(ingestor.engine, "SELECT max(ingested_at_utc) FROM core.match") == ingested_at
    assert scalar(ingestor.engine, "SELECT count(*) FROM raw.ingest_diff") == 0


def test_a_cacheable_endpoint_is_asked_conditionally_the_second_time(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)
    api.calls.clear()
    ingestor.ingest_event(synthetic.season, synthetic.code)

    conditional = {path for path, header in api.calls if header == LAST_MODIFIED}
    assert conditional == {path for path in api.paths if any(m in path for m in CACHEABLE)}
    assert scalar(ingestor.engine, "SELECT sum(check_count) FROM raw.ingest_run") == 2 * len(api.calls)


def test_an_uncacheable_endpoint_is_stopped_by_the_payload_hash_instead(
    ingestor: Ingestor, synthetic: SyntheticEvent
) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)
    ingestor.ingest_event(synthetic.season, synthetic.code)

    with ingestor.engine.connect() as conn:
        runs = conn.execute(
            text("SELECT endpoint, run_count, check_count FROM raw.ingest_run WHERE endpoint LIKE '%rankings%'")
        ).all()

    assert [(r.run_count, r.check_count) for r in runs] == [(1, 2)]


# --------------------------------------------------------------------------------------------------------- replay


def test_a_replayed_match_is_overwritten_in_place_and_recorded(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)
    before = scalar(ingestor.engine, "SELECT score_red_final FROM core.match WHERE match_number = 3 AND series = 0")

    api.event = synthetic.replay(3)
    api.last_modified = LATER
    ingestor.ingest_event(synthetic.season, synthetic.code)

    assert scalar(ingestor.engine, "SELECT score_red_final FROM core.match WHERE match_number = 3 AND series = 0") == (
        before + 17
    )
    assert scalar(ingestor.engine, "SELECT count(*) FROM core.match") == 24
    with ingestor.engine.connect() as conn:
        diff = conn.execute(text("SELECT table_name, key, before, after FROM raw.ingest_diff")).mappings().all()
    assert len(diff) == 1
    assert diff[0]["table_name"] == "core.match"
    assert diff[0]["key"]["match_number"] == 3
    assert diff[0]["after"]["score_red_final"] == before + 17


# -------------------------------------------------------------------------------------------------------- skipped


def test_an_event_the_api_does_not_know_is_skipped_rather_than_invented(ingestor: Ingestor) -> None:
    report = ingestor.ingest_event(9999, "NOSUCH")

    assert report.skipped_reason == "not found"
    assert report.steps == []
    assert scalar(ingestor.engine, "SELECT count(*) FROM core.event") == 0


def test_a_remote_event_is_skipped_before_anything_is_written(
    clean_engine: Engine, settings: Settings, tmp_path: Path
) -> None:
    remote = generate_event(remote=True)
    api = FakeApi(remote)
    client = FtcEventsClient(settings, transport=httpx.MockTransport(api.handler))
    report = Ingestor(clean_engine, client, settings).ingest_event(remote.season, remote.code)
    client.close()

    assert report.skipped_reason == "remote or hybrid event"
    assert scalar(clean_engine, "SELECT count(*) FROM core.event") == 0


# ------------------------------------------------------------------------------------------------ score detail


def test_a_breakdown_that_disagrees_with_the_pack_stops_the_event(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    broken = copy.deepcopy(synthetic)
    broken.scores_qual["matchScores"][0]["alliances"][0]["autoWidgetPoints"] = "twelve"
    api.event = broken

    with pytest.raises(BreakdownValidationError, match="autoWidgetPoints"):
        ingestor.ingest_event(synthetic.season, synthetic.code)


def test_a_component_the_pack_does_not_declare_is_stored_rather_than_refused(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    extended = copy.deepcopy(synthetic)
    for row in extended.scores_qual["matchScores"]:
        for side in row["alliances"]:
            side["brandNewThing"] = 4
    api.event = extended

    ingestor.ingest_event(synthetic.season, synthetic.code)

    assert scalar(ingestor.engine, "SELECT breakdown->>'brandNewThing' FROM core.match_breakdown LIMIT 1") == "4"


# ------------------------------------------------------------------------------------------------------- alliances


def test_a_seated_pick_carries_the_alliance_it_joined(ingestor: Ingestor, synthetic: SyntheticEvent) -> None:
    ingestor.ingest_event(synthetic.season, synthetic.code)

    with ingestor.engine.connect() as conn:
        picks = conn.execute(
            text("SELECT action, alliance_number FROM core.playoff_alliance_pick ORDER BY pick_ordinal")
        ).all()

    assert [(p.action, p.alliance_number) for p in picks] == [
        ("CAPTAIN", 1),
        ("CAPTAIN", 2),
        ("DECLINE", None),
        ("ACCEPT", 1),
        ("ACCEPT", 2),
    ]


def test_an_event_with_no_playoff_is_empty_rather_than_a_gap(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    league_meet = copy.deepcopy(synthetic)
    league_meet.alliances = {"alliances": [], "count": 0}
    league_meet.selection = {"selections": [], "count": 0}
    api.event = league_meet

    report = ingestor.ingest_event(synthetic.season, synthetic.code)

    alliances = [s for s in report.steps if "/alliances/" in s.endpoint]
    assert [s.outcome for s in alliances] == ["empty", "empty"]
    assert not any(s.opened for s in alliances)
    with ingestor.engine.connect() as conn:
        assert counts(conn, core.playoff_alliance, core.playoff_alliance_pick) == {
            "playoff_alliance": 0,
            "playoff_alliance_pick": 0,
        }
        runs = conn.execute(
            text(
                "SELECT empty_count, run_count, last_modified FROM raw.ingest_run "
                "WHERE endpoint LIKE '%/alliances/%' ORDER BY endpoint"
            )
        ).all()
    # Nothing to remember: an empty answer carries no cursor, so the next pass asks again.
    assert [(r.empty_count, r.run_count, r.last_modified) for r in runs] == [(1, 0, None), (1, 0, None)]


def test_the_alliance_backfill_reaches_only_events_with_a_playoff_match(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    unpublished = copy.deepcopy(synthetic)
    unpublished.alliances = {"alliances": [], "count": 0}
    unpublished.selection = {"selections": [], "count": 0}
    api.event = unpublished
    ingestor.ingest_event(synthetic.season, synthetic.code)

    api.event = synthetic
    assert ingestor.backfill_alliances(synthetic.season)["events"] == 1
    with ingestor.engine.connect() as conn:
        assert counts(conn, core.playoff_alliance, core.playoff_alliance_pick) == {
            "playoff_alliance": len(synthetic.alliances["alliances"]),
            "playoff_alliance_pick": len(synthetic.selection["selections"]),
        }

    with ingestor.engine.begin() as conn:
        playoff = "SELECT match_id FROM core.match WHERE level <> 'QUALIFICATION'"
        conn.execute(text(f"DELETE FROM core.match_team WHERE match_id IN ({playoff})"))
        conn.execute(text(f"DELETE FROM core.match_breakdown WHERE match_id IN ({playoff})"))
        conn.execute(text("DELETE FROM core.match WHERE level <> 'QUALIFICATION'"))
    api.calls.clear()

    assert ingestor.backfill_alliances(synthetic.season)["events"] == 0
    assert not any("/alliances/" in path for path in api.paths)


# ------------------------------------------------------------------------------------------------------ live signal

_RESULTS = ("scoreRedFinal", "scoreBlueFinal", "scoreRedAuto", "scoreBlueAuto", "scoreRedFoul", "scoreBlueFoul")


def _publish(api: FakeApi, synthetic: SyntheticEvent, scored: int, stamp: str) -> None:
    """The qualification schedule with the first ``scored`` matches played and the rest listed but unplayed."""
    schedule = copy.deepcopy(synthetic.hybrid_qual["schedule"])
    for row in schedule[scored:]:
        row.update(dict.fromkeys(_RESULTS))
    api.event.hybrid_qual = {"schedule": schedule}
    api.last_modified = stamp


def test_only_the_poller_signals_and_each_match_once(
    ingestor: Ingestor, synthetic: SyntheticEvent, api: FakeApi
) -> None:
    api.event = copy.deepcopy(synthetic)
    api.event.hybrid_playoff = {"schedule": []}
    n_quals = len(synthetic.hybrid_qual["schedule"])

    _publish(api, synthetic, 3, "Sat, 10 Jan 2026 15:00:00 GMT")
    ingestor.ingest_event(synthetic.season, synthetic.code)
    assert scalar(ingestor.engine, "SELECT count(*) FROM raw.match_signal") == 0

    cadence = Cadence(hybrid_s=60, after_close_s=[3600], restart_s=30, sweep_at=dt.time(6))
    poller = Poller(ingestor, WatchList(events=[synthetic.code]), cadence)
    poller.start()
    now = next(iter(poller.windows.values())).opens_at + dt.timedelta(hours=12)
    for scored, stamp in ((7, "Sat, 10 Jan 2026 16:00:00 GMT"), (n_quals, "Sat, 10 Jan 2026 17:00:00 GMT")):
        _publish(api, synthetic, scored, stamp)
        for _ in range(2):
            poller.tick(now)
            now += dt.timedelta(minutes=2)

    signals = "SELECT count(*), count(DISTINCT match_id) FROM raw.match_signal WHERE kind = 'scored'"
    with ingestor.engine.connect() as conn:
        assert tuple(conn.execute(text(signals)).one()) == (n_quals - 3, n_quals - 3)
    assert scalar(ingestor.engine, "SELECT count(*) FROM raw.ingest_diff") == 0

    corrected = copy.deepcopy(synthetic.hybrid_qual)
    corrected["schedule"][0]["scoreRedFinal"] += 5
    api.event.hybrid_qual, api.last_modified = corrected, "Sun, 11 Jan 2026 12:00:00 GMT"
    poller.tick(now)

    assert scalar(ingestor.engine, "SELECT count(*) FROM raw.match_signal WHERE kind = 'replayed'") == 1
    assert scalar(ingestor.engine, "SELECT count(*) FROM raw.ingest_diff") == 1


# ---------------------------------------------------------------------------------------------------- season scope


def test_a_widened_region_reapplies_the_stored_payload_without_fetching_again(
    clean_engine: Engine, settings: Settings, api: FakeApi, synthetic: SyntheticEvent
) -> None:
    client = FtcEventsClient(settings, transport=httpx.MockTransport(api.handler))
    ingestor = Ingestor(clean_engine, client, settings)

    narrow = ingestor.ingest_season_events(synthetic.season, region_code="USNY")
    assert narrow.rows == 0

    api.calls.clear()
    widened = ingestor.ingest_season_events(synthetic.season, region_code="USPA")
    client.close()

    assert widened.rows == 1
    assert widened.opened
    assert api.calls == [(f"/v2.0/{synthetic.season}/events", None)]
    assert scalar(clean_engine, "SELECT count(*) FROM core.event") == 1
