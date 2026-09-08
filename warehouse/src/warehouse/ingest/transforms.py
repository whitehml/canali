"""Payload to row.

Every function here is pure: a supplier's JSON in, dicts shaped like the fact tables out. Nothing touches the
database, so a stored payload replays through the same code that first read it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from warehouse.schema.types import ALLIANCE_PICK_ACTIONS, ALLIANCE_ROLES, MATCH_LEVELS

log = structlog.get_logger(__name__)

Json = Mapping[str, Any]


# ------------------------------------------------------------------------------------------------------------ time


def parse_local(value: str | None) -> datetime | None:
    """Read an FTC Events timestamp as a naive venue-local wall clock.

    Any offset the string carries is discarded, since the conversion is driven by ``event.timezone``.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().removesuffix("Z"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def resolve_timezone(api_timezone: str | None, default: str) -> tuple[str, bool]:
    """Return the event's timezone and whether it was assumed.

    A missing or unrecognised zone falls back to a fixed constant.
    """
    if not api_timezone:
        return default, True
    try:
        ZoneInfo(api_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return default, True
    return api_timezone, False


def to_utc(local: datetime | None, timezone: str) -> datetime | None:
    if local is None:
        return None
    return local.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)


def timestamp_pair(value: str | None, timezone: str) -> tuple[datetime | None, datetime | None]:
    """Return the UTC instant and the local wall clock. Both are stored."""
    local = parse_local(value)
    return to_utc(local, timezone), local


# ---------------------------------------------------------------------------------------------------------- events


def _blank_to_none(value: Any) -> Any:
    """The API answers with an empty string rather than a null on several optional codes."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _collection(payload: Json | None, *keys: str) -> list[Json]:
    """The first list the payload holds under any of ``keys``. Capitalisation varies by endpoint."""
    if payload is None:
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def is_excluded_event(row: Json) -> bool:
    return bool(row.get("remote")) or bool(row.get("hybrid"))


def event_rows(season: int, payload: Json | None, default_timezone: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _collection(payload, "events", "Events"):
        if is_excluded_event(row):
            continue
        date_start = parse_local(row.get("dateStart"))
        if date_start is None:
            log.warning("events.no_date_start", code=row.get("code"))
            continue
        date_end = parse_local(row.get("dateEnd"))
        timezone, assumed = resolve_timezone(row.get("timezone"), default_timezone)
        out.append(
            {
                "event_id": uuid.UUID(str(row["eventId"])),
                "season": season,
                "code": row["code"],
                "name": row.get("name") or row["code"],
                "type": row.get("typeName") or row.get("type"),
                "division_code": _blank_to_none(row.get("divisionCode")),
                "region_code": _blank_to_none(row.get("regionCode")),
                "league_code": _blank_to_none(row.get("leagueCode")),
                "field_count": row.get("fieldCount"),
                "date_start": date_start.date(),
                "date_end": date_end.date() if date_end else None,
                "venue": row.get("venue"),
                "city": row.get("city"),
                "state_prov": row.get("stateprov") or row.get("stateProv"),
                "country": row.get("country"),
                "timezone": timezone,
                "timezone_assumed": assumed,
            }
        )
    return out


# ----------------------------------------------------------------------------------------------------------- teams


def team_rows(payload: Json | None) -> list[dict[str, Any]]:
    """Identity only. Anything that varies by season belongs to team_season."""
    return [
        {"team_number": row["teamNumber"], "rookie_year": row.get("rookieYear") or None}
        for row in _collection(payload, "teams", "Teams")
    ]


def team_season_rows(season: int, payload: Json | None) -> list[dict[str, Any]]:
    return [
        {
            "season": season,
            "team_number": row["teamNumber"],
            "name_full": row.get("nameFull"),
            "name_short": row.get("nameShort"),
            "home_state": row.get("stateProv") or row.get("stateprov"),
            "home_country": row.get("country"),
            "home_region": _blank_to_none(row.get("homeRegion")),
            "city": row.get("city"),
        }
        for row in _collection(payload, "teams", "Teams")
    ]


def event_team_numbers(payload: Json | None) -> list[int]:
    return [row["teamNumber"] for row in _collection(payload, "teams", "Teams")]


# --------------------------------------------------------------------------------------------------------- matches

_LEVEL_ALIASES: dict[str, str] = {
    "QUAL": "QUALIFICATION",
    "QUALS": "QUALIFICATION",
    "PLAYOFF": "PLAYOFF",
    "SEMI": "SEMIFINAL",
    "SEMIS": "SEMIFINAL",
    "FINALS": "FINAL",
    "NONE": "OTHER",
}


def normalize_level(value: str | None) -> str:
    """Map a spelling of the tournament level onto the enum."""
    if not value:
        return "OTHER"
    upper = value.strip().upper().replace(" ", "").replace("-", "")
    upper = _LEVEL_ALIASES.get(upper, upper)
    return upper if upper in MATCH_LEVELS else "OTHER"


def _station_alliance(station: str) -> str:
    side = station.strip().upper().rstrip("0123456789")
    return side if side in ("RED", "BLUE") else "RED"


def hybrid_match_rows(event_id: uuid.UUID, timezone: str, payload: Json | None) -> list[dict[str, Any]]:
    """Rows from ``/schedule/{eventCode}/{level}/hybrid``, which carries the schedule and the result together."""
    out: list[dict[str, Any]] = []
    for row in _collection(payload, "schedule", "Schedule", "matches", "Matches"):
        start_utc, start_local = timestamp_pair(row.get("startTime"), timezone)
        actual_utc, actual_local = timestamp_pair(row.get("actualStartTime"), timezone)
        post_utc, post_local = timestamp_pair(row.get("postResultTime"), timezone)
        modified_utc, _ = timestamp_pair(row.get("modifiedOn"), timezone)

        teams = [
            {
                "station": str(slot["station"]),
                "alliance": _station_alliance(str(slot["station"])),
                "team_number": slot["teamNumber"],
                "surrogate": bool(slot.get("surrogate")),
                "no_show": bool(slot.get("noShow")),
                "dq": bool(slot.get("dq")),
                "on_field": bool(slot.get("onField", True)),
            }
            for slot in row.get("teams") or []
            if slot.get("teamNumber") is not None
        ]

        out.append(
            {
                "event_id": event_id,
                "level": normalize_level(row.get("tournamentLevel")),
                "series": int(row.get("series") or 0),
                "match_number": int(row["matchNumber"]),
                "description": row.get("description"),
                "start_time_utc": start_utc,
                "start_time_local": start_local,
                "actual_start_time_utc": actual_utc,
                "actual_start_time_local": actual_local,
                "post_result_time_utc": post_utc,
                "post_result_time_local": post_local,
                "modified_on_utc": modified_utc,
                "score_red_final": row.get("scoreRedFinal"),
                "score_blue_final": row.get("scoreBlueFinal"),
                "score_red_auto": row.get("scoreRedAuto"),
                "score_blue_auto": row.get("scoreBlueAuto"),
                "score_red_foul": row.get("scoreRedFoul"),
                "score_blue_foul": row.get("scoreBlueFoul"),
                "teams": teams,
            }
        )
    return out


RESULT_FIELDS: tuple[str, ...] = (
    "score_red_final",
    "score_blue_final",
    "score_red_auto",
    "score_blue_auto",
    "score_red_foul",
    "score_blue_foul",
)


def results_differ(stored: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    """Whether an occupied slot came back with a different result."""
    if any(stored.get(field) != incoming.get(field) for field in RESULT_FIELDS):
        return True
    return _slot_key(stored.get("teams") or []) != _slot_key(incoming.get("teams") or [])


def _slot_key(teams: Iterable[Mapping[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted((t["station"], t["team_number"], t["surrogate"], t["no_show"], t["dq"]) for t in teams))


def score_breakdowns(payload: Json | None) -> Iterator[tuple[str, int, int, str, dict[str, Any]]]:
    """Yield the level, series, match number, alliance and breakdown of each side of each scored match."""
    for row in _collection(payload, "matchScores", "MatchScores", "scores"):
        level = normalize_level(row.get("matchLevel"))
        series = int(row.get("matchSeries") or 0)
        number = int(row["matchNumber"])
        for side in row.get("alliances") or []:
            alliance = str(side.get("alliance", "")).strip().upper()
            if alliance not in ("RED", "BLUE"):
                continue
            yield level, series, number, alliance, {k: v for k, v in side.items() if k != "alliance"}


# ------------------------------------------------------------------------------------------------ rankings, awards


def ranking_rows(event_id: uuid.UUID, payload: Json | None) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event_id,
            "team_number": row["teamNumber"],
            "rank": row["rank"],
            "wins": row.get("wins"),
            "losses": row.get("losses"),
            "ties": row.get("ties"),
            "matches_played": row.get("matchesPlayed"),
            "qual_average": row.get("qualAverage"),
            "dq": row.get("dq"),
            **{f"sort_order_{i}": row.get(f"sortOrder{i}") for i in range(1, 7)},
        }
        for row in _collection(payload, "rankings", "Rankings")
    ]


JUDGED_TEAM_AWARD_CODES = frozenset({1, 3, 4, 5, 6, 7, 8, 9, 11, 25, 26})


def award_rows(event_id: uuid.UUID, payload: Json | None) -> list[dict[str, Any]]:
    """Judged team awards from ``/awards/{eventCode}``, keyed by award code and series."""
    deduped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in _collection(payload, "awards", "Awards"):
        award_code = int(row.get("awardId") or 0)
        team_number = row.get("teamNumber")
        if award_code not in JUDGED_TEAM_AWARD_CODES or team_number is None:
            continue
        series = int(row.get("series") or 1)
        deduped[(award_code, series)] = {
            "event_id": event_id,
            "award_code": award_code,
            "series": series,
            "team_number": team_number,
            "source": "ftc_events",
        }
    return list(deduped.values())


SCOUT_AWARD_TYPES: dict[str, int] = {
    "JudgesChoice": 1,
    "Promote": 3,
    "Control": 4,
    "Motivate": 5,
    "Design": 6,
    "Innovate": 7,
    "Connect": 8,
    "Think": 9,
    "Inspire": 11,
    "Reach": 25,
    "Sustain": 26,
}

SCOUT_AWARD_TYPES_DROPPED = frozenset(
    {
        "Compass",
        "DeansListFinalist",
        "DeansListSemiFinalist",
        "DeansListWinner",
        "Winner",
        "Finalist",
        "DivisionWinner",
        "DivisionFinalist",
        "ConferenceFinalist",
        "TopRanked",
    }
)


def scout_award_rows(event_id: uuid.UUID, awards: Iterable[Json]) -> list[dict[str, Any]]:
    deduped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in awards:
        kind = str(row.get("type") or "")
        award_code = SCOUT_AWARD_TYPES.get(kind)
        if award_code is None:
            if kind not in SCOUT_AWARD_TYPES_DROPPED:
                log.warning("ftcscout.unmapped_award_type", award_type=kind)
            continue
        team_number = row.get("teamNumber")
        if team_number is None:
            continue
        series = int(row.get("placement") or 1)
        deduped[(award_code, series)] = {
            "event_id": event_id,
            "award_code": award_code,
            "series": series,
            "team_number": team_number,
            "source": "ftcscout",
        }
    return list(deduped.values())


# ---------------------------------------------------------------------------------------------- alliances

ALLIANCE_SLOTS: tuple[str, ...] = ("captain", "round1", "round2")

SEATING_ACTIONS = frozenset({"CAPTAIN", "ACCEPT"})


def _slot_team(slot: Any) -> int | None:
    return slot.get("teamNumber") if isinstance(slot, Mapping) else None


def playoff_alliance_rows(event_id: uuid.UUID, payload: Json | None) -> list[dict[str, Any]]:
    """The seated alliances from ``/alliances/{eventCode}``.

    ``round3``, ``backup`` and ``backupReplaced`` are discarded.
    """
    out: list[dict[str, Any]] = []
    for row in _collection(payload, "alliances", "Alliances"):
        number = row.get("number")
        if number is None:
            continue
        out.append(
            {
                "event_id": event_id,
                "alliance_number": int(number),
                "name": row.get("name"),
                **{slot: _slot_team(row.get(slot)) for slot in ALLIANCE_SLOTS},
            }
        )
    return out


def alliance_pick_rows(event_id: uuid.UUID, payload: Json | None) -> list[dict[str, Any]]:
    """The pick log from ``/alliances/{eventCode}/selection``, in the order it happened.

    The log names the team and the result, never the alliance, so ``alliance_number`` is left to the writer.
    """
    deduped: dict[int, dict[str, Any]] = {}
    for position, row in enumerate(_collection(payload, "selections", "Selections")):
        team_number = row.get("team")
        action = str(row.get("result") or "").strip().upper()
        if team_number is None:
            continue
        if action not in ALLIANCE_PICK_ACTIONS:
            log.warning("alliances.unmapped_selection_result", result=row.get("result"))
            continue
        index = row.get("index")
        deduped[int(index) if index is not None else position] = {
            "event_id": event_id,
            "team_number": int(team_number),
            "action": action,
        }
    return [{"pick_ordinal": ordinal, **row} for ordinal, row in sorted(deduped.items())]


# ----------------------------------------------------------------------------------------------------- advancement


def advancement_points_rows(event_id: uuid.UUID, payload: Any) -> list[dict[str, Any]]:
    """Official advancement points for a completed event."""
    rows = payload if isinstance(payload, list) else _collection(payload, "advancementPoints")
    out: list[dict[str, Any]] = []
    for row in rows:
        team = row.get("team", row.get("teamNumber"))
        if team is None:
            continue
        points = row.get("points")
        total = float(points[0]) if isinstance(points, list) and points else float(points or 0.0)
        out.append(
            {
                "event_id": event_id,
                "team_number": int(team),
                "points": total,
                "detail": {"points": points},
            }
        )
    return out


def advancement_slot_rows(event_id: uuid.UUID, payload: Json | None) -> list[dict[str, Any]]:
    """Who advanced, in the order the endpoint publishes them."""
    return [
        {
            "event_id": event_id,
            "slot": int(row.get("slot") or index + 1),
            "team_number": row.get("team", row.get("teamNumber")),
            "status": row.get("status"),
        }
        for index, row in enumerate(_collection(payload, "advancement", "slots", "teams"))
    ]


def event_advancement_row(event_id: uuid.UUID, payload: Json | None) -> dict[str, Any] | None:
    """The header of ``/advancement/{eventCode}``"""
    if not payload:
        return None
    return {
        "event_id": event_id,
        "advances_to": payload.get("advancesTo"),
        "slots": payload.get("slots"),
        "slots_first_championship": payload.get("fcmpReserved"),
    }


# ------------------------------------------------------------------------------------------------ FTCScout payloads

_SCOUT_LEVELS: dict[str, str] = {
    "QUALS": "QUALIFICATION",
    "SEMIS": "SEMIFINAL",
    "FINALS": "FINAL",
    "DOUBLEELIM": "PLAYOFF",
}

_SCOUT_STATION: dict[str, str] = {"ONE": "1", "TWO": "2"}


def scout_station(alliance: str | None, station: str | None) -> str | None:
    """Compose FTCScout's separate alliance and station into one slot label."""
    side = (alliance or "").strip().upper()
    slot = _SCOUT_STATION.get((station or "").strip().upper())
    if side not in ("RED", "BLUE") or slot is None:
        return None
    return f"{side.title()}{slot}"


def scout_match_rows(event_id: uuid.UUID, timezone: str, matches: Iterable[Json]) -> list[dict[str, Any]]:
    """Rows from FTCScout's ``Event.matches``, shaped like :func:`hybrid_match_rows`."""
    out: list[dict[str, Any]] = []
    for row in matches:
        if not row.get("hasBeenPlayed"):
            continue
        level = _SCOUT_LEVELS.get(str(row.get("tournamentLevel", "")).upper())
        if level is None:
            continue

        slots: list[dict[str, Any]] = []
        for slot in row.get("teams") or []:
            station = scout_station(slot.get("alliance"), slot.get("station"))
            if station is None or slot.get("teamNumber") is None:
                continue
            slots.append(
                {
                    "station": station,
                    "alliance": station[:-1].upper(),
                    "team_number": slot["teamNumber"],
                    "surrogate": bool(slot.get("surrogate")),
                    "no_show": bool(slot.get("noShow")),
                    "dq": bool(slot.get("dq")),
                    "on_field": bool(slot.get("onField", True)),
                    "alliance_role": slot.get("allianceRole"),
                }
            )
        if len(slots) != 4:
            continue

        scores = row.get("scores") or {}
        red, blue = scores.get("red") or {}, scores.get("blue") or {}
        if not red or not blue:
            continue

        out.append(
            {
                "event_id": event_id,
                "level": level,
                "series": int(row.get("series") or 0),
                "match_number": int(row["matchNum"]),
                "description": row.get("description"),
                **_scout_times(row, timezone),
                "score_red_final": red.get("totalPoints"),
                "score_blue_final": blue.get("totalPoints"),
                "score_red_auto": red.get("autoPoints"),
                "score_blue_auto": blue.get("autoPoints"),
                "score_red_foul": red.get("penaltyPointsCommitted"),
                "score_blue_foul": blue.get("penaltyPointsCommitted"),
                "teams": slots,
            }
        )
    return out


def scout_alliance_role_rows(event_id: uuid.UUID, matches: Iterable[Json]) -> list[dict[str, Any]]:
    """Playoff alliance roles, one row per team that held one, carrying only what identifies its match slot."""
    out: list[dict[str, Any]] = []
    for row in matches:
        level = _SCOUT_LEVELS.get(str(row.get("tournamentLevel", "")).upper())
        if level is None or level == "QUALIFICATION":
            continue
        for slot in row.get("teams") or []:
            role = slot.get("allianceRole")
            station = scout_station(slot.get("alliance"), slot.get("station"))
            if role is None or station is None or slot.get("teamNumber") is None:
                continue
            if role not in ALLIANCE_ROLES:
                log.warning("ftcscout.unmapped_alliance_role", role=role)
                continue
            out.append(
                {
                    "event_id": event_id,
                    "level": level,
                    "series": int(row.get("series") or 0),
                    "match_number": int(row["matchNum"]),
                    "station": station,
                    "team_number": slot["teamNumber"],
                    "alliance_role": role,
                }
            )
    return out


def scout_event_codes(events: Iterable[Json]) -> list[str]:
    return [str(event["code"]) for event in events if event.get("code")]


def _scout_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _scout_times(row: Json, timezone: str) -> dict[str, Any]:
    """FTCScout returns UTC where FTC Events returns venue-local, so the wall clock is derived the other way round."""
    out: dict[str, Any] = {}
    for field, column in (
        ("scheduledStartTime", "start_time"),
        ("actualStartTime", "actual_start_time"),
        ("postResultTime", "post_result_time"),
    ):
        utc_value = _scout_utc(row.get(field))
        out[f"{column}_utc"] = utc_value
        out[f"{column}_local"] = utc_value.astimezone(ZoneInfo(timezone)).replace(tzinfo=None) if utc_value else None
    out["modified_on_utc"] = None
    return out
