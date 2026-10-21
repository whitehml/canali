"""Static publication retries failed deploys without coupling visitor traffic to the backend."""

import json
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from static_publish import deploy_if_changed, prepare_site, publication_lock


def test_deployment_skips_unchanged_data_and_retries_without_losing_last_success(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "generated").mkdir()
    (site / "generated/index.json").write_text('{"regions":[]}')
    state = tmp_path / "deployed.json"
    calls = []

    def deploy(command, **kwargs):
        calls.append(command)
        assert kwargs["check"]
        assert "--assets" in command
        assert Path(command[command.index("--config") + 1]).name == "data.wrangler.toml"

    assert deploy_if_changed(site, state, run=deploy)
    assert not deploy_if_changed(site, state, run=deploy)
    assert len(calls) == 1
    previous = state.read_bytes()
    (site / "catalog.json").write_text('{"revision":2}')

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "wrangler")

    with pytest.raises(subprocess.CalledProcessError):
        deploy_if_changed(site, state, run=fail)
    assert state.read_bytes() == previous
    assert deploy_if_changed(site, state, run=deploy)
    assert not deploy_if_changed(site, state, run=deploy)
    assert len(calls) == 2


def test_restart_retains_data_and_serves_cross_origin_without_compiled_client(tmp_path):
    site = tmp_path / "site"
    output = prepare_site(site)
    (output / "index.json").write_text('{"regions":[]}')
    (output / "old-event.json").write_text("frozen snapshot")
    assert prepare_site(site) == output
    assert not (site / "index.html").exists()
    assert json.loads((output / "index.json").read_text()) == {"regions": []}
    assert (output / "old-event.json").read_text() == "frozen snapshot"
    headers = (site / "_headers").read_text()
    assert "Access-Control-Allow-Origin: *" in headers
    assert "/generated/catalogs/*\n  Cache-Control: no-cache" in headers
    assert "immutable" in headers


def test_only_one_publisher_can_run_and_lock_releases_after_failure(tmp_path):
    lock = tmp_path / "publisher.lock"
    with pytest.raises(ValueError, match="stop"), publication_lock(lock):
        with pytest.raises(RuntimeError, match="already running"), publication_lock(lock):
            pytest.fail("second publisher acquired the lock")
        raise ValueError("stop")
    with publication_lock(lock):
        pass


def test_public_deployment_contains_no_request_handler_or_backend_bindings():
    root = Path(__file__).parents[1]
    for filename in ("wrangler.toml", "data.wrangler.toml"):
        config = tomllib.loads((root / filename).read_text())
        assert not {"main", "services", "d1_databases", "workflows"} & config.keys()
        assert "run_worker_first" not in config["assets"]
        if filename == "data.wrangler.toml":
            assert config["name"] == "canali-data"
            assert config["assets"]["directory"] != "./dist"
            assert config["assets"]["not_found_handling"] == "none"


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    import publish

    first = SimpleNamespace(season_events=lambda *_: SimpleNamespace(status=200, data={"events": []}))
    monkeypatch.setitem(
        sys.modules, "warehouse.ingest.client", SimpleNamespace(FtcEventsClient=lambda: nullcontext(first))
    )
    monkeypatch.setattr(publish.httpx, "Client", lambda **_: nullcontext(None))
    monkeypatch.setattr(publish, "STATE", tmp_path)
    monkeypatch.setattr(publish, "prepare_site", lambda *_: tmp_path / "site/generated")
    monkeypatch.setattr(publish, "build_engine", lambda *_: {})
    args = SimpleNamespace(
        base_url="http://127.0.0.1:8787",
        season=2025,
        region=["USPA", "USNJ"],
        label="Test",
        deploy=True,
        watch=True,
        interval_s=60,
    )
    return publish, args


def test_watcher_backs_off_source_and_deployment_failures_then_resets(publisher, tmp_path, monkeypatch):
    publish, args = publisher
    attempts, deploys = [], []
    delays, now = [], [1000]
    monkeypatch.setattr(publish.time, "time", lambda: now[0])
    monkeypatch.setattr(publish.random, "uniform", lambda low, high: high)

    def export(*args, **kwargs):
        attempts.append("export")
        if len(attempts) == 1:
            raise ValueError("source unavailable")
        return {"events": []}

    def deploy(*args):
        deploys.append("deploy")
        if len(deploys) == 1:
            raise subprocess.CalledProcessError(1, "wrangler")
        return True

    def sleep(delay):
        delays.append(delay)
        now[0] += delay
        if len(deploys) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(publish, "export_catalog", export)
    monkeypatch.setattr(publish, "deploy_if_changed", deploy)
    monkeypatch.setattr(publish.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        publish.run_publisher(args)
    assert len(attempts) == 5
    assert len(deploys) == 2
    assert delays == [60, 120, 60]
    assert not (tmp_path / "retry.json").exists()


def test_backoff_caps_jitters_and_honors_server_retry_after(monkeypatch):
    import httpx
    import publish

    monkeypatch.setattr(publish.random, "uniform", lambda low, high: high)
    error = RuntimeError("unavailable")
    assert [publish.retry_delay(n, 60, error) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1800, 1800, 1800]
    assert publish.retry_delay(10000, 3600, error) == 3600
    monkeypatch.setattr(publish.random, "uniform", lambda low, high: low)
    assert publish.retry_delay(2, 60, error) == 96
    assert publish.retry_delay(20, 60, error) == 1440
    assert publish.retry_delay(1, 60, error) == 60
    monkeypatch.setattr(publish.time, "time", lambda: 0)
    request = httpx.Request("GET", "https://backend.example/export")
    for header, expected in [
        ("7200", 7200),
        ("Thu, 01 Jan 1970 02:00:00 GMT", 7200),
        ("bad", 60),
        ("NaN", 60),
        ("-1", 60),
    ]:
        response = httpx.Response(429, headers={"Retry-After": header}, request=request)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()
        assert publish.retry_delay(1, 60, caught.value) == expected


def test_one_shot_failure_persists_cooldown_across_invocations(publisher, tmp_path, monkeypatch):
    publish, args = publisher
    args.watch = False
    now = [1000]
    monkeypatch.setattr(publish.time, "time", lambda: now[0])
    monkeypatch.setattr(publish.random, "uniform", lambda low, high: high)
    attempts = []

    def fail(*args, **kwargs):
        attempts.append("export")
        raise ValueError("engine mismatch")

    monkeypatch.setattr(publish, "export_catalog", fail)
    with pytest.raises(ValueError, match="engine mismatch"):
        publish.run_publisher(args)
    state = json.loads((tmp_path / "retry.json").read_text())
    assert state == {"failures": 1, "retry_at": 1060}
    now[0] = 1059
    publish.run_publisher(args)
    assert attempts == ["export"]
    now[0] = 1060
    with pytest.raises(ValueError, match="engine mismatch"):
        publish.run_publisher(args)
    assert json.loads((tmp_path / "retry.json").read_text()) == {"failures": 2, "retry_at": 1180}

    monkeypatch.setattr(publish, "export_catalog", lambda *_, **__: {"events": []})
    monkeypatch.setattr(publish, "deploy_if_changed", lambda *_: False)
    now[0] = 1180
    publish.run_publisher(args)
    assert not (tmp_path / "retry.json").exists()
