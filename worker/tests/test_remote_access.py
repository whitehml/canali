import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remote_access


def test_credentials_are_required_only_for_the_project_backend(monkeypatch, tmp_path):
    stored = tmp_path / "credentials.json"
    monkeypatch.setattr(remote_access, "ACCESS_CREDENTIALS", stored)
    for name in ("CANALI_ACCESS_CLIENT_ID", "CANALI_ACCESS_CLIENT_SECRET", "CANALI_BENCHMARK_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    remote = "https://canali-benchmark.mjhanson.workers.dev"
    with pytest.raises(ValueError, match="BENCHMARK_TOKEN"):
        remote_access.request_headers(remote)
    monkeypatch.setenv("CANALI_BENCHMARK_TOKEN", "a" * 32)
    with pytest.raises(ValueError, match="ACCESS_CLIENT_ID"):
        remote_access.request_headers(remote)
    stored.write_text(json.dumps({"CANALI_ACCESS_CLIENT_ID": "id", "CANALI_ACCESS_CLIENT_SECRET": "secret"}))
    expected = {"Authorization": "Bearer " + "a" * 32, "CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "secret"}
    assert remote_access.request_headers(remote) == expected
    monkeypatch.setenv("CANALI_ACCESS_CLIENT_ID", "different")
    with pytest.raises(ValueError, match="ACCESS_CLIENT_ID"):
        remote_access.request_headers(remote)
    monkeypatch.setenv("CANALI_ACCESS_CLIENT_SECRET", "different-secret")
    assert remote_access.request_headers(remote)["CF-Access-Client-Secret"] == "different-secret"
    assert remote_access.request_headers("http://127.0.0.1:8787") == {}
    for url in (
        "https://other.workers.dev",
        remote + "/health",
        remote + "?secret=x",
        remote + ":444",
        "https://user@canali-benchmark.mjhanson.workers.dev",
    ):
        with pytest.raises(ValueError):
            remote_access.request_headers(url)
