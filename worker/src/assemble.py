"""Build event snapshots from revision-linked component ratings and official progress."""

import json

from components import component_value
from compute import integer, number
from forecast_store import save_input
from playoff_input import current_playoff, scored_playoffs
from pydantic import Field, PositiveInt, model_validator
from season import Config, Event, history_for, priors, read_priors, replay, state_for
from snapshot import EventSnapshot, InputModel, ModelsInput, SeasonPrior

from epa.scale import Scale
from sim.award_model import AWARD_NAMES, INSPIRE_PLACED
from sim.event import Slots


class ContextInput(InputModel):
    allied: dict[PositiveInt, frozenset[PositiveInt]]
    history: dict[PositiveInt, dict[str, float]]
    inspire_won_before: frozenset[PositiveInt]
    slots: Slots | None

    @model_validator(mode="after")
    def check_history(self):
        known = {*AWARD_NAMES.values(), INSPIRE_PLACED}
        if any(name not in known or value < 0 for awards in self.history.values() for name, value in awards.items()):
            raise ValueError("award history needs known award names and nonnegative values")
        return self


class AssemblyInput(InputModel):
    models: ModelsInput
    context: ContextInput
    selection_published: bool
    alliances: dict | None = None
    playoff_schedule: dict | None = None
    playoff_scores: dict | None = None
    through: int | None = Field(default=None, ge=0, le=1000)
    seed: int = Field(default=42, ge=0)
    tolerance: float = Field(default=0.01, gt=0, le=0.5)
    min_iterations: int = Field(default=1000, ge=1, le=100_000)
    max_iterations: int = Field(default=10_000, ge=1, le=100_000)
    matches_per_team: int = Field(default=5, ge=1, le=20)


def assemble(
    event: Event, source: dict, config: Config, scale: Scale, *, history: dict, options: AssemblyInput
) -> EventSnapshot:
    pack = config.rule_pack
    if pack is None:
        raise ValueError("forecast assembly requires component EPA and a rule pack")
    teams = set(event.roster)
    context = options.context
    if (set(context.allied) | set(context.history) | set(context.inspire_won_before)) - teams:
        raise ValueError("context contains teams outside the event roster")
    scored = sum(match.red_score is not None for match in event.matches)
    cutoff = scored if options.through is None else options.through
    if cutoff > scored:
        raise ValueError("through exceeds the number of scored qualification matches")
    alliances, playoffs, playoff_breakdowns, decided = current_playoff(event, pack, options)
    truncated = event.model_copy(deep=True)
    retained = set()
    for match in truncated.matches:
        if match.red_score is not None and len(retained) < cutoff:
            retained.add(match.ordinal)
        else:
            match.red_score = match.blue_score = None
            match.red_components = match.blue_components = None
    ratings = {row["number"]: row["components"] for row in priors(event, pack.season, history, config, scale)}
    ratings.update(
        {
            row["team"]: row["components"]
            for row in replay(
                truncated, pack.season, history, config, scale, playoffs=playoffs, playoff_breakdowns=playoff_breakdowns
            )
        }
    )
    roster = {row["teamNumber"]: row for row in source["teams"]["teams"]}
    ages, rookies = {}, set()
    for team in teams:
        year = roster[team].get("rookieYear")
        if year:
            integer(year, "rookie year", minimum=1)
            if year > pack.season:
                raise ValueError("rookie year exceeds the event season")
            ages[team] = float(pack.season - year)
            if year == pack.season:
                rookies.add(team)
    schedule, progress = published_progress(event, source, retained, options, pack)
    if alliances is not None:
        progress["alliances"] = alliances
        progress["decided"] = [
            {"series": series, "game": game, "winner": winner} for (series, game), winner in decided.items()
        ]
    last_match = {"level": "QUALIFICATION", "number": max(retained)} if retained else None
    if playoffs:
        row, series, key, _ = list(scored_playoffs(options.playoff_schedule))[-1]
        last_match = {"level": row["tournamentLevel"], "number": key[1], "series": series}
    return EventSnapshot(
        rule_pack=pack,
        field={
            "event_type": event.event_type,
            "predicted": ratings,
            "allied": context.allied,
            "city": {team: roster[team].get("city") for team in teams},
            "team_age": ages,
            "history": context.history,
            "rookie": rookies,
            "inspire_won_before": context.inspire_won_before,
        },
        models=options.models,
        schedule=schedule,
        progress=progress,
        slots=context.slots,
        as_of_match=cutoff + len(playoffs),
        last_match=last_match,
        **options.model_dump(include={"seed", "tolerance", "min_iterations", "max_iterations", "matches_per_team"}),
    )


