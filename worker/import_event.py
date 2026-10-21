"""Fetch FIRST data with the native warehouse client and submit it to the local Worker."""

import argparse
import json
import math
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from warehouse.ingest.client import FtcEventsClient

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season", type=int)
    parser.add_argument("event_code")
    parser.add_argument(
        "--parameters", type=Path, required=True, help="Prepared event JSON with priors and calibration"
    )
    parser.add_argument(
        "--initial-epa", type=float, help="Explicitly replace priors with a uniform rating and zero history"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--season-priors", action="store_true", help="Use the local season replay's pre-event ratings")
    args = parser.parse_args()
    url = urlparse(args.base_url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.path not in ("", "/"):
        parser.error("--base-url must be a local HTTP Worker URL")
    if not 2015 <= args.season <= 2100 or re.fullmatch(r"[A-Za-z0-9_-]{1,50}", args.event_code) is None:
        parser.error("invalid season or event code")
    if args.initial_epa is not None and (not math.isfinite(args.initial_epa) or args.initial_epa < 0):
        parser.error("--initial-epa must be finite and nonnegative")
    if args.season_priors and args.initial_epa is not None:
        parser.error("--season-priors cannot be combined with --initial-epa")
    parameters = json.loads(args.parameters.read_text(encoding="utf-8"))
    parameters.pop("matches", None)
    if args.season_priors:
        with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as worker:
            response = worker.get(f"/seasons/{args.season}/priors/{args.event_code}")
            if response.status_code != 200:
                raise SystemExit(f"Season priors unavailable (HTTP {response.status_code}): {response.text}")
            parameters.update(response.json())
    # Reuse authentication, rate limiting, retries and pagination without a Postgres connection.
    with FtcEventsClient() as client:
        teams = client.event_teams(args.season, args.event_code)
        schedule = client.hybrid_schedule(args.season, args.event_code, "qual")
    if teams.status != 200 or schedule.status != 200:
        parser.error(f"FIRST returned HTTP {teams.status}/{schedule.status}; nothing imported")
    if args.initial_epa is not None:
        parameters.pop("season_prior", None)
        parameters["teams"] = [
            {"number": team["teamNumber"], "epa": args.initial_epa, "played": 0} for team in teams.data["teams"]
        ]
    source = {
        "season": args.season,
        "event_code": args.event_code,
        "teams": teams.data,
        "schedule": schedule.data,
        "parameters": parameters,
    }
    key = f"{args.season}-{args.event_code}"
    archive = ROOT / "var" / "worker-import" / key / "source.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(source, indent=2, allow_nan=False), encoding="utf-8")
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as worker:
        response = worker.put(f"/imports/{key}", json=source)
        if response.status_code != 200:
            raise SystemExit(f"Import failed (HTTP {response.status_code}): {response.text}")
        print(json.dumps(response.json()))
        response = worker.post(f"/events/{key}/run")
        if response.status_code != 200:
            raise SystemExit(f"Computation failed (HTTP {response.status_code}): {response.text}")
        result = response.json()
        (archive.parent / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Computed {result['result']['scored_matches']} qualification matches; saved in {archive.parent}")


if __name__ == "__main__":
    main()
