"""Live smoke test against FTC Events.

Manually triggered, minimal API hits. It confirms every ingest pathway is still open, not that the data is right.
Not scheduled, and not in CI.

    uv run pytest -m credentialed -q

Requires WAREHOUSE_FTC_EVENTS_USERNAME and WAREHOUSE_FTC_EVENTS_TOKEN.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from warehouse.config import load_settings
from warehouse.ingest.client import ApiResponse, FtcEventsClient

pytestmark = pytest.mark.credentialed

SEASON = 2025
EVENT = "USPACMP"


@pytest.fixture(scope="module")
def client() -> Iterator[FtcEventsClient]:
    settings = load_settings()
    if not settings.ftc_events_configured():
        pytest.skip("no FTC Events credentials configured")
    with FtcEventsClient(settings) as live:
        yield live


def test_every_ingest_pathway_is_still_open(client: FtcEventsClient) -> None:
    """One call per pathway, which is the whole budget."""
    checks: dict[str, ApiResponse] = {
        "events": client.fetch(f"/{SEASON}/events", params={"eventCode": EVENT}),
        "teams": client.event_teams(SEASON, EVENT),
        "hybrid_qual": client.hybrid_schedule(SEASON, EVENT, "qual"),
        "hybrid_playoff": client.hybrid_schedule(SEASON, EVENT, "playoff"),
        "scores": client.scores(SEASON, EVENT, "qual"),
        "rankings": client.rankings(SEASON, EVENT),
        "awards": client.awards(SEASON, EVENT),
        "alliances": client.alliances(SEASON, EVENT),
        "alliance_selection": client.alliance_selection(SEASON, EVENT),
        "advancement_points": client.advancement_points(SEASON, EVENT),
        "advancement_slots": client.advancement_slots(SEASON, EVENT),
    }

    empty = sorted(name for name, response in checks.items() if response.outcome == "empty")
    assert not empty, f"pathway returned nothing: {empty}"
