"""The FTCScout GraphQL client.

A second source of what FTC Events already publishes, and FTCScout reads FTC Events to get it. What it offers is a
larger traffic allowance.

Its copy has been observed to drop values, so FTC Events is authoritative wherever the two disagree.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import httpx
import structlog

from warehouse.config import Settings, load_settings

log = structlog.get_logger(__name__)

RETRYABLE = frozenset({429, 500, 502, 503, 504})

BACKFILL_SEASONS: tuple[int, ...] = (2022, 2023, 2024, 2025)

COMMON_SCORE_FIELDS: tuple[str, ...] = (
    "totalPoints",
    "totalPointsNp",
    "autoPoints",
    "dcPoints",
    "penaltyPointsCommitted",
    "penaltyPointsByOpp",
    "minorsCommitted",
    "majorsCommitted",
    "minorsByOpp",
    "majorsByOpp",
)


class FtcScoutError(RuntimeError):
    pass


def _scores_fragment(seasons: Iterable[int] = BACKFILL_SEASONS) -> str:
    fields = " ".join(COMMON_SCORE_FIELDS)
    return " ".join(f"... on MatchScores{year} {{ red {{ {fields} }} blue {{ {fields} }} }}" for year in seasons)


def events_with_matches_query(seasons: Iterable[int] = BACKFILL_SEASONS) -> str:
    """Events and their matches in one request, windowed by start date.

    ``eventsSearch`` takes start and end but no offset, so the backfill pages by date window. Re-running a window
    is idempotent.
    """
    return f"""
query EventsWithMatches($season: Int!, $start: Date, $end: Date) {{
  eventsSearch(season: $season, hasMatches: true, start: $start, end: $end) {{
    code name start end type regionCode remote hybrid
    matches {{
      id hasBeenPlayed tournamentLevel series matchNum description
      scheduledStartTime actualStartTime postResultTime
      teams {{ teamNumber alliance station surrogate noShow dq onField allianceRole }}
      scores {{ {_scores_fragment(seasons)} }}
    }}
  }}
}}"""


ALLIANCE_ROLES_QUERY = """
query AllianceRoles($season: Int!, $start: Date, $end: Date) {
  eventsSearch(season: $season, hasMatches: true, start: $start, end: $end) {
    code regionCode remote hybrid
    matches {
      tournamentLevel series matchNum
      teams { teamNumber alliance station allianceRole }
    }
  }
}
"""

SEASON_AWARDS_QUERY = """
query SeasonAwards($season: Int!, $limit: Int!) {
  eventsSearch(season: $season, limit: $limit) {
    season
    code
    awards { teamNumber type placement }
  }
}
"""


class FtcScoutClient:
    """One GraphQL endpoint, retried on a 429 or a 5xx."""

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings or load_settings()
        self._client = httpx.Client(
            timeout=self.settings.ftc_events_timeout_s,
            headers={"Content-Type": "application/json"},
            transport=transport,
        )

    def __enter__(self) -> FtcScoutClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------------------------------------ requesting

    def execute(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        retries: int = 4,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            response = self._client.post(
                self.settings.ftcscout_url,
                json={"query": query, "variables": variables},
                timeout=timeout if timeout is not None else self._client.timeout,
            )
            if response.status_code in RETRYABLE and attempt < retries:
                attempt += 1
                delay = min(30.0, 2.0**attempt)
                log.warning(
                    "ftcscout.backoff",
                    status=response.status_code,
                    attempt=attempt,
                    delay_s=delay,
                )
                time.sleep(delay)
                continue
            if response.status_code != 200:
                raise FtcScoutError(f"HTTP {response.status_code}: {response.text[:200]}")

            payload = response.json()
            # GraphQL reports a failed query as a 200 carrying an errors array, so the status is not a success check.
            if payload.get("errors"):
                raise FtcScoutError(str(payload["errors"])[:400])
            data: dict[str, Any] = payload.get("data") or {}
            return data

    # ------------------------------------------------------------------------------------------------- endpoints

    def events_with_matches(
        self, season: int, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """One date window's events, each with its full match list."""
        data = self.execute(events_with_matches_query(), {"season": season, "start": start, "end": end})
        return _in_person(data.get("eventsSearch") or [])

    def alliance_roles(self, season: int, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """One date window's events, each with its match slots and their roles."""
        data = self.execute(ALLIANCE_ROLES_QUERY, {"season": season, "start": start, "end": end})
        return _in_person(data.get("eventsSearch") or [])

    def season_awards(self, season: int, limit: int = 6000) -> list[dict[str, Any]]:
        """Every award in one season, worldwide, in one call."""
        data = self.execute(
            SEASON_AWARDS_QUERY,
            {"season": season, "limit": limit},
            timeout=self.settings.ftcscout_bulk_timeout_s,
        )
        return [event for event in _in_person(data.get("eventsSearch") or []) if event.get("awards")]


def _in_person(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if not event.get("remote") and not event.get("hybrid")]
