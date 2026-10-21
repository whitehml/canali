"""Keep the hosted benchmark private and the unauthenticated prototype on loopback."""

from hmac import compare_digest
from urllib.parse import urlparse


def allowed(env, request) -> bool:
    if getattr(env, "LOCAL_PROTOTYPE", None) == "true":
        return urlparse(request.url).hostname in ("127.0.0.1", "localhost", "::1")
    token = getattr(env, "BENCHMARK_TOKEN", None)
    if getattr(env, "BENCHMARK", None) != "true" or not isinstance(token, str) or len(token) < 32:
        return False
    supplied = request.headers.get("Authorization") or ""
    return compare_digest(supplied.encode(), f"Bearer {token}".encode())
