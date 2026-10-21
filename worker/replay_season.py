"""Import a selected FIRST season corpus and resume bounded local D1 replay steps."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

from warehouse.rules.model import RulePack
from warehouse.tier import tier_for


def checked(response):
    if response.status_code not in (200, 202):
        raise SystemExit(f"Local Worker HTTP {response.status_code}: {response.text}")
    return response.json()


def select_events(events, codes, region):
    requested = (
        set(codes.split(","))
        if codes
        else {
            code
            for code, event in events.items()
            if event.get("regionCode") == region and tier_for(event.get("typeName")) is not None
        }
    )
    if not requested or requested - events.keys():
        raise ValueError("no matching rated competitions or unknown event codes")
    return requested


def main():
    from warehouse.ingest.client import FtcEventsClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season", type=int)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--events", help="Comma-separated event codes, including divisions as needed")
    selection.add_argument("--region", help="Import this region's rated competitions")
    parser.add_argument("--config", type=Path, help="Optional previous_norm and second_norm mappings")
    parser.add_argument("--rule-pack", type=Path, help="Enable component EPA with this season's rule-pack TOML")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    url = urlparse(args.base_url)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.path not in ("", "/"):
        parser.error("--base-url must be a local HTTP Worker URL")
    if not 2015 <= args.season <= 2100:
        parser.error("invalid season")
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    if args.rule_pack:
        pack = RulePack.from_file(args.rule_pack)
        if pack.season != args.season:
            parser.error("--rule-pack must match the requested season")
        config["rule_pack"] = pack.model_dump(mode="json")
    archive = Path(__file__).resolve().parents[1] / "var" / "worker-season" / str(args.season)
    archive.mkdir(parents=True, exist_ok=True)
    with FtcEventsClient() as client, httpx.Client(base_url=args.base_url.rstrip("/"), timeout=120) as worker:
        events = {event["code"]: event for event in client.season_events(args.season).data["events"]}
        try:
            requested = select_events(events, args.events, args.region)
        except ValueError as exc:
            parser.error(str(exc))
        checked(worker.put(f"/seasons/{args.season}", json=config))
        for code in sorted(requested):
            teams = client.event_teams(args.season, code)
            schedule = client.hybrid_schedule(args.season, code, "qual")
            if teams.status != 200 or schedule.status != 200:
                raise SystemExit(f"FIRST HTTP {teams.status}/{schedule.status} for {code}; replay not started")
            if args.region and (len(teams.data["teams"]) < 4 or not schedule.data["schedule"]):
                print(f"Skipping {code}: fewer than four teams or no published qualification schedule", flush=True)
                continue
            source = {"event": events[code], "teams": teams.data, "schedule": schedule.data}
            if config.get("rule_pack"):
                scores = client.scores(args.season, code, "qual")
                if scores.status != 200:
                    raise SystemExit(f"FIRST scores HTTP {scores.status} for {code}; replay not started")
                source["scores"] = scores.data
            print(json.dumps(checked(worker.put(f"/seasons/{args.season}/events/{code}", json=source))))
            (archive / f"{code}.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
        while True:
            report = checked(worker.post(f"/seasons/{args.season}/step"))
            print(json.dumps(report))
            if report["status"] == "complete":
                break


if __name__ == "__main__":
    main()