def published_progress(event: Event, source: dict, retained: set[int], options: AssemblyInput, pack):
    if not event.matches:
        return None, None
    raw_matches = {row["matchNumber"]: row for row in source["schedule"]["schedule"]}
    raw_scores = {
        (row["matchNumber"], side["alliance"].lower()): side
        for row in source["scores"]["matchScores"]
        for side in row["alliances"]
    }
    stations, surrogates, played, scores, dqs = [], [], [], [], []
    bonus = {name: [] for name in options.models.rp_curves}
    components = {name: [] for name in options.models.tiebreaker_shares}
    for match in event.matches:
        raw = raw_matches[match.ordinal]
        by_station = {slot["station"]: slot for slot in raw["teams"]}
        slots = [[by_station[f"{side}{index}"] for index in (1, 2)] for side in ("Red", "Blue")]
        stations.append([[slot["teamNumber"] for slot in side] for side in slots])
        surrogates.append([[bool(slot.get("surrogate", False)) for slot in side] for side in slots])
        is_played = match.ordinal in retained
        played.append(is_played)
        scores.append(
            [number(raw[f"score{side}Final"], "published score") if is_played else 0.0 for side in ("Red", "Blue")]
        )
        dqs.append([[bool(slot.get("dq", False)) if is_played else False for slot in side] for side in slots])
        for name, rows in bonus.items():
            rows.append(
                [
                    bool(component_value(pack, name, raw_scores[match.ordinal, side])) if is_played else False
                    for side in ("red", "blue")
                ]
            )
        for name, rows in components.items():
            rows.append(
                [
                    component_value(pack, name, raw_scores[match.ordinal, side]) if is_played else 0.0
                    for side in ("red", "blue")
                ]
            )
    return {"stations": stations, "surrogate": surrogates}, {
        "played": played,
        "score": scores,
        "bonus": bonus,
        "components": components,
        "dq": dqs,
    }


async def handle(db, request, season: int, code: str) -> tuple[dict, int]:
    if request.method != "POST":
        return {"error": "method not allowed"}, 405
    body = await request.text()
    if len(body.encode()) > 512_000:
        return {"error": "input exceeds 512000 bytes"}, 413
    options = AssemblyInput.model_validate_json(body)
    report, status = await read_priors(db, season, code)
    if status != 200:
        return report, status
    dependency = SeasonPrior.model_validate(report["season_prior"])
    state = await state_for(db, season)
    if state.revision != dependency.revision:
        return {"error": "season changed; retry assembly"}, 409
    row = (
        await db.prepare("SELECT document, source FROM season_event WHERE season = ? AND event_code = ?")
        .bind(season, code)
        .first()
    )
    event = Event.model_validate_json(row.document)
    snapshot = assemble(
        event,
        json.loads(row.source),
        Config.model_validate_json(state.config),
        Scale(**json.loads(state.init_scale)),
        history=await history_for(db, season, event),
        options=options,
    )
    snapshot.season_prior = dependency
    saved, status = await save_input(db, f"{season}-{code}", snapshot)
    if status == 200:
        saved["snapshot"] = snapshot.model_dump(mode="json")
    return saved, status
