"""Assemble a local D1 forecast from imported FIRST data and explicit models and history."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx


def checked(response):
    if response.status_code not in (200, 202):
        raise SystemExit(f"Local Worker HTTP {response.status_code}: {response.text}")
    return response.json()


def stage_options(first, season, code, *, historical=False, after_qualifications=False):
    alliances = first.alliances(season, code)
    if alliances.status != 200:
        raise SystemExit(f"FIRST alliances HTTP {alliances.status}; stage could not be verified")
    body = {"selection_published": bool(alliances.data.get("alliances"))}
    if body["selection_published"] and not historical and not after_qualifications:
        playoff = first.hybrid_schedule(season, code, "playoff")
        scores = first.scores(season, code, "playoff")
        if playoff.status != 200 or scores.status != 200:
            raise SystemExit(f"FIRST playoff HTTP {playoff.status}/{scores.status}")
        body.update(alliances=alliances.data, playoff_schedule=playoff.data, playoff_scores=scores.data)
    if after_qualifications:
        schedule = first.hybrid_schedule(season, code, "qual")
        if schedule.status != 200:
            raise SystemExit(f"FIRST schedule HTTP {schedule.status}")
        matches = schedule.data["schedule"]
        if not matches or any(row.get("scoreRedFinal") is None or row.get("scoreBlueFinal") is None for row in matches):
            raise SystemExit("a complete scored qualification schedule is required")
        body["through"] = len(matches)
    return body


def main():
    from warehouse.ingest.client import FtcEventsClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season", type=int)
    parser.add_argument("event_code")
    parser.add_argument(
        "--models", type=Path, required=True, help="Models JSON, or a prepared snapshot containing models"
    )
    parser.add_argument(
        "--context", type=Path, required=True, help="Prior alliance/award history and advancement assumptions"
    )
    parser.add_argument(
        "--through", type=int, help="Use only this many scored qualification matches, in match-number order"
    )
    parser.add_argument(
        "--after-qualifications",
        action="store_true",
        help="Replay after all qualifications, ignoring later alliance selection and playoffs",
    )
    parser.add_argument("--run", action="store_true", help="Resume Monte Carlo steps until convergence or exhaustion")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    if args.after_qualifications and args.through is not None:
        parser.error("--after-qualifications cannot be combined with --through")
    url = urlparse(args.base_url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.path not in ("", "/"):
        parser.error("--base-url must be a local HTTP Worker URL")
    if not 2015 <= args.season <= 2100 or re.fullmatch(r"[A-Za-z0-9_-]{1,50}", args.event_code) is None:
        parser.error("invalid season or event code")
    models = json.loads(args.models.read_text(encoding="utf-8"))
    body = {
        "models": models.get("models", models),
        "context": json.loads(args.context.read_text(encoding="utf-8")),
        "through": args.through,
    }
    for key in ("seed", "tolerance", "min_iterations", "max_iterations", "matches_per_team"):
        if key in models:
            body[key] = models[key]
    with FtcEventsClient() as first:
        body.update(
            stage_options(
                first,
                args.season,
                args.event_code,
                historical=args.through is not None,
                after_qualifications=args.after_qualifications,
            )
        )
    key = f"{args.season}-{args.event_code}"
    archive = Path(__file__).resolve().parents[1] / "var" / "worker-forecast" / key
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as worker:
        report = checked(worker.post(f"/seasons/{args.season}/forecasts/{args.event_code}", json=body))
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "snapshot.json").write_text(json.dumps(report.pop("snapshot"), indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
        print(f"Snapshot saved in {archive}", flush=True)
        if args.run:
            while True:
                report = checked(worker.post(f"/forecasts/{key}/step"))
                print(json.dumps({k: v for k, v in report.items() if k != "result"}), flush=True)
                if report["status"] != "running":
                    break
            (archive / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
