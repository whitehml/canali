"""Plays a season back match by match, moving each team's rating as its results come in.

A rating is recorded at four moments: where a team started the season, where it stood entering an event, after each
match it played, and where it finished the event.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date

from epa.constants import SeasonConstants
from epa.corpus import RatedMatch
from epa.evaluate import Prediction
from epa.model import update_for_match
from epa.norm import NormMap, build_norm_map
from epa.partition import response_values, series
from epa.scale import Carryover, Initialization, LayoffBoost, Scale, carry_forward
from warehouse.rules.partition import ResolvedPartition

TAG_SEASON_START = "season_start"
TAG_PRE_EVENT = "pre_event"
TAG_POST_EVENT = "post_event"
TAG_MATCH = "match"


@dataclass(slots=True)
class TeamState:
    """One team's live rating during a season, in this season's point units."""

    team_number: int
    series: dict[str, float]
    played: int = 0

    @property
    def total(self) -> float:
        return sum(self.series.values())


@dataclass(frozen=True, slots=True)
class TeamSeed:
    """A rating a team already holds, for a replay that resumes rather than starts."""

    series: dict[str, float]
    played: int
    last_played: date | None = None


@dataclass(frozen=True, slots=True)
class EpaRow:
    """One rating as a ``derived.team_epa`` row."""

    season: int
    team_number: int
    tag: str
    event_id: uuid.UUID | None
    as_of_match: int | None
    epa_scaled: float
    scale_provisional: bool
    components: dict[str, float]

    epa_norm: float | None = None
    """Filled in after the replay: a percentile cannot exist before there is a field to rank against."""


@dataclass(slots=True)
class ReplayResult:
    season: int
    season_scale: Scale | None
    partition: ResolvedPartition
    rows: list[EpaRow] = field(default_factory=list)
    final_norm: dict[int, float] = field(default_factory=dict)
    norm_map: NormMap | None = None
    matches: int = 0
    teams: int = 0
    skipped_missing_breakdown: int = 0
    predictions: list[Prediction] = field(default_factory=list)
    """One per alliance-row, recorded before the match updates anything."""


def apply_norm_map(result: ReplayResult, finals: Mapping[int, float]) -> None:
    """Rank the season's teams against each other and stamp every row."""
    if not finals:
        return
    try:
        norm_map = build_norm_map(list(finals.values()))
    except ValueError:
        return
    result.norm_map = norm_map
    result.final_norm = {team: norm_map(epa) for team, epa in sorted(finals.items())}
    result.rows = [replace(row, epa_norm=norm_map(row.epa_scaled)) for row in result.rows]


def _initial_series(init: Initialization, partition: ResolvedPartition) -> dict[str, float]:
    names = series(partition)
    return dict.fromkeys(names, init.scaled / len(names))


def _apply_layoff(
    team: TeamState,
    boost: LayoffBoost | None,
    match: RatedMatch,
    last_played: date | None,
    init_scale: Scale,
) -> float:
    """Credit a team for the time it spent improving between competitions, returning the points added."""
    if boost is None or match.event_date is None:
        return 0.0
    gap = None if last_played is None else (match.event_date - last_played).days
    points = boost.points(gap, init_scale)
    names = list(team.series)
    if not points or not names:
        return 0.0
    weights = [max(team.series[name], 0.0) for name in names]
    mass = sum(weights)
    shares = [w / mass for w in weights] if mass > 0 else [1.0 / len(names)] * len(names)
    for name, share in zip(names, shares, strict=True):
        team.series[name] += points * share
    return points


def _row(
    team: TeamState,
    season: int,
    tag: str,
    event_id: uuid.UUID | None,
    as_of_match: int | None,
    *,
    provisional: bool,
) -> EpaRow:
    return EpaRow(
        season=season,
        team_number=team.team_number,
        tag=tag,
        event_id=event_id,
        as_of_match=as_of_match,
        epa_scaled=team.total,
        scale_provisional=provisional,
        components=dict(team.series),
    )


def _breakdown_values(
    match: RatedMatch,
    partition: ResolvedPartition,
    breakdowns: Mapping[tuple[uuid.UUID, str], dict[str, float]] | None,
) -> tuple[Mapping[str, float] | None, Mapping[str, float] | None] | None:
    """Both alliances' component values, or None when the match cannot be fitted."""
    if partition.is_total_only:
        return (None, None)
    red = (breakdowns or {}).get((match.match_id, "RED"))
    blue = (breakdowns or {}).get((match.match_id, "BLUE"))
    return None if red is None or blue is None else (red, blue)


def _predictions_for_match(
    match: RatedMatch,
    state: Mapping[int, TeamState],
    event_ordinal: int,
) -> list[Prediction]:
    """Both alliances' pre-match predictions, called before any delta is applied."""
    red_hat = sum(state[t].total for t in match.red.teams)
    blue_hat = sum(state[t].total for t in match.blue.teams)
    played = {t: state[t].played for t in (*match.red.teams, *match.blue.teams)}
    return [
        Prediction(
            season=match.season,
            team_numbers=own.teams,
            predicted=own_hat,
            actual=own.score_no_foul,
            matches_played=min(played[t] for t in own.teams),
            opponent_predicted=opponent_hat,
            event_ordinal=event_ordinal,
            event_type=match.event_type,
            is_elimination=match.is_elimination,
        )
        for own, opponent, own_hat, opponent_hat in (
            (match.red, match.blue, red_hat, blue_hat),
            (match.blue, match.red, blue_hat, red_hat),
        )
    ]


