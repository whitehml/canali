"""Paired DECODE qualification scenarios with explicit robot LEAVE outcomes."""

from time import monotonic

import numpy as np
from forecast import BATCH_ITERATIONS, BATCH_SECONDS
from pydantic import BaseModel, ConfigDict, Field

from sim.event import _alliance_predictions
from sim.match import draw_matches
from sim.standings import MatchResults, RankingRules, standings

LEAVE_POINTS = 3
# DECODE manual 10.5.4: 0/5/10 per robot, plus 10 when both fully return.
BASE_TOTALS = np.array([0, 5, 10, 15, 30], dtype=float)


class LeaveComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    team: int = Field(gt=0)
    current: float = Field(ge=0, le=1)
    improved: float = Field(ge=0, le=1)


def base_points(expected, uniform):
    """Interpolate adjacent legal totals, preserving a clipped expected alliance BASE rating."""
    expected = np.clip(expected, 0, 30)
    high = np.clip(np.searchsorted(BASE_TOTALS, expected, side="right"), 1, len(BASE_TOTALS) - 1)
    low = high - 1
    chance = (expected - BASE_TOTALS[low]) / (BASE_TOTALS[high] - BASE_TOTALS[low])
    return np.where(uniform < chance, BASE_TOTALS[high], BASE_TOTALS[low])


def validate_leave(snapshot, request):
    if snapshot.rule_pack.season != 2025 or snapshot.rule_pack.game != "DECODE":
        raise ValueError("autonomous LEAVE comparisons currently support DECODE only")
    if request.team not in snapshot.field.predicted or snapshot.schedule is None:
        raise ValueError("choose a field team with a published qualification schedule")
    if request.improved < request.current:
        raise ValueError("improved success rate must be at least the current rate")
    required = {"auto_leave_points", "teleop_base_points"}
    if any(not required <= values.keys() for values in snapshot.field.predicted.values()):
        raise ValueError("LEAVE comparisons require leave and base component ratings")
    schedule = snapshot.schedule.build()
    remaining = np.ones(len(schedule.stations), dtype=bool)
    if snapshot.progress:
        remaining &= ~np.array(snapshot.progress.played)
    appearances = (schedule.stations == request.team).any(axis=(1, 2))
    if not np.any(remaining & appearances):
        raise ValueError("this team has no remaining qualification matches")


def qualification_pair(snapshot, request, rng):
    """Share every random outcome across scenarios except this robot's LEAVE success."""
    schedule = snapshot.schedule.build()
    predicted = _alliance_predictions(snapshot.field, schedule)
    residual = {name: values.copy() for name, values in predicted.items()}
    residual["auto_leave_points"][:] = 0
    residual["teleop_base_points"][:] = 0
    models = snapshot.models.build().season
    drawn = draw_matches(models, snapshot.rule_pack, residual, snapshot.field.event_type, rng)
    base = base_points(predicted["teleop_base_points"], rng.random(drawn.score.shape))
    probabilities = np.vectorize(
        lambda team: np.clip(snapshot.field.predicted[int(team)]["auto_leave_points"] / LEAVE_POINTS, 0, 1),
        otypes=[float],
    )(schedule.stations)
    leave_uniform = rng.random(schedule.stations.shape)
    target = schedule.stations == request.team
    movement = next(b for b in snapshot.rule_pack.ranking.bonus_rp if b.component == "movementRP")
    # Other autonomous points retain their modeled mean share; BASE never leaks into AUTO tiebreakers.
    other_total = np.sum(list(residual.values()), axis=0)
    other_auto = np.sum([v for k, v in residual.items() if k.startswith("auto_")], axis=0)
    auto_share = np.divide(other_auto, other_total, out=np.zeros_like(other_total), where=other_total > 0)
    result = []
    for rate in (request.current, request.improved):
        leave = LEAVE_POINTS * (leave_uniform < np.where(target, rate, probabilities)).sum(axis=2)
        components = {name: value.copy() for name, value in drawn.components.items()}
        components["preFoulTotal"] = drawn.components["preFoulTotal"] + base + leave
        components["autoPoints"] = drawn.components["preFoulTotal"] * auto_share + leave
        components["teleopBasePoints"] = base.copy()
        bonus = {**drawn.bonus, "movementRP": base + leave >= movement.threshold(snapshot.field.event_type)}
        outcome = MatchResults(drawn.score + base + leave, bonus, components)
        if snapshot.progress:
            outcome = snapshot.progress.build().merged(outcome)
        result.append(outcome)
    return schedule, result


def advance_leave(snapshot, request, checkpoint=None):
    validate_leave(snapshot, request)
    rng = np.random.default_rng(snapshot.seed)
    n = 0
    rank = np.zeros(2)
    rank_delta = np.zeros(2)
    totals = np.zeros((2, len(snapshot.schedule.stations), 3))
    if checkpoint:
        rng.bit_generator.state = checkpoint["rng"]
        n = checkpoint["iterations"]
        rank, rank_delta, totals = (np.array(checkpoint[key]) for key in ("rank", "rank_delta", "totals"))
    rules = RankingRules.from_pack(snapshot.rule_pack)
    started = monotonic()
    for _ in range(min(BATCH_ITERATIONS, snapshot.max_iterations - n)):
        schedule, pair = qualification_pair(snapshot, request, rng)
        trial_seed = int(rng.integers(0, 2**63))
        places = []
        for index, results in enumerate(pair):
            ranked = standings(schedule, results, rules, np.random.default_rng(trial_seed))
            alliance_rp = rules.alliance_rp(results)
            place = int(np.flatnonzero(ranked.teams == request.team)[0]) + 1
            places.append(place)
            rank[index] += place
            for match, alliance, _station in zip(*np.nonzero(schedule.stations == request.team), strict=True):
                totals[index, match] += (
                    results.score[match, alliance],
                    results.bonus["movementRP"][match, alliance],
                    alliance_rp[match, alliance],
                )
        delta = places[1] - places[0]
        rank_delta += (delta, delta * delta)
        n += 1
        if monotonic() - started >= BATCH_SECONDS:
            break
    complete = n >= snapshot.max_iterations
    played = snapshot.progress.played if snapshot.progress else [False] * len(schedule.stations)
    rows = []
    for match, alliance, station in zip(*np.nonzero(schedule.stations == request.team), strict=True):
        if played[match]:
            continue
        rows.append(
            {
                "match": int(match + 1),
                "partner": int(schedule.stations[match, alliance, 1 - station]),
                "surrogate": bool(schedule.surrogate[match, alliance, station]),
                "current": (totals[0, match] / n).tolist(),
                "improved": (totals[1, match] / n).tolist(),
            }
        )
    result = {
        "iterations": n,
        "mean_rank": (rank / n).tolist(),
        "rank_delta_se": float(np.sqrt(max(0, rank_delta[1] / n - (rank_delta[0] / n) ** 2) / max(1, n - 1))),
        "matches": rows,
    }
    return {
        "iterations": n,
        "rng": rng.bit_generator.state,
        "rank": rank.tolist(),
        "rank_delta": rank_delta.tolist(),
        "totals": totals.tolist(),
    }, {"iterations": n, "status": "complete" if complete else "running", "result": result if complete else None}
