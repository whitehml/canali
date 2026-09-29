"""Wiring: load, assert, replay, persist.

Every other module is pure and testable without a database. This one knows the order of operations: resolve the
season's partition against the rule pack, assert it, establish the scales, replay event-sequentially, then write the
rows under one ``fit_run``.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import structlog

from epa.constants import (
    INIT_WINDOW,
    MEASURED_INIT_SCALES,
    MODEL_VERSION,
    SeasonConstants,
    for_season,
)
from epa.corpus import (
    QUALIFICATION_MATCHES,
    RATED_MATCHES,
    RatedMatch,
    event_stream,
    pair_alliances,
    season_breakdowns,
    season_stream,
)
from epa.partition import (
    PartitionReport,
    ResolvedPartition,
    assert_partition,
    partition_from_pack,
    resolve_partition,
)
from epa.replay import TAG_POST_EVENT, EpaRow, ReplayResult, TeamSeed, replay_season
from epa.scale import Scale, compute_scale, provisional_scale
from warehouse.client import Warehouse

MODEL_NAME = "epa"

# Below this the partition misses on a structured fraction of the corpus rather than on a handful of bad rows.
ISOLATED_FAILURE_RATE = 0.99

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SeasonData:
    """One season's corpus, read once, and the partition it was checked against."""

    season: int
    matches: list[RatedMatch]
    breakdowns: dict[tuple[uuid.UUID, str], dict[str, float]]
    partition: ResolvedPartition
    report: PartitionReport
    ordinals: dict[uuid.UUID, int]

    @property
    def alliance_scores(self) -> list[float]:
        return [score for match in self.matches for score in (match.red.score_no_foul, match.blue.score_no_foul)]


@dataclass(frozen=True, slots=True)
class _SeasonHistory:
    """What a season's match stream says about the teams entering one event, and about the season's own spread."""

    before_ordinal: int
    played: dict[int, int]
    last_played: dict[int, date]
    alliance_scores: list[float]


@dataclass(slots=True)
class SeasonRun:
    """One season's replay, with the evidence that it was allowed to run."""

    season: int
    partition: ResolvedPartition
    report: PartitionReport
    season_scale: Scale | None
    result: ReplayResult


def season_partition(warehouse: Warehouse, season: int) -> ResolvedPartition:
    """The decomposition the season's rule pack declares, resolved onto component columns."""
    components = warehouse.fittable_components(season)
    return resolve_partition(season, partition_from_pack(season, components), components)


def load_season(warehouse: Warehouse, season: int, *, strict: bool = False) -> SeasonData:
    """Read a season once and verify that its partition reconstructs the no-foul total."""

    partition = season_partition(warehouse, season)
    breakdowns = season_breakdowns(warehouse, season, partition.columns)
    matches = list(season_stream(warehouse, season))

    rows: list[tuple[object, float, Mapping[str, float]]] = []
    for match in matches:
        for side, alliance in ((match.red, "RED"), (match.blue, "BLUE")):
            values = breakdowns.get((match.match_id, alliance))
            if values is not None:
                rows.append(((match.event_code, match.event_match_ordinal, alliance), side.score_no_foul, values))
    report = assert_partition(partition, rows)

    if not (report.ok or partition.is_total_only):
        isolated = report.rate > ISOLATED_FAILURE_RATE
        if strict or not isolated:
            raise ValueError(
                f"season {season} partition {list(partition.names)} reconciles only {report.rate:.4%} of "
                f"{report.rows} rows. "
                + (
                    "Refusing under strict."
                    if isolated
                    else "This is a rule-pack bug and not bad rows: fit total-only and report it."
                )
            )
        log.warning("epa.partition_failures", season=season, rate=report.rate, failures=len(report.failures))

    return SeasonData(
        season=season,
        matches=matches,
        breakdowns=breakdowns,
        partition=partition,
        report=report,
        ordinals={event.event_id: event.event_ordinal for event in warehouse.events(season)},
    )


def resolve_season_scale(season: int, scores: Sequence[float]) -> Scale | None:
    """The scale of every alliance-row so far."""
    if len(scores) < 2:
        return None
    return compute_scale(scores, season)


def resolve_init_scale(
    season: int,
    scores: Sequence[float],
    *,
    window: int = INIT_WINDOW,
    previous: Sequence[Scale] = (),
) -> Scale:
    """The init scale a carried rating is converted through, frozen once the window fills."""
    values = list(scores)
    if len(values) < window:
        return provisional_scale(season, previous or list(MEASURED_INIT_SCALES.values()))
    return compute_scale(values, season, window=window)


def run_season(
    warehouse: Warehouse,
    season: int,
    *,
    previous_norm: Mapping[int, float] | None = None,
    second_norm: Mapping[int, float] | None = None,
    strict: bool = False,
    constants_override: SeasonConstants | None = None,
    data: SeasonData | None = None,
    compute_norm: bool = True,
) -> SeasonRun:
    """Prepare, verify and replay one season."""

    loaded = data or load_season(warehouse, season, strict=strict)
    constants = constants_override or for_season(season)
    season_scale = resolve_season_scale(season, loaded.alliance_scores)
    init_scale = resolve_init_scale(season, loaded.alliance_scores, window=constants.init_window)

    result = replay_season(
        loaded.matches,
        constants=constants,
        partition=loaded.partition,
        season_scale=season_scale,
        init_scale=init_scale,
        layoff_boost=constants.layoff_boost,
        previous_norm=previous_norm,
        second_norm=second_norm,
        breakdowns=loaded.breakdowns,
        event_ordinals=loaded.ordinals,
        compute_norm=compute_norm,
    )
    return SeasonRun(
        season=season, partition=loaded.partition, report=loaded.report, season_scale=season_scale, result=result
    )


