from types import SimpleNamespace

import pytest
from access import allowed


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_local_access_requires_loopback(host):
    env = SimpleNamespace(LOCAL_PROTOTYPE="true")
    assert allowed(env, SimpleNamespace(url=f"http://{host}:8787/health"))
    assert not allowed(env, SimpleNamespace(url="https://canali.workers.dev/health"))


def test_hosted_access_fails_closed_for_every_route_and_method():
    token = "a" * 32
    env = SimpleNamespace(LOCAL_PROTOTYPE="false", BENCHMARK="true", BENCHMARK_TOKEN=token)
    for path in ("/health", "/forecasts/test/step", "/seasons/2025", "/imports/test"):
        for method in ("GET", "PUT", "POST"):
            request = SimpleNamespace(url=f"https://canali.workers.dev{path}", method=method, headers={})
            assert not allowed(env, request)
            request.headers = {"Authorization": "Bearer wrong"}
            assert not allowed(env, request)
            request.headers = {"Authorization": f"Bearer {token}"}
            assert allowed(env, request)
            request.headers = {"Authorization": "Bearer é"}
            assert not allowed(env, request)
    for broken in (
        SimpleNamespace(),
        SimpleNamespace(BENCHMARK="true", BENCHMARK_TOKEN="short"),
        SimpleNamespace(BENCHMARK="false", BENCHMARK_TOKEN=token),
    ):
        assert not allowed(broken, request)
