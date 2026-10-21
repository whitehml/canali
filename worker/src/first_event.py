"""Prepare one event's qualification totals from FIRST's teams and hybrid schedule."""

import json
import re

from compute import integer, number, validate

IMPORT_INPUT = (
    "INSERT INTO event_input(event_code, revision, document, source_document) VALUES (?, 1, ?, ?) "
    "ON CONFLICT(event_code) DO UPDATE SET "
    "revision = revision + CASE WHEN document != excluded.document "
    "OR source_document IS NOT excluded.source_document THEN 1 ELSE 0 END, "
    "document = excluded.document, source_document = excluded.source_document RETURNING revision"
)


def prepare(source: dict, code: str) -> dict:
    """Keep official scores separate from explicitly supplied priors and calibration."""
    season = integer(source["season"], "season", minimum=2015)
    event = source["event_code"]
    if not isinstance(event, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,50}", event) is None:
        raise ValueError("invalid FIRST event code")
    if code != f"{season}-{event}":
        raise ValueError("import key must be season-event_code")
    roster = [integer(t["teamNumber"], "team number", minimum=1) for t in source["teams"]["teams"]]
    parameters = source["parameters"]
    if len(set(roster)) != len(roster) or set(roster) != {t["number"] for t in parameters["teams"]}:
        raise ValueError("priors must match the official event roster exactly")
    document = {**parameters, "matches": qualification_matches(roster, source["schedule"])}
    validate(document)
    return document


def qualification_matches(roster: list[int], schedule: dict) -> list[dict]:
    """Convert official stations and scores without inventing rating priors."""
    matches = []
    for row in sorted(schedule["schedule"], key=lambda m: m["matchNumber"]):
        if row["tournamentLevel"] not in ("QUALIFICATION", "QUAL", "QUALS"):
            raise ValueError("import requires a qualification-only schedule")
        slots = row["teams"]
        if not isinstance(slots, list) or any(not isinstance(t, dict) for t in slots):
            raise ValueError("match teams must be station objects")
        if any(not isinstance(t.get("noShow", False), bool) for t in slots):
            raise ValueError("noShow must be boolean")
        stations = [t["station"] for t in slots]
        if len(stations) != 4 or set(stations) != {"Red1", "Red2", "Blue1", "Blue2"}:
            raise ValueError("each match needs four distinct FTC stations")
        participants = [integer(t["teamNumber"], "scheduled team", minimum=1) for t in slots]
        if len(set(participants)) != 4 or not set(participants) <= set(roster):
            raise ValueError("scheduled teams must be distinct and on the official roster")
        match = {"ordinal": row["matchNumber"]}
        for side in ("red", "blue"):
            label = side.title()
            # Surrogates contribute to the score; absent teams receive no rating update.
            match[side] = [
                t["teamNumber"]
                for t in sorted(slots, key=lambda t: t["station"])
                if t["station"].startswith(label) and not t.get("noShow", False)
            ]
            final = row[f"score{label}Final"]
            # FIRST reports fouls committed: the opponent's fouls increase this final score.
            opponent = "Blue" if side == "red" else "Red"
            foul = row.get(f"score{opponent}Foul")
            match[f"{side}_score"] = (
                None if final is None else number(final, "final score") - number(foul, "foul score")
            )
        matches.append(match)
    return matches


async def handle(db, request, code: str) -> tuple[dict, int]:
    if request.method == "GET":
        row = (
            await db.prepare(
                "SELECT revision, source_document FROM event_input WHERE event_code = ? AND source_document IS NOT NULL"
            )
            .bind(code)
            .first()
        )
        if row is None:
            return {"error": "no imported source for this event"}, 404
        return {"event": code, "revision": row.revision, "source": json.loads(row.source_document)}, 200
    if request.method != "PUT":
        return {"error": "method not allowed"}, 405
    body = await request.text()
    if len(body.encode()) > 512_000:
        return {"error": "input exceeds 512000 bytes"}, 413
    source = json.loads(body)
    if not isinstance(source, dict) or set(source) != {"season", "event_code", "teams", "schedule", "parameters"}:
        raise ValueError("import needs season, event_code, teams, schedule and parameters")
    document = prepare(source, code)
    row = (
        await db.prepare(IMPORT_INPUT)
        .bind(
            code,
            json.dumps(document, sort_keys=True, allow_nan=False),
            json.dumps(source, sort_keys=True, allow_nan=False),
        )
        .first()
    )
    return {
        "event": code,
        "revision": row.revision,
        "teams": len(document["teams"]),
        "matches": len(document["matches"]),
    }, 200
