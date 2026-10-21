"""Qualification totals and match forecasts from prepared event inputs.

The caller supplies pre-event EPA, schedules and fitted score-model coefficients.
Season normalization, component ratings and event advancement are separate workloads.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np

from epa.model import Schedule, update_for_match
from pridge.estimator import fit
from sim.score import ScoreModel

MAX_TEAMS = 128
MAX_MATCHES = 1000
DRAW_BATCH = 256


def number(value: object, name: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return result


def integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer at least {minimum}")
    return value


def validate(document: dict) -> None:
    """Validate the bounded input before numerical code or D1 writes."""
    if not isinstance(document, dict):
        raise ValueError("event input must be an object")
    teams, matches = document.get("teams"), document.get("matches")
    if not isinstance(teams, list) or not 4 <= len(teams) <= MAX_TEAMS:
        raise ValueError(f"teams must contain 4 to {MAX_TEAMS} entries")
    if not isinstance(matches, list) or not 1 <= len(matches) <= MAX_MATCHES:
        raise ValueError(f"matches must contain 1 to {MAX_MATCHES} entries")
    known = set()
    for team in teams:
        team_number = integer(team["number"], "team number", minimum=1)
        if team_number in known:
            raise ValueError("team numbers must be unique")
        known.add(team_number)
        number(team["epa"], "EPA", minimum=-math.inf)
        integer(team["played"], "matches played")
    previous = 0
    for match in matches:
        ordinal = integer(match["ordinal"], "match ordinal", minimum=1)
        if ordinal <= previous:
            raise ValueError("matches must have strictly increasing ordinals")
        previous = ordinal
        for alliance in ("red", "blue"):
            members = match[alliance]
            if not isinstance(members, list) or not 1 <= len(members) <= 2:
                raise ValueError("each alliance needs one or two rated teams")
            for team in members:
                integer(team, "scheduled team", minimum=1)
        participants = match["red"] + match["blue"]
        if len(set(participants)) != len(participants) or not set(participants) <= known:
            raise ValueError("scheduled teams must be distinct and declared")
        red, blue = match["red_score"], match["blue_score"]
        if (red is None) != (blue is None):
            raise ValueError("both non-foul scores must be present or both null")
        if red is not None:
            number(red, "red score")
            number(blue, "blue score")
    for name in ("k", "m"):
        schedule = document[name]
        breakpoints = schedule["breakpoints"]
        for edge in breakpoints:
            integer(edge, f"{name} breakpoint")
        if len(set(breakpoints)) != len(breakpoints):
            raise ValueError("schedule breakpoints must be distinct")
        for value in schedule["values"]:
            number(value, f"{name} value")
        Schedule(tuple(breakpoints), tuple(schedule["values"]))
    number(document["ridge_lambda"], "ridge lambda", minimum=1e-9)
    draws = integer(document["draws"], "draws", minimum=100)
    if draws > 100_000:
        raise ValueError("draws must not exceed 100000")
    integer(document["seed"], "seed")
    if "season_prior" in document:
        prior = document["season_prior"]
        integer(prior["season"], "prior season", minimum=2015)
        integer(prior["revision"], "prior revision", minimum=1)
    model = ScoreModel(**document["score_model"])
    for name in ("intercept", "slope", "dispersion", "dof", "skew", "foul_mean", "foul_variance"):
        number(getattr(model, name), name, minimum=-1e6 if name == "intercept" else 0)
    if model.dof <= 2 or model.skew <= 0 or model.dispersion <= 0 or model.slope <= 0:
        raise ValueError("invalid score-model degrees of freedom, skew, dispersion or slope")


def compute(document: dict) -> dict:
    """Reuse the existing EPA, ridge and score-sampling kernels on one event."""
    validate(document)
    teams = sorted(team["number"] for team in document["teams"])
    column = {team: index for index, team in enumerate(teams)}
    initial = {team["number"]: float(team["epa"]) for team in document["teams"]}
    ratings = initial.copy()
    played = {team["number"]: team["played"] for team in document["teams"]}
    schedules = {
        name: Schedule(tuple(document[name]["breakpoints"]), tuple(document[name]["values"])) for name in ("k", "m")
    }
    scored = [match for match in document["matches"] if match["red_score"] is not None]
    design = np.zeros((2 * len(scored), len(teams)))
    response = np.empty(2 * len(scored))
    for index, match in enumerate(scored):
        pending = {}
        for side, opponent, offset in (("red", "blue", 0), ("blue", "red", 1)):
            own = match[side]
            average_played = sum(played[team] for team in own) // len(own)
            update = update_for_match(
                ratings,
                own_teams=own,
                opponent_teams=match[opponent],
                own_score=match[f"{side}_score"],
                opponent_score=match[f"{opponent}_score"],
                k=schedules["k"].at(average_played),
                m=schedules["m"].at(average_played),
            )
            pending.update(update.deltas)
            for team in own:
                design[2 * index + offset, column[team]] = 1
            response[2 * index + offset] = match[f"{side}_score"]
        for team, delta in pending.items():
            ratings[team] += delta
            played[team] += 1
    prior = np.array([initial[team] for team in teams])
    ridge = fit(design, response, prior, document["ridge_lambda"]).beta if scored else prior
    model = ScoreModel(**document["score_model"])
    rng = np.random.default_rng(document["seed"])
    forecasts = []
    for match in document["matches"]:
        if match["red_score"] is not None:
            continue
        predicted = np.array([sum(ratings[t] for t in match[side]) for side in ("red", "blue")])
        counts: Counter = Counter()
        for start in range(0, document["draws"], DRAW_BATCH):
            size = min(DRAW_BATCH, document["draws"] - start)
            mu = np.broadcast_to(predicted, (size, 2))
            scores = model.sample(mu, rng) + model.foul_points(mu.shape, rng)
            margin = scores[:, 0] - scores[:, 1]
            counts.update(red=int(np.count_nonzero(margin > 0)), tie=int(np.count_nonzero(margin == 0)))
        red, tie = counts["red"] / document["draws"], counts["tie"] / document["draws"]
        forecasts.append({"ordinal": match["ordinal"], "p_red_win": red, "p_tie": tie, "p_blue_win": 1 - red - tie})
    return {
        "scope": "qualification totals and independent remaining-match forecasts",
        "scored_matches": len(scored),
        "draws_per_match": document["draws"],
        "seed": document["seed"],
        "teams": [{"number": team, "epa": ratings[team], "pridge": float(ridge[column[team]])} for team in teams],
        "forecasts": forecasts,
    }
