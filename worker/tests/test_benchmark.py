import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("benchmark", Path(__file__).resolve().parents[1] / "benchmark.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_benchmark_records_progress_and_stops_without_duplicate_work():
    steps = 0

    def serve(request):
        nonlocal steps
        if request.method == "PUT":
            assert json.loads(request.content) == {"seed": 42}
            return httpx.Response(200, json={"revision": 1})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        steps += 1
        return httpx.Response(
            202 if steps == 1 else 200,
            json={"iterations": steps * 32, "status": "running" if steps == 1 else "complete", "result": {}},
            headers={"cf-ray": "test-ray"},
        )

    report = {"requests": [], "progress": []}
    with httpx.Client(base_url="https://example.workers.dev", transport=httpx.MockTransport(serve)) as client:
        module.benchmark(client, {"seed": 42}, "test", 2, report)
    assert len(report["requests"]) == 4
    assert report["forecast"]["iterations"] == 64
    assert report["requests"][-1]["cf_ray"] == "test-ray"
    assert "result" not in report["progress"][0]


def test_benchmark_bounds_requests_and_records_http_failures():
    report = {"requests": [], "progress": []}

    def serve(_request):
        return httpx.Response(403, json={"error": "access denied"})

    with (
        httpx.Client(base_url="https://example.workers.dev", transport=httpx.MockTransport(serve)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        module.benchmark(client, {}, "test", 2, report)
    assert len(report["requests"]) == 1
    assert report["requests"][0]["status"] == 403

    report = {"requests": [], "progress": []}

    def running(_request):
        return httpx.Response(200, json={"iterations": 32, "status": "running"})

    with (
        httpx.Client(base_url="https://example.workers.dev", transport=httpx.MockTransport(running)) as client,
        pytest.raises(RuntimeError, match="budget reached"),
    ):
        module.benchmark(client, {}, "test", 1, report)
    assert len(report["requests"]) == 3
