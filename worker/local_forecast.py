"""Compute a frozen event forecast natively, then publish its result to authenticated D1."""

import argparse
import json
import re
import runpy
import sys
from pathlib import Path

import httpx


def run_local(snapshot, output, *, progress=print):
    """Persist bounded batches; resume only when both inputs and engine match."""
    from engine_version import ENGINE_VERSION
    from forecast import advance
    from forecast_store import snapshot_hash

    identity = {"engine_version": ENGINE_VERSION, "snapshot_sha256": snapshot_hash(snapshot)}
    saved = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    if any(saved.get(key) != value for key, value in identity.items()):
        saved = {}
    checkpoint = saved.get("checkpoint")
    report = saved.get("report", {"status": "running"})
    output.parent.mkdir(parents=True, exist_ok=True)
    while report["status"] == "running":
        checkpoint, report = advance(snapshot, checkpoint)
        saved = {**identity, "checkpoint": checkpoint, "report": report}
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, allow_nan=False), encoding="utf-8")
        temporary.replace(output)
        if report["iterations"] % 1000 < 32 or report["status"] != "running":
            progress(f"{report['status']}: {report['iterations']} iterations; SE {report['standard_error']:.5f}")
    if report["status"] != "complete":
        raise ValueError("iteration budget exhausted before convergence; no result uploaded")
    return json.loads(json.dumps({**identity, **report["result"]}))


def publish(client, event, snapshot, result):
    """Save frozen inputs and guard result publication against concurrent corrections."""
    response = client.get("/health")
    response.raise_for_status()
    if response.json().get("engine_version") != result["engine_version"]:
        raise ValueError("backend engine differs from local result; deploy matching sources before uploading")
    response = client.put(f"/forecasts/{event}", json=snapshot.model_dump(mode="json"))
    response.raise_for_status()
    revision = response.json()["revision"]
    response = client.put(f"/forecasts/{event}/result", json={"revision": revision, **result})
    response.raise_for_status()
    return response.json()


def main():
    from remote_access import request_headers

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--event", required=True, help="Season-qualified key, e.g. 2025-USPAHAQ1")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, help="Resumable native checkpoint; defaults beside the snapshot")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.event) is None:
        parser.error("invalid event key")
    try:
        headers = request_headers(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    root = Path(__file__).resolve().parent
    runpy.run_path(str(root / "prepare.py"))
    sys.path.insert(0, str(root / "src"))
    from snapshot import EventSnapshot

    snapshot = EventSnapshot.model_validate_json(args.snapshot.read_text(encoding="utf-8"))
    # Local D1 revision numbers have no meaning in a different database; publish a frozen snapshot.
    snapshot = snapshot.model_copy(update={"season_prior": None})
    if not args.event.startswith(f"{snapshot.rule_pack.season}-"):
        parser.error("event key must start with the snapshot season")
    try:
        result = run_local(snapshot, args.output or args.snapshot.with_name("native-result.json"))
        with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=120) as client:
            report = publish(client, args.event, snapshot, result)
    except (ValueError, httpx.HTTPError) as exc:
        raise SystemExit(f"Local forecast stopped: {exc}") from exc
    print(f"Published {args.event}, revision {report['revision']}: {report['iterations']} iterations")


if __name__ == "__main__":
    main()
