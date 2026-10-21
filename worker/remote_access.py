"""Native CLI credentials for the project's Access-protected backend."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

ACCESS_CREDENTIALS = Path(__file__).with_name(".dev.vars.access.json")
BACKEND_HOST = "canali-benchmark.mjhanson.workers.dev"


def request_headers(base_url):
    url = urlparse(base_url)
    local = url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    remote = url.scheme == "https" and url.hostname == BACKEND_HOST and url.port in (None, 443)
    if not (local or remote) or url.path not in ("", "/") or url.username or url.password or url.query or url.fragment:
        raise ValueError("use a loopback HTTP URL or the project's HTTPS benchmark Worker")
    if local:
        return {}
    token = os.environ.get("CANALI_BENCHMARK_TOKEN", "")
    if len(token) < 32:
        raise ValueError("set CANALI_BENCHMARK_TOKEN to the deployed secret (at least 32 characters)")
    keys = ("CANALI_ACCESS_CLIENT_ID", "CANALI_ACCESS_CLIENT_SECRET")
    credentials = {key: os.environ.get(key, "") for key in keys}
    if not any(credentials.values()) and ACCESS_CREDENTIALS.exists():
        credentials = json.loads(ACCESS_CREDENTIALS.read_text(encoding="utf-8"))
    if not all(isinstance(credentials.get(key), str) and credentials[key].strip() for key in keys):
        raise ValueError("set CANALI_ACCESS_CLIENT_ID and CANALI_ACCESS_CLIENT_SECRET for Cloudflare Access")
    return {
        "Authorization": f"Bearer {token}",
        "CF-Access-Client-Id": credentials[keys[0]],
        "CF-Access-Client-Secret": credentials[keys[1]],
    }
