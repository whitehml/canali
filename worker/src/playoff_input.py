"""Validate FIRST's seated alliances and playoff results before snapshot publication."""

import uuid

from components import values
from compute import integer, number

from epa.corpus import Alliance, RatedMatch
from sim.playoff import published_winners
from sim.selection import alliance_count, alliance_size
from warehouse.rules.partition import RECONCILE_TOLERANCE


def seated_alliances(payload, event, pack):
    rows = sorted(payload["alliances"], key=lambda row: row["number"])
    count = alliance_count(pack.structure, len(event.roster))
    size = alliance_size(event.event_type)
    if [row["number"] for row in rows] != list(range(1, count + 1)):
        raise ValueError("published alliances need contiguous seeds matching the field")
    alliances = [
        tuple(
            integer(row[slot]["teamNumber"], "alliance team", minimum=1)
            for slot in ("captain", "round1", "round2")[:size]
        )
        for row in rows
    ]
    selected = [team for alliance in alliances for team in alliance]
    if len(set(selected)) != len(selected) or not set(selected) <= set(event.roster):
        raise ValueError("published alliances need distinct roster teams")
    return alliances


def current_playoff(event, pack, options):
    if not options.selection_published or options.through is not None:
        return None, [], {}, {}
    if not event.matches or any(match.red_score is None for match in event.matches):
        raise ValueError("published alliances require completed qualifications")
    if options.alliances is None or options.playoff_schedule is None or options.playoff_scores is None:
        raise ValueError("published selection requires alliances, playoff schedule and score payloads")
    alliances = seated_alliances(options.alliances, event, pack)
    matches, breakdowns, decided = playoff_inputs(
        event, pack, alliances, options.playoff_schedule, options.playoff_scores
    )
    return alliances, matches, breakdowns, decided


def playoff_breakdowns(scores):
    breakdowns = {}
    for row in scores["matchScores"]:
        if row["matchLevel"] not in ("PLAYOFF", "SEMIFINAL", "FINAL"):
            raise ValueError("playoff breakdowns must have a playoff level")
        for side in row["alliances"]:
            key = (row["matchSeries"], row["matchNumber"], side["alliance"].lower())
            if key in breakdowns or key[2] not in ("red", "blue"):
                raise ValueError("duplicate or invalid playoff breakdown")
            breakdowns[key] = side
    return breakdowns


def scored_playoffs(schedule):
    seen = set()
    for row in sorted(schedule["schedule"], key=lambda row: (row["series"], row["matchNumber"])):
        series = integer(row["series"], "playoff series", minimum=1)
        key = (series, integer(row["matchNumber"], "playoff match", minimum=1))
        if key in seen or row["tournamentLevel"] not in ("PLAYOFF", "SEMIFINAL", "FINAL"):
            raise ValueError("duplicate match or invalid playoff level")
        seen.add(key)
        finals = [row[f"score{side}Final"] for side in ("Red", "Blue")]
        if all(score is None for score in finals):
            continue
        if any(score is None for score in finals):
            raise ValueError("both playoff scores must be present")
        yield row, series, key, finals


def playoff_inputs(event, pack, alliances, schedule, scores):
    seed_of = {frozenset(alliance): seed for seed, alliance in enumerate(alliances, 1)}
    breakdowns = playoff_breakdowns(scores)
    matches, components, games = [], {}, []
    for row, series, key, finals in scored_playoffs(schedule):
        slots = row["teams"]
        size = len(alliances[0])
        expected = {f"{side}{n}" for side in ("Red", "Blue") for n in range(1, size + 1)}
        if len(slots) != len(expected) or {s["station"] for s in slots} != expected:
            raise ValueError("playoff stations must match alliance size")
        sides = [[s for s in slots if s["station"].startswith(side)] for side in ("Red", "Blue")]
        seeds = [seed_of.get(frozenset(s["teamNumber"] for s in side)) for side in sides]
        if None in seeds or seeds[0] == seeds[1]:
            raise ValueError("playoff teams differ from seated alliances")
        identifier = uuid.uuid5(event.event_id, f"playoff:{series}:{key[1]}")
        rated, points = [], []
        for side, opponent, stations, final in zip(("red", "blue"), ("Blue", "Red"), sides, finals, strict=True):
            if any(not isinstance(s.get("noShow", False), bool) for s in stations):
                raise ValueError("noShow must be boolean")
            teams = tuple(s["teamNumber"] for s in stations if not s.get("noShow", False))
            if not teams:
                raise ValueError("a scored playoff alliance needs a present team")
            score = number(final, "playoff score") - number(row.get(f"score{opponent}Foul"), "committed foul points")
            if score < 0:
                raise ValueError("non-foul playoff score must be nonnegative")
            component = values(pack, breakdowns[(*key, side)])
            if abs(sum(component.values()) - score) > RECONCILE_TOLERANCE:
                raise ValueError("playoff breakdown does not reconcile with its non-foul score")
            components[identifier, side.upper()] = component
            rated.append(Alliance(teams, score))
            points.append(number(final, "playoff score"))
        games.append((series, *seeds, *points))
        matches.append(
            RatedMatch(
                match_id=identifier,
                event_id=event.event_id,
                season=pack.season,
                event_code=event.code,
                event_match_ordinal=max((m.ordinal for m in event.matches), default=0) + len(matches) + 1,
                red=rated[0],
                blue=rated[1],
                level="PLAYOFF",
                event_type=event.event_type,
                event_date=event.day,
            )
        )
    return matches, components, published_winners(len(alliances), games)
