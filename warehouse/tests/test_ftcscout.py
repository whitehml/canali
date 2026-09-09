"""The FTCScout GraphQL client, against a mock transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from warehouse.config import Settings
from warehouse.ingest.ftcscout import (
    BACKFILL_SEASONS,
    COMMON_SCORE_FIELDS,
    FtcScoutClient,
    FtcScoutError,
    events_with_matches_query,
)

Handler = Callable[[httpx.Request], httpx.Response]

EVENTS = [
    {"code": "USPAX", "regionCode": "USPA", "remote": False, "hybrid": False, "awards": [{"teamNumber": 1}]},
    {"code": "USPAR", "regionCode": "USPA", "remote": True, "hybrid": False, "awards": [{"teamNumber": 2}]},
    {"code": "USPAH", "regionCode": "USPA", "remote": False, "hybrid": True, "awards": [{"teamNumber": 3}]},
]


def _client(handler: Handler, bulk_timeout_s: float = 300.0) -> FtcScoutClient:
    settings = Settings(
        ftcscout_url="https://scout.test/graphql",
        ftc_events_timeout_s=30.0,
        ftcscout_bulk_timeout_s=bulk_timeout_s,
    )
    return FtcScoutClient(settings, transport=httpx.MockTransport(handler))


def _answers(payload: object) -> Handler:
    return lambda request: httpx.Response(200, json=payload)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warehouse.ingest.ftcscout.time.sleep", lambda _: None)


# ------------------------------------------------------------------------------------------------------ failure


def test_a_failed_query_arrives_as_a_200_and_still_raises() -> None:
    """GraphQL reports a bad query with an errors array under a 200."""
    handler = _answers({"errors": [{"message": "Cannot query field allianceRole"}], "data": None})

    with _client(handler) as client, pytest.raises(FtcScoutError, match="allianceRole"):
        client.events_with_matches(2025)


def test_a_response_carrying_no_data_is_an_empty_result_not_a_crash() -> None:
    with _client(_answers({"data": None})) as client:
        assert client.events_with_matches(2025) == []


def test_a_persistent_429_is_retried_and_then_raises() -> None:
    """FTCScout throttles with a 429."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    with _client(handler) as client, pytest.raises(FtcScoutError, match="429"):
        client.execute("query {}", {}, retries=2)

    assert calls["n"] == 3


def test_a_429_that_clears_returns_the_answer_rather_than_raising() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": {"eventsSearch": EVENTS[:1]}})

    with _client(handler) as client:
        assert [e["code"] for e in client.events_with_matches(2025)] == ["USPAX"]

    assert calls["n"] == 2


def test_a_404_raises_without_retrying() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="no such endpoint")

    with _client(handler) as client, pytest.raises(FtcScoutError, match="404"):
        client.execute("query {}", {})

    assert calls["n"] == 1


# ------------------------------------------------------------------------------------------------- the requests


def test_a_window_is_sent_as_query_variables() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"eventsSearch": []}})

    with _client(handler) as client:
        client.events_with_matches(2025, "2025-11-01", "2025-12-01")

    assert sent[0]["variables"] == {"season": 2025, "start": "2025-11-01", "end": "2025-12-01"}


def test_every_backfill_season_has_a_score_fragment() -> None:
    query = events_with_matches_query()

    for season in BACKFILL_SEASONS:
        assert f"... on MatchScores{season}" in query
    for field in COMMON_SCORE_FIELDS:
        assert field in query


def test_the_worldwide_awards_call_gets_the_bulk_timeout() -> None:
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"data": {"eventsSearch": EVENTS}})

    with _client(handler) as client:
        client.season_awards(2025)
        client.events_with_matches(2025)

    assert seen == [300.0, 30.0]


# ------------------------------------------------------------------------------------------------------ filtering


@pytest.mark.parametrize("call", ["events_with_matches", "alliance_roles", "season_awards"])
def test_remote_and_hybrid_events_are_dropped_by_every_endpoint(call: str) -> None:
    with _client(_answers({"data": {"eventsSearch": EVENTS}})) as client:
        events = getattr(client, call)(2025)

    assert [e["code"] for e in events] == ["USPAX"]


def test_an_event_with_no_awards_is_not_returned_as_one() -> None:
    payload = {"data": {"eventsSearch": [{"code": "A", "awards": []}, {"code": "B", "awards": [{"teamNumber": 1}]}]}}
    with _client(_answers(payload)) as client:
        assert [e["code"] for e in client.season_awards(2025)] == ["B"]
