"""Export a region's FIRST catalog and matching D1 forecasts into static assets."""

import argparse
import json
import math
import random
import re
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from prepare import OUTPUT, build_engine, catalog_path, publish_event, write_catalog, write_json
from static_publish import STATE, deploy_if_changed, prepare_site, publication_lock

sys.path.append(str(Path(__file__).resolve().parents[1] / "worker"))
from remote_access import request_headers

EVENT_TYPES = {
    "0": "Non-Advancement",
    "1": "League Meet",
    "2": "Qualifier",
    "3": "League Tournament",
    "4": "Championship",
    "6": "FIRST Championship",
    "7": "Super Qualifier",
    "10": "Off-Season",
    "17": "Premier",
}


def retry_delay(failures, interval_s, error):
    cap = max(1800, interval_s)
    ceiling = min(max(60, interval_s) * 2 ** min(failures - 1, 10), cap)
    delay = random.uniform(max(interval_s, ceiling * 0.8), ceiling)
    if isinstance(error, httpx.HTTPStatusError):
        retry_after = error.response.headers.get("Retry-After")
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            if math.isfinite(seconds):
                delay = max(delay, seconds)
    return delay


def previous_snapshot(output, url):
    if not url or re.fullmatch(r"/generated/event-[0-9a-f]{64}\.json", url) is None:
        return None
    path = output / Path(url).name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def metadata_for(event, season, label):
    return {
        "code": event["code"],
        "season": season,
        "name": event["name"].strip(),
        "date_start": event["dateStart"].split("T")[0],
        "date_end": (event.get("dateEnd") or event["dateStart"]).split("T")[0],
        "city": (event.get("city") or "").strip(),
        "venue": (event.get("venue") or "").strip(),
        "type": EVENT_TYPES[str(event["type"])],
        "label": label,
        "source_url": f"https://ftc-events.firstinspires.org/{season}/{event['code']}",
    }


def export_catalog(events, season, *, region, label, first, worker, engine, output=OUTPUT):
    from snapshot import EventSnapshot

    path = catalog_path(output, season, region)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    previous_events = {(row["season"], row["code"]): row for row in previous["events"]} if previous else {}
    catalog = []
    for event in sorted(events, key=lambda e: (e["dateStart"], e["code"])):
        if event.get("regionCode") != region or str(event["type"]) not in EVENT_TYPES:
            continue
        metadata = metadata_for(event, season, label)
        response = worker.get(f"/forecasts/{season}-{event['code']}/export")
        if response.status_code == 404:
            catalog.append({**metadata, "status": "unpublished", "forecast_url": None})
            continue
        response.raise_for_status()
        packet = response.json()
        if packet["event"] != f"{season}-{event['code']}":
            raise ValueError("export event differs from catalog")
        if packet["engine_version"] != engine["engine_version"]:
            raise ValueError("deployed engine differs from local sources; deploy matching sources before publication")
        if packet["status"] != "complete":
            catalog.append({**metadata, "status": packet["status"], "forecast_url": None})
            continue
        snapshot = EventSnapshot.model_validate(packet["snapshot"])
        if snapshot.rule_pack.season != season:
            raise ValueError("forecast season differs from catalog")
        roster = first.event_teams(season, event["code"])
        if roster.status != 200:
            raise ValueError(f"FIRST roster unavailable for {event['code']}")
        names = {
            str(t["teamNumber"]): t.get("nameShort") or t.get("nameFull") or f"Team {t['teamNumber']}"
            for t in roster.data["teams"]
        }
        if set(names) != {str(t) for t in snapshot.field.predicted}:
            raise ValueError(f"FIRST roster differs from forecast for {event['code']}")
        old = previous_events.get((season, event["code"]), {}).get("forecast_url")
        previous_document = previous_snapshot(output, old)
        catalog.append(
            publish_event(
                {**metadata, "teams": names},
                snapshot,
                packet["forecast"],
                engine,
                revision=packet["revision"],
                output=output,
                previous=previous_document,
                computed_at=packet.get("computed_at"),
            )
        )
    if not catalog:
        raise ValueError("no matching competition events; existing catalog was not replaced")
    document = {
        "season": season,
        "region": region,
        "region_name": "Pennsylvania" if region == "USPA" else region,
        "published_at": datetime.now(UTC).isoformat(),
        "events": catalog,
    }
    if previous and {k: v for k, v in previous.items() if k != "published_at"} == {
        k: v for k, v in document.items() if k != "published_at"
    }:
        document["published_at"] = previous["published_at"]
    write_catalog(document, output)
    return document


def run_publisher(args):
    from warehouse.ingest.client import FtcEventsClient

    headers = request_headers(args.base_url)
    with publication_lock(STATE / "publisher.lock"):
        retry_path = STATE / "retry.json"
        retry = json.loads(retry_path.read_text(encoding="utf-8")) if retry_path.exists() else {}
        output = prepare_site(STATE / "site") if args.deploy else OUTPUT
        engine = build_engine(output)
        with (
            FtcEventsClient() as first,
            httpx.Client(
                base_url=args.base_url.rstrip("/"), headers=headers, timeout=120, follow_redirects=False
            ) as worker,
        ):
            while True:
                remaining = retry.get("retry_at", 0) - time.time()
                if remaining > 0:
                    if not args.watch:
                        print(f"Publication deferred for {remaining:.0f}s after an earlier failure", flush=True)
                        return
                    time.sleep(remaining)
                try:
                    events = first.season_events(args.season)
                    if events.status != 200:
                        raise ValueError("FIRST event catalog unavailable")
                    total = 0
                    for region in args.region:
                        catalog = export_catalog(
                            events.data["events"],
                            args.season,
                            region=region,
                            label=args.label,
                            first=first,
                            worker=worker,
                            engine=engine,
                            output=output,
                        )
                        total += len(catalog["events"])
                    if args.deploy:
                        changed = deploy_if_changed(STATE / "site", STATE / "deployed.json")
                        print("Static publication deployed" if changed else "Static publication unchanged", flush=True)
                    else:
                        print(f"Exported {total} event listings", flush=True)
                    retry = {}
                    retry_path.unlink(missing_ok=True)
                except (httpx.HTTPError, ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                    failures = retry.get("failures", 0) + 1
                    delay = retry_delay(failures, args.interval_s, exc)
                    retry = {"failures": failures, "retry_at": time.time() + delay}
                    write_json(retry_path, retry)
                    if not args.watch:
                        raise
                    print(
                        f"Publication failed ({failures} consecutive); retrying after {delay:.0f}s: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if not args.watch:
                    return
                time.sleep(args.interval_s)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season", type=int)
    parser.add_argument("--region", action="append", help="Repeat for multiple regions; defaults to USPA")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--label", required=True, help="Data quality, model and historical replay assumptions")
    parser.add_argument(
        "--deploy", action="store_true", help="Publish changed data to the separate canali-data static deployment"
    )
    parser.add_argument("--watch", action="store_true", help="Keep exporting and retry failed publication cycles")
    parser.add_argument("--interval-s", type=int, default=60, help="Seconds between completed publication cycles")
    args = parser.parse_args()
    args.region = list(dict.fromkeys(args.region or ["USPA"]))
    try:
        request_headers(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))
    if not 2015 <= args.season <= 2100 or any(not r.isascii() or not r.isalnum() for r in args.region):
        parser.error("invalid season or region")
    if args.interval_s < 1:
        parser.error("--interval-s must be positive")
    with suppress(KeyboardInterrupt):
        run_publisher(args)


if __name__ == "__main__":
    main()
