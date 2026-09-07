"""The FTC Events HTTP client.

Every request is conditional, rate limited, and retried on a 5xx.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
import structlog

from warehouse.config import Settings, load_settings

log = structlog.get_logger(__name__)

Outcome = Literal["changed", "not_modified", "empty"]

RETRYABLE = frozenset({500, 502, 503, 504})


class FtcEventsError(RuntimeError):
    pass


class AuthMissingError(FtcEventsError):
    pass


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One conditional fetch. Only some endpoints carry Last-Modified, which is what `cacheable` reports."""

    endpoint: str
    status: int
    outcome: Outcome
    body: bytes
    last_modified: str | None
    data: Any

    @property
    def changed(self) -> bool:
        return self.outcome == "changed"

    @property
    def cacheable(self) -> bool:
        return self.last_modified is not None


def _is_empty(data: Any) -> bool:
    if data is None:
        return True
    # /advancement/{code}/points answers with a bare JSON array rather than an object.
    if isinstance(data, list | str):
        return len(data) == 0
    if isinstance(data, dict):
        if not data:
            return True
        collections = [v for v in data.values() if isinstance(v, list)]
        if collections and all(len(v) == 0 for v in collections):
            return True
    return False


class RateLimiter:
    """A minimum gap between requests. The server's budget is unpublished."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        gap = time.monotonic() - self._last
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last = time.monotonic()


class FtcEventsClient:
    """Conditional, rate limited, backing off on a 5xx."""

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings or load_settings()
        self._limiter = RateLimiter(self.settings.ftc_events_min_interval_s)
        self._client = httpx.Client(
            base_url=self.settings.ftc_events_base_url,
            auth=(self.settings.ftc_events_username, self.settings.ftc_events_token),
            timeout=self.settings.ftc_events_timeout_s,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def __enter__(self) -> FtcEventsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------------------------------------- fetching

    def fetch(
        self,
        endpoint: str,
        *,
        if_modified_since: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ApiResponse:
        if not self.settings.ftc_events_configured():
            raise AuthMissingError("WAREHOUSE_FTC_EVENTS_USERNAME and _TOKEN are required for live requests")

        headers: dict[str, str] = {}
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since

        return self._interpret(endpoint, self._request(endpoint, headers=headers, params=params))

    def fetch_all_pages(
        self,
        endpoint: str,
        *,
        if_modified_since: str | None = None,
        params: dict[str, Any] | None = None,
        collection: str = "teams",
    ) -> ApiResponse:
        """Fetch a paginated endpoint and merge its pages into one payload."""
        first = self.fetch(endpoint, if_modified_since=if_modified_since, params=params)
        if first.outcome != "changed" or not isinstance(first.data, dict):
            return first

        page_total = int(first.data.get("pageTotal") or 1)
        if page_total <= 1:
            return first

        merged: list[Any] = list(first.data.get(collection) or [])
        for page in range(2, page_total + 1):
            following = self.fetch(endpoint, params={**(params or {}), "page": page})
            if isinstance(following.data, dict):
                merged.extend(following.data.get(collection) or [])

        data = {**first.data, collection: merged, "pageCurrent": 1, "pageTotal": 1}
        body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
        log.info("ftc_events.paged", endpoint=endpoint, pages=page_total, rows=len(merged))
        return ApiResponse(endpoint, first.status, "changed", body, first.last_modified, data)

    def _interpret(self, endpoint: str, response: httpx.Response) -> ApiResponse:
        last_modified = response.headers.get("Last-Modified")

        if response.status_code == 304:
            return ApiResponse(endpoint, 304, "not_modified", b"", last_modified, None)

        body = response.content
        data = response.json() if body else None

        if _is_empty(data):
            return ApiResponse(endpoint, response.status_code, "empty", body, None, data)

        return ApiResponse(endpoint, response.status_code, "changed", body, last_modified, data)

    def _request(
        self,
        endpoint: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            self._limiter.wait()
            response = self._client.get(endpoint, headers=headers, params=params)

            if response.status_code not in RETRYABLE:
                if response.status_code >= 400 and response.status_code != 404:
                    raise FtcEventsError(f"{endpoint} -> HTTP {response.status_code}")
                return response

            attempt += 1
            if attempt > self.settings.ftc_events_max_retries:
                raise FtcEventsError(f"{endpoint} -> HTTP {response.status_code} after {attempt} attempts")
            delay = self._backoff_delay(response, attempt)
            log.warning(
                "ftc_events.backoff",
                endpoint=endpoint,
                status=response.status_code,
                attempt=attempt,
                delay_s=round(delay, 2),
            )
            time.sleep(delay)

    @staticmethod
    def _backoff_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    delta = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError):
                    delta = 0.0
                return max(0.0, delta)
        return min(60.0, 2.0**attempt) + random.random()

    # ------------------------------------------------------------------------------------------------ endpoints

    def season_events(self, season: int, **params: Any) -> ApiResponse:
        return self.fetch(f"/{season}/events", params=params or None)

    def season_teams(self, season: int, *, if_modified_since: str | None = None) -> ApiResponse:
        return self.fetch_all_pages(f"/{season}/teams", if_modified_since=if_modified_since, collection="teams")

    def event_teams(self, season: int, event_code: str, *, if_modified_since: str | None = None) -> ApiResponse:
        return self.fetch_all_pages(
            f"/{season}/teams",
            params={"eventCode": event_code},
            if_modified_since=if_modified_since,
            collection="teams",
        )

    def hybrid_schedule(
        self,
        season: int,
        event_code: str,
        tournament_level: str,
        *,
        if_modified_since: str | None = None,
    ) -> ApiResponse:
        """Schedule and results together, with the per-slot flags, in one call.

        The tournament_level parameter accepts only qual and playoff, while the response carries the whole six-value
        level enum.
        """
        return self.fetch(
            f"/{season}/schedule/{event_code}/{tournament_level}/hybrid",
            if_modified_since=if_modified_since,
        )

    def scores(self, season: int, event_code: str, tournament_level: str, **kw: Any) -> ApiResponse:
        return self.fetch(f"/{season}/scores/{event_code}/{tournament_level}", **kw)

    def rankings(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        return self.fetch(f"/{season}/rankings/{event_code}", **kw)

    def awards(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        """An empty response mid-event is expected."""
        return self.fetch(f"/{season}/awards/{event_code}", **kw)

    def alliances(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        """The seated playoff alliances. Empty for a league meet, a league tournament or an Other."""
        return self.fetch(f"/{season}/alliances/{event_code}", **kw)

    def alliance_selection(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        """The pick log in the order it happened, carrying declines and removals."""
        return self.fetch(f"/{season}/alliances/{event_code}/selection", **kw)

    def advancement_points(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        """DECODE onwards."""
        return self.fetch(f"/{season}/advancement/{event_code}/points", **kw)

    def advancement_slots(self, season: int, event_code: str, **kw: Any) -> ApiResponse:
        return self.fetch(f"/{season}/advancement/{event_code}", **kw)
