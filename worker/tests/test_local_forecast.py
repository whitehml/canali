"""Native results pass the same D1 publication guards as Worker computations."""

import asyncio
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from engine_version import ENGINE_VERSION
from forecast_store import export, save_input, upload_result
from snapshot import EventSnapshot, SeasonPrior
from test_season import Database, Request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_forecast", ROOT / "local_forecast.py")
local_forecast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_forecast)


@pytest.fixture
def completed(tmp_path):
    document = json.loads((ROOT / "fixtures/forecast.json").read_text())
    document.update(min_iterations=20, max_iterations=100, tolerance=0.3)
    snapshot = EventSnapshot.model_validate(document)
    output = tmp_path / "native.json"
    result = local_forecast.run_local(snapshot, output, progress=lambda _: None)
    assert local_forecast.run_local(snapshot, output, progress=lambda _: None) == result
    return snapshot, result


def test_native_publication_and_export(completed):
    snapshot, result = completed
    db = Database()
    asyncio.run(save_input(db, "2025-A", snapshot))
    payload = {"revision": 1, **result}
    report, status = asyncio.run(upload_result(db, Request("PUT", payload), "2025-A"))
    assert status == 200 and report["result"]["iterations"] == result["iterations"]
    packet, status = asyncio.run(export(db, Request("GET", None), "2025-A"))
    assert status == 200 and packet["forecast"] == report["result"]
    assert packet["snapshot"] == snapshot.model_dump(mode="json")
    assert asyncio.run(upload_result(db, Request("POST", payload), "2025-A"))[1] == 405
    assert asyncio.run(upload_result(db, Request("PUT", payload), "unknown"))[1] == 404


@pytest.mark.parametrize("change", ["revision", "engine_version", "snapshot_sha256", "season"])
def test_reject_stale_publication(completed, change):
    snapshot, result = completed
    db = Database()
    if change == "season":
        db.sql.execute(
            "INSERT INTO season_run(season, config, engine_version, scale_dirty) VALUES (2025, '{}', ?, 0)",
            (ENGINE_VERSION,),
        )
        snapshot.season_prior = SeasonPrior(season=2025, revision=1)
        from forecast_store import snapshot_hash

        result = {**result, "snapshot_sha256": snapshot_hash(snapshot)}
    asyncio.run(save_input(db, "2025-A", snapshot))
    payload = {"revision": 1, **result}
    if change == "season":
        db.sql.execute("UPDATE season_run SET revision = 2")
    else:
        payload[change] = 2 if change == "revision" else "obsolete"
    assert asyncio.run(upload_result(db, Request("PUT", payload), "2025-A"))[1] == 409
    assert db.sql.execute("SELECT COUNT(*) FROM forecast_state").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["iterations", "standard_error", "teams", "probability", "rank", "nan"])
def test_reject_malformed_results(completed, change):
    snapshot, result = completed
    db = Database()
    asyncio.run(save_input(db, "2025-A", snapshot))
    payload = deepcopy({"revision": 1, **result})
    team = next(iter(payload["teams"].values()))
    if change == "iterations":
        payload[change] = 1
    elif change == "standard_error":
        payload[change] = 0.4
    elif change == "teams":
        payload[change].pop(next(iter(payload[change])))
    elif change == "probability":
        team["p_win"] = 1.1
    elif change == "rank":
        team["mean_rank"] += 1
    else:
        team["rank_probabilities"][0] = float("nan")
    with pytest.raises(ValueError):
        asyncio.run(upload_result(db, Request("PUT", payload), "2025-A"))
    assert db.sql.execute("SELECT COUNT(*) FROM forecast_state").fetchone()[0] == 0


def test_client_uploads_revision_and_preserves_inputs(completed):
    snapshot, result = completed
    paths = []

    def respond(request):
        paths.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"engine_version": result["engine_version"]})
        body = json.loads(request.content)
        if request.url.path.endswith("/result"):
            assert body == {"revision": 7, **result}
            return httpx.Response(200, json={"status": "complete"})
        assert body == snapshot.model_dump(mode="json")
        return httpx.Response(200, json={"revision": 7})

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(respond)) as client:
        assert local_forecast.publish(client, "2025-A", snapshot, result)["status"] == "complete"
    assert paths == [
        ("GET", "/health"),
        ("PUT", "/forecasts/2025-A"),
        ("PUT", "/forecasts/2025-A/result"),
    ]


def test_engine_mismatch_does_not_replace_inputs(completed):
    snapshot, result = completed

    def respond(request):
        assert request.method == "GET" and request.url.path == "/health"
        return httpx.Response(200, json={"engine_version": "obsolete"})

    with (
        httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(ValueError, match="engine differs"),
    ):
        local_forecast.publish(client, "2025-A", snapshot, result)


def test_region_import_selects_only_rated_competitions():
    spec = importlib.util.spec_from_file_location("replay_season", ROOT / "replay_season.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    events = {
        "PA": {"regionCode": "USPA", "typeName": "Qualifier"},
        "NJ": {"regionCode": "USNJ", "typeName": "Qualifier"},
        "SCRIM": {"regionCode": "USPA", "typeName": "Scrimmage"},
    }
    assert replay.select_events(events, None, "USPA") == {"PA"}
    assert replay.select_events(events, "PA,NJ", None) == {"PA", "NJ"}
    with pytest.raises(ValueError):
        replay.select_events(events, "UNKNOWN", None)
    with pytest.raises(ValueError):
        replay.select_events(events, None, "MISSING")
