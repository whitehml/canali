"""Synthetic payload generator.

No real FTC data enters git history, so a test that needs an event builds one here. The payload shape is copied from
the API and every value is drawn from a seeded RNG. Components are the ones
the 9999 synthetic rule pack declares, and scores are shaped like FTC scoring, roughly 100 to 300 points an
alliance with penalties rare.
"""

from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

STATIONS: tuple[str, ...] = ("Red1", "Red2", "Blue1", "Blue2")

SYNTHETIC_SEASON = 9999
"""The season the fixture rule pack declares. Its components are the ones :func:`breakdown` reports."""

_PERCH = ("NONE", "LOW", "HIGH")
_LATTICE = ("NONE", "PURPLE", "GREEN")


@dataclass(slots=True)
class SyntheticEvent:
    """One event's worth of payloads."""

    season: int
    code: str
    event_id: uuid.UUID
    events: dict[str, Any]
    teams: dict[str, Any]
    hybrid_qual: dict[str, Any]
    hybrid_playoff: dict[str, Any]
    scores_qual: dict[str, Any]
    scores_playoff: dict[str, Any]
    rankings: dict[str, Any]
    awards: dict[str, Any]
    alliances: dict[str, Any]
    selection: dict[str, Any]
    advancement: dict[str, Any]
    advancement_points: list[dict[str, Any]]
    team_numbers: list[int] = field(default_factory=list)

    def replay(self, match_number: int, *, level: str = "QUALIFICATION") -> SyntheticEvent:
        """Return a copy in which one match's scores changed."""
        clone = copy.deepcopy(self)
        schedule = clone.hybrid_qual if level == "QUALIFICATION" else clone.hybrid_playoff
        for row in schedule["schedule"]:
            if row["matchNumber"] == match_number and row["tournamentLevel"] == level:
                row["scoreRedFinal"] = int(row["scoreRedFinal"]) + 17
                row["modifiedOn"] = "2026-01-02T12:00:00"
        return clone


def breakdown(rng: random.Random) -> dict[str, Any]:
    """One alliance's score detail."""
    auto = rng.randrange(0, 60, 3)
    teleop = rng.randrange(30, 160, 3)
    pre_foul = auto + teleop
    foul = rng.choices([0, 5, 15], weights=[80, 15, 5])[0]

    return {
        "autoWidgetPoints": auto,
        "teleopWidgetPoints": teleop,
        "preFlangeTotal": pre_foul,
        "totalPoints": pre_foul + foul,
        "robot1Sprocket": rng.randrange(0, 20, 5),
        "flangeRP": rng.random() < 0.45,
        "robot2Perch": rng.choice(_PERCH),
        "gizmoLatticeState": [rng.choice(_LATTICE) for _ in range(9)],
        # Always 0 in a two-team alliance, a remnant of the single-player remote score models.
        "team": 0,
    }


def _foul(detail: dict[str, Any]) -> int:
    return int(detail["totalPoints"]) - int(detail["preFlangeTotal"])