def replay_live_event(
    warehouse: Warehouse,
    season: int,
    event_id: uuid.UUID,
    *,
    model_version: str,
    partition: ResolvedPartition | None = None,
    season_scale: Scale | None = None,
    init_scale: Scale | None = None,
) -> ReplayResult:
    """Replay one in-progress event, continuing the batch run named by ``model_version``.

    The partition and both scales are derived unless given.
    """

    batch_run = warehouse.batch_fit_run(MODEL_NAME, season, model_version)
    if batch_run is None:
        raise ValueError(
            f"no completed {model_version} batch run for season {season}; "
            f"a live event continues a batch run and cannot be rated without one"
        )

    ordinals = {event.event_id: event.event_ordinal for event in warehouse.events(season)}
    if event_id not in ordinals:
        raise ValueError(f"event {event_id} is not in season {season}")
    matches = list(event_stream(warehouse, event_id))
    resolved = partition if partition is not None else season_partition(warehouse, season)
    constants = for_season(season)
    history = _season_history(warehouse, season, ordinals[event_id])
    previous_norm, second_norm = _prior_norms(warehouse, season, model_version)

    result = replay_season(
        matches,
        constants=constants,
        partition=resolved,
        season_scale=season_scale or resolve_season_scale(season, history.alliance_scores),
        init_scale=init_scale or resolve_init_scale(season, history.alliance_scores, window=constants.init_window),
        layoff_boost=constants.layoff_boost,
        previous_norm=previous_norm,
        second_norm=second_norm,
        breakdowns=warehouse.breakdowns(season, event_id, resolved.columns),
        event_ordinals=ordinals,
        seed=_seed_from_batch(
            warehouse,
            season,
            matches=matches,
            batch_run=batch_run,
            history=history,
            partition=resolved,
        ),
        compute_norm=False,
    )
    result.rows = [row for row in result.rows if row.tag != TAG_POST_EVENT]
    return result


def _seed_from_batch(
    warehouse: Warehouse,
    season: int,
    *,
    matches: Sequence[RatedMatch],
    batch_run: uuid.UUID,
    history: _SeasonHistory,
    partition: ResolvedPartition,
) -> dict[int, TeamSeed]:
    """Each playing team's rating as the batch run left it at an earlier event.

    A team the run has not seen this season is absent, so the replay starts it from carryover.
    """
    playing = sorted({team for match in matches for team in (*match.red.teams, *match.blue.teams)})
    carried = warehouse.carried_epa(batch_run, before_ordinal=history.before_ordinal, teams=playing)

    expected = set(partition.series)
    seed: dict[int, TeamSeed] = {}
    for team, row in carried.items():
        if set(row.components) != expected:
            raise ValueError(
                f"season {season} team {team} carries series {sorted(row.components)} where the partition declares "
                f"{sorted(expected)}; the batch run was fitted under a different decomposition"
            )
        seed[team] = TeamSeed(
            series=dict(row.components),
            played=history.played.get(team, 0),
            last_played=history.last_played.get(team),
        )
    return seed


def _prior_norms(warehouse: Warehouse, season: int, model_version: str) -> tuple[dict[int, float], dict[int, float]]:
    """The two previous seasons' final norm ratings, as carryover splits a team's history between them."""
    carried: list[dict[int, float]] = []
    for prior in (season - 1, season - 2):
        run = warehouse.batch_fit_run(MODEL_NAME, prior, model_version)
        rows = {} if run is None else warehouse.carried_epa(run)
        carried.append({team: row.epa_norm for team, row in rows.items() if row.epa_norm is not None})
    return carried[0], carried[1]


def _season_history(warehouse: Warehouse, season: int, before_ordinal: int) -> _SeasonHistory:
    """One pass over the season's matches, for the two things no rating row carries and for the scales.

    The K schedule ramps on qualification matches played and the layoff boost is measured from the date a team last
    competed, both counted at earlier events only.
    """
    ordinals = {event.event_id: event.event_ordinal for event in warehouse.events(season)}
    played: Counter[int] = Counter()
    last_played: dict[int, date] = {}
    scores: list[float] = []
    for match in pair_alliances(warehouse.season_matches(season, levels=RATED_MATCHES)):
        scores.extend((match.red.score_no_foul, match.blue.score_no_foul))
        if ordinals.get(match.event_id, before_ordinal) >= before_ordinal:
            continue
        teams = (*match.red.teams, *match.blue.teams)
        if match.level in QUALIFICATION_MATCHES:
            played.update(teams)
        if match.event_date is not None:
            last_played.update(dict.fromkeys(teams, match.event_date))
    return _SeasonHistory(
        before_ordinal=before_ordinal, played=dict(played), last_played=last_played, alliance_scores=scores
    )


def persist(
    warehouse: Warehouse,
    rows: Sequence[EpaRow],
    *,
    season: int,
    scope: str,
    event_id: uuid.UUID | None = None,
    model_version: str = MODEL_VERSION,
    notes: dict[str, object] | None = None,
) -> uuid.UUID:
    """Write a replay's rows under one ``fit_run``, which on finishing replaces the run it supersedes."""
    run_id = warehouse.start_fit_run(
        model=MODEL_NAME,
        model_version=model_version,
        scope=scope,
        season=season,
        event_id=event_id,
        notes=notes,
    )
    warehouse.write_team_epa(
        run_id,
        model_version,
        [
            {
                "season": row.season,
                "team_number": row.team_number,
                "event_id": row.event_id,
                "tag": row.tag,
                "as_of_match": row.as_of_match,
                "epa_norm": row.epa_norm,
                "epa_scaled": row.epa_scaled,
                "scale_provisional": row.scale_provisional,
                "components": row.components,
            }
            for row in rows
        ],
    )
    warehouse.finish_fit_run(run_id)
    return run_id
