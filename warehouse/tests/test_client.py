"""Conditional fetching against a mock transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from warehouse.config import Settings
from warehouse.ingest.client import AuthMissingError, FtcEventsClient, FtcEventsError

LAST_MODIFIED = "Sun, 15 Mar 2026 19:52:21 GMT"

Handler = Callable[[httpx.Request], httpx.Response]


def _settings() -> Settings:
    return Settings(
        ftc_events_username="user",
        ftc_events_token="token",
        ftc_events_min_interval_s=0.0,
        ftc_events_max_retries=2,
    )


def _client(handler: Handler) -> FtcEventsClient:
    return FtcEventsClient(_settings(), transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------------------------------- conditional


def test_a_repeated_request_is_answered_304_rather_than_a_silent_200() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("If-Modified-Since")
        seen.append(header)
        if header == LAST_MODIFIED:
            return httpx.Response(304, headers={"Last-Modified": LAST_MODIFIED})
        return httpx.Response(200, json={"events": [{"code": "X"}]}, headers={"Last-Modified": LAST_MODIFIED})

    with _client(handler) as client:
        first = client.fetch("/2025/events")
        assert first.outcome == "changed"
        assert first.cacheable

        second = client.fetch("/2025/events", if_modified_since=first.last_modified)
        assert second.outcome == "not_modified"
        assert second.status == 304

    assert seen == [None, LAST_MODIFIED]


def test_an_empty_result_is_a_third_outcome_and_carries_no_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"alliances": [], "count": 0}, headers={"Last-Modified": LAST_MODIFIED})

    with _client(handler) as client:
        response = client.fetch("/2025/alliances/X")

    assert response.outcome == "empty"
    assert response.last_modified is None


def test_a_result_without_last_modified_is_data_rather_than_emptiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rankings": [{"rank": 1, "teamNumber": 42}]})

    with _client(handler) as client:
        response = client.fetch("/2025/rankings/X")

    assert response.outcome == "changed"
    assert response.cacheable is False


def test_a_bare_array_is_read_as_data_and_an_empty_one_as_emptiness() -> None:
    with _client(lambda r: httpx.Response(200, json=[{"team": 1, "points": [10.0]}])) as client:
        assert client.fetch("/2025/advancement/X/points").outcome == "changed"

    with _client(lambda r: httpx.Response(200, json=[])) as client:
        assert client.fetch("/2025/advancement/X/points").outcome == "empty"


def test_a_payload_whose_collections_are_all_empty_is_empty_but_one_filled_collection_is_not() -> None:
    with _client(lambda r: httpx.Response(200, json={"alliances": [], "count": 0})) as client:
        assert client.fetch("/2025/alliances/X").outcome == "empty"

    with _client(lambda r: httpx.Response(200, json={"selections": [{"index": 0}], "count": 1})) as client:
        assert client.fetch("/2025/alliances/X/selection").outcome == "changed"


# ------------------------------------------------------------------------------------------------------ failure


def test_a_503_backs_off_and_then_gives_up() -> None:
    """429 is not in the API's documented set; 503 is the throttling shape."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "0"})

    with _client(handler) as client, pytest.raises(FtcEventsError, match="503"):
        client.fetch("/2025/events")

    assert calls["n"] == 3


def test_a_retry_after_in_the_past_does_not_sleep_backwards() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "Sun, 15 Mar 2026 19:52:21 GMT"})

    with _client(handler) as client, pytest.raises(FtcEventsError, match="503"):
        client.fetch("/2025/events")


def test_a_404_is_an_empty_result_rather_than_a_raise() -> None:
    with _client(lambda r: httpx.Response(404)) as client:
        response = client.fetch("/2025/alliances/NOSUCH")

    assert response.status == 404
    assert response.outcome == "empty"


def test_a_401_raises_rather_than_returning_nothing() -> None:
    with _client(lambda r: httpx.Response(401)) as client, pytest.raises(FtcEventsError, match="401"):
        client.fetch("/2025/events")


def test_missing_credentials_fail_before_any_request_is_sent() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json={})

    client = FtcEventsClient(
        Settings(ftc_events_username="", ftc_events_token=""),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AuthMissingError, match="USERNAME"):
        client.fetch("/2025/events")
    client.close()
    assert sent == []


# ------------------------------------------------------------------------------------------------------ paging


def test_a_paginated_endpoint_is_merged_into_one_complete_payload() -> None:
    pages = {
        1: {"teams": [{"teamNumber": 1}, {"teamNumber": 2}], "pageCurrent": 1, "pageTotal": 3},
        2: {"teams": [{"teamNumber": 3}], "pageCurrent": 2, "pageTotal": 3},
        3: {"teams": [{"teamNumber": 4}], "pageCurrent": 3, "pageTotal": 3},
    }
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        requested.append(page)
        return httpx.Response(200, json=pages[page], headers={"Last-Modified": LAST_MODIFIED})

    with _client(handler) as client:
        response = client.season_teams(2025)

    assert requested == [1, 2, 3]
    assert [t["teamNumber"] for t in response.data["teams"]] == [1, 2, 3, 4]
    assert json.loads(response.body)["teams"] == response.data["teams"]
    assert response.data["pageTotal"] == 1


def test_an_idle_paginated_endpoint_costs_one_request_not_thirty_one() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(304, headers={"Last-Modified": LAST_MODIFIED})

    with _client(handler) as client:
        response = client.season_teams(2025, if_modified_since=LAST_MODIFIED)

    assert response.outcome == "not_modified"
    assert len(calls) == 1