def generate_event(
    *,
    season: int = SYNTHETIC_SEASON,
    code: str = "SYNTH01",
    n_teams: int = 12,
    n_quals: int = 20,
    n_playoffs: int = 4,
    n_alliances: int = 2,
    alliance_picks: int = 1,
    region_code: str = "USPA",
    timezone: str | None = "America/New_York",
    start: date = date(2026, 1, 10),
    seed: int = 20260826,
    remote: bool = False,
    hybrid: bool = False,
    event_type: str = "Qualifier",
) -> SyntheticEvent:
    """One event across every endpoint the ingest reads.

    ``alliance_picks`` is how many teams each captain picks: one seats the two-team alliances a qualifier runs,
    two the three-team alliances of a FIRST Championship event.
    """
    rng = random.Random(seed)
    event_id = uuid.UUID(int=rng.getrandbits(128), version=4)
    team_numbers = sorted(rng.sample(range(10000, 40000), n_teams))

    events = {
        "events": [
            {
                "eventId": str(event_id),
                "code": code,
                "name": f"Synthetic {code}",
                "type": "1",
                "typeName": event_type,
                "divisionCode": None,
                "regionCode": region_code,
                # The API returns '' rather than null for several optional codes.
                "leagueCode": "",
                "fieldCount": 2,
                "dateStart": f"{start.isoformat()}T00:00:00",
                "dateEnd": f"{(start + timedelta(days=1)).isoformat()}T00:00:00",
                "venue": "Synthetic Arena",
                "city": "Nowhere",
                "stateprov": "PA",
                "country": "USA",
                "timezone": timezone,
                "published": True,
                "remote": remote,
                "hybrid": hybrid,
            }
        ]
    }

    teams = {
        "teams": [
            {
                "teamNumber": number,
                "nameFull": f"Synthetic Team {number}",
                "nameShort": f"Synth {number}",
                "city": "Nowhere",
                "stateProv": "PA",
                "country": "USA",
                "rookieYear": rng.randint(2005, 2025),
                "homeRegion": region_code,
            }
            for number in team_numbers
        ],
        "teamCountTotal": n_teams,
    }

    def build(level: str, count: int, series: int) -> tuple[list[Any], list[Any]]:
        schedule: list[Any] = []
        scores: list[Any] = []
        for index in range(count):
            number = index + 1
            picks = rng.sample(team_numbers, 4)
            red, blue = breakdown(rng), breakdown(rng)
            when = datetime.combine(start, datetime.min.time()) + timedelta(hours=9, minutes=6 * index)
            schedule.append(
                {
                    "description": f"{level} {number}",
                    "tournamentLevel": level,
                    "series": series,
                    "matchNumber": number,
                    "field": "1",
                    "startTime": when.isoformat(),
                    "actualStartTime": (when + timedelta(minutes=1)).isoformat(),
                    "postResultTime": (when + timedelta(minutes=4)).isoformat(),
                    "modifiedOn": "2026-01-01T12:00:00",
                    "scoreRedFinal": red["totalPoints"],
                    "scoreRedAuto": red["autoWidgetPoints"],
                    "scoreRedFoul": _foul(red),
                    "scoreBlueFinal": blue["totalPoints"],
                    "scoreBlueAuto": blue["autoWidgetPoints"],
                    "scoreBlueFoul": _foul(blue),
                    "teams": [
                        {
                            "teamNumber": picks[slot],
                            "station": STATIONS[slot],
                            "surrogate": level == "QUALIFICATION" and rng.random() < 0.05,
                            "noShow": rng.random() < 0.02,
                            "dq": False,
                            "onField": True,
                        }
                        for slot in range(4)
                    ],
                }
            )
            scores.append(
                {
                    "matchLevel": level,
                    "matchSeries": series,
                    "matchNumber": number,
                    "alliances": [
                        {"alliance": "Red", **red},
                        {"alliance": "Blue", **blue},
                    ],
                }
            )
        return schedule, scores

    qual_schedule, qual_scores = build("QUALIFICATION", n_quals, series=0)
    playoff_schedule, playoff_scores = build("PLAYOFF", n_playoffs, series=1)

    ranked = rng.sample(team_numbers, n_teams)
    rankings = {
        "rankings": [
            {
                "rank": index + 1,
                "teamNumber": number,
                "sortOrder1": round(rng.uniform(1, 6), 2),
                "sortOrder2": round(rng.uniform(100, 260), 2),
                "sortOrder3": round(rng.uniform(5, 30), 2),
                "sortOrder4": round(rng.uniform(20, 90), 2),
                "sortOrder5": round(rng.uniform(0, 1e6), 2),
                "sortOrder6": round(rng.uniform(100, 300), 2),
                "wins": rng.randint(0, 5),
                "losses": rng.randint(0, 5),
                "ties": 0,
                "qualAverage": round(rng.uniform(80, 250), 2),
                "dq": 0,
                "matchesPlayed": 5,
            }
            for index, number in enumerate(ranked)
        ]
    }

    alliances, selection = _selection(ranked, n_alliances, alliance_picks)

    awards = {
        "awards": [
            {"awardId": 11, "teamNumber": ranked[0], "eventCode": code, "name": "Inspire Award", "series": 1},
            {"awardId": 9, "teamNumber": ranked[1], "eventCode": code, "name": "Think Award", "series": 1},
            {"awardId": 9, "teamNumber": ranked[2], "eventCode": code, "name": "Think Award", "series": 2},
            # Neither of these is stored: 2 is not a judged team award, and an unclaimed slot has no winner.
            {"awardId": 2, "teamNumber": ranked[3], "eventCode": code, "name": "Winner", "series": 1},
            {"awardId": 6, "teamNumber": None, "eventCode": code, "name": "Design Award", "series": 1},
        ]
    }

    advancement = {
        "advancesTo": "SYNTHCMP",
        "slots": 4,
        "fcmpReserved": 1,
        "advancement": [
            {
                "team": number,
                "displayTeam": str(number),
                "slot": index + 1,
                "criteria": f"Criteria {index + 1}",
                "declined": False,
                "status": "FIRST",
            }
            for index, number in enumerate(ranked[:4])
        ],
    }

    advancement_points = [
        {"team": number, "points": [round(40 - 3 * index, 1), float(index)]} for index, number in enumerate(ranked)
    ]

    return SyntheticEvent(
        season=season,
        code=code,
        event_id=event_id,
        events=events,
        teams=teams,
        hybrid_qual={"schedule": qual_schedule},
        hybrid_playoff={"schedule": playoff_schedule},
        scores_qual={"matchScores": qual_scores},
        scores_playoff={"matchScores": playoff_scores},
        rankings=rankings,
        awards=awards,
        alliances=alliances,
        selection=selection,
        advancement=advancement,
        advancement_points=advancement_points,
        team_numbers=team_numbers,
    )


def _selection(
    ranked: list[int],
    n_alliances: int,
    picks: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The seated alliances and the pick log that produced them."""
    captains = ranked[:n_alliances]
    pool = list(ranked[n_alliances:])
    seats: list[list[int]] = [[captain] for captain in captains]

    log: list[dict[str, Any]] = [{"team": captain, "result": "CAPTAIN"} for captain in captains]
    declined = False
    for round_index in range(picks):
        order = range(n_alliances) if round_index % 2 == 0 else reversed(range(n_alliances))
        for alliance_index in order:
            if not declined:
                log.append({"team": pool.pop(0), "result": "DECLINE"})
                declined = True
            picked = pool.pop(0)
            seats[alliance_index].append(picked)
            log.append({"team": picked, "result": "ACCEPT"})

    alliances = {
        "alliances": [
            {
                "number": index + 1,
                "name": f"Alliance {index + 1}",
                **{
                    slot: {"teamNumber": seat, "displayTeamNumber": str(seat), "teamName": f"Synth {seat}"}
                    for slot, seat in zip(("captain", "round1", "round2"), members, strict=False)
                },
                "round3": None,
                "backup": None,
                "backupReplaced": None,
            }
            for index, members in enumerate(seats)
        ],
        "count": n_alliances,
    }
    selection = {
        "selections": [{"index": index, **row} for index, row in enumerate(log)],
        "count": len(log),
    }
    return alliances, selection