def _deltas_for_match(
    match: RatedMatch,
    *,
    state: Mapping[int, TeamState],
    constants: SeasonConstants,
    partition: ResolvedPartition,
    red_values: Mapping[str, float] | None,
    blue_values: Mapping[str, float] | None,
) -> dict[int, dict[str, float]]:
    """Every team's per-series delta from one match."""
    teams = (*match.red.teams, *match.blue.teams)
    weight = constants.elim_weight if match.is_elimination else 1.0
    if not weight:
        return {team: {} for team in teams}
    snapshot = {team: dict(state[team].series) for team in teams}
    responses = {
        "RED": response_values(partition, match.red.score_no_foul, red_values),
        "BLUE": response_values(partition, match.blue.score_no_foul, blue_values),
    }
    pending: dict[int, dict[str, float]] = {team: {} for team in teams}

    for own, opponent, own_key, opponent_key in (
        (match.red, match.blue, "RED", "BLUE"),
        (match.blue, match.red, "BLUE", "RED"),
    ):
        played = [state[team].played for team in own.teams]
        average_played = int(sum(played) / len(played)) if played else 0
        k = constants.k.at(average_played)
        m = constants.m.at(average_played)
        for name in series(partition):
            update = update_for_match(
                {team: snapshot[team][name] for team in teams},
                own_teams=own.teams,
                opponent_teams=opponent.teams,
                own_score=responses[own_key][name],
                opponent_score=responses[opponent_key][name],
                k=k,
                m=m,
            )
            for team, delta in update.deltas.items():
                pending[team][name] = pending[team].get(name, 0.0) + delta * weight
    return pending


def _apply_deltas(
    state: Mapping[int, TeamState],
    pending: Mapping[int, Mapping[str, float]],
    match: RatedMatch,
    last_played: dict[int, date | None],
) -> None:
    """Move every team this match touched, and record when it last played."""
    for team_number, deltas in pending.items():
        team = state[team_number]
        for name, delta in deltas.items():
            team.series[name] += delta
        team.played += 0 if match.is_elimination else 1
        last_played[team_number] = match.event_date


def replay_season(
    matches: Iterable[RatedMatch],
    *,
    constants: SeasonConstants,
    partition: ResolvedPartition,
    season_scale: Scale | None,
    init_scale: Scale,
    layoff_boost: LayoffBoost | None = None,
    previous_norm: Mapping[int, float] | None = None,
    second_norm: Mapping[int, float] | None = None,
    breakdowns: Mapping[tuple[uuid.UUID, str], dict[str, float]] | None = None,
    event_ordinals: Mapping[uuid.UUID, int] | None = None,
    seed: Mapping[int, TeamSeed] | None = None,
    compute_norm: bool = True,
) -> ReplayResult:
    """Replay one season, returning every row it produced.

    A seeded team enters holding the rating it is given rather than one carried forward, so it takes no season-start
    row. It enters its first event of the stream like any other team, layoff boost and entry row included.
    """

    carryover: Carryover = constants.carryover or Carryover(year_one_weight=1.0, mean_reversion=1.0)
    previous = previous_norm or {}
    second = second_norm or {}
    ordinals = event_ordinals or {}
    result = ReplayResult(season=constants.season, season_scale=season_scale, partition=partition)
    seeded = seed or {}
    state: dict[int, TeamState] = {
        number: TeamState(team_number=number, series=dict(start.series), played=start.played)
        for number, start in seeded.items()
    }
    last_played: dict[int, date | None] = {
        number: start.last_played for number, start in seeded.items() if start.last_played is not None
    }

    def row(team: TeamState, tag: str, event_id: uuid.UUID | None, as_of_match: int | None) -> EpaRow:
        return _row(team, constants.season, tag, event_id, as_of_match, provisional=init_scale.provisional)

    def ensure(team_number: int) -> TeamState:
        existing = state.get(team_number)
        if existing is not None:
            return existing
        init = carry_forward(
            previous_norm=previous.get(team_number),
            second_norm=second.get(team_number),
            carryover=carryover,
            init_scale=init_scale,
        )
        fresh = TeamState(team_number=team_number, series=_initial_series(init, partition))
        state[team_number] = fresh
        result.rows.append(row(fresh, TAG_SEASON_START, None, None))
        return fresh

    current_event: uuid.UUID | None = None
    event_roster: set[int] = set()
    last_ordinal = 0

    def close_event() -> None:
        if current_event is None:
            return
        for team_number in sorted(event_roster):
            result.rows.append(row(state[team_number], TAG_POST_EVENT, current_event, last_ordinal))

    for match in matches:
        if match.event_id != current_event:
            close_event()
            current_event = match.event_id
            event_roster = set()
        last_ordinal = match.event_match_ordinal

        for team_number in sorted((*match.red.teams, *match.blue.teams)):
            ensure(team_number)
            if team_number not in event_roster:
                event_roster.add(team_number)
                _apply_layoff(state[team_number], layoff_boost, match, last_played.get(team_number), init_scale)
                result.rows.append(row(state[team_number], TAG_PRE_EVENT, match.event_id, 0))

        values = _breakdown_values(match, partition, breakdowns)
        if values is None:
            result.skipped_missing_breakdown += 1
            continue
        red_values, blue_values = values

        result.predictions.extend(_predictions_for_match(match, state, ordinals.get(match.event_id, 0)))
        pending = _deltas_for_match(
            match,
            state=state,
            constants=constants,
            partition=partition,
            red_values=red_values,
            blue_values=blue_values,
        )

        _apply_deltas(state, pending, match, last_played)
        for team_number in sorted(pending):
            result.rows.append(row(state[team_number], TAG_MATCH, match.event_id, match.event_match_ordinal))

        result.matches += 1

    close_event()
    result.teams = len(state)
    apply_norm_map(result, {team: st.total for team, st in state.items()} if compute_norm else {})
    return result
