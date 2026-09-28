"""The pipeline against an in-memory warehouse: which scale a season gets, the partition gate, and the live path."""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, cast

import pytest

from epa.partition import resolve_partition
from epa.pipeline import ISOLATED_FAILURE_RATE, init_scale, load_season, replay_live_event, run_season, season_scale
from epa.replay import TAG_MATCH, TAG_POST_EVENT, TAG_PRE_EVENT, TAG_SEASON_START, EpaRow
from epa.scale import SeasonScale
from warehouse.client import CarriedEpaRow, EventRow, MatchRow, Warehouse
from warehouse.rules.model import to_column_name

SEASON = 2025
VERSION = "epa-test"
PREVIOUS = [
    SeasonScale(season=2023, mu=70.0, sigma=56.0, rows=100),
    SeasonScale(season=2024, mu=90.0, sigma=76.0, rows=100),
]
EVENT_DATES = (date(2025, 10, 4), date(2025, 11, 8), date(2025, 12, 13))
ROSTERS = (tuple(range(1, 9)), tuple(range(5, 13)), (1, 2, 3, 4, 9, 10, 11, 12, 13, 14))
QUALS, PLAYOFFS = 36, 4
TOLERANCE = 1.0 - ISOLATED_FAILURE_RATE
PHASE_COMPONENTS = [
    {
        "name": name,
        "column_name": to_column_name(name),
        "level": "alliance",
        "kind": "numeric",
        "is_subtotal": True,
        "is_derived": False,
        "partition_group": "phase",
    }
    for name in ("autoPoints", "teleopPoints")
]


def _season() -> tuple[list[EventRow], list[MatchRow]]:
    """Three events with overlapping rosters, a bracket at each, and two teams new at the third."""
    rng = random.Random(3)
    events: list[EventRow] = []
    rows: list[MatchRow] = []
    for ordinal, (day, roster) in enumerate(zip(EVENT_DATES, ROSTERS, strict=True), start=1):
        event_id = uuid.UUID(int=ordinal)
        events.append(EventRow(event_id, SEASON, f"USPA{ordinal}", day, ordinal, "Qualifier"))
        for number in range(1, QUALS + PLAYOFFS + 1):
            teams = rng.sample(roster, 4)
            for alliance, pair in (("BLUE", teams[2:]), ("RED", teams[:2])):
                rows.append(
                    MatchRow(
                        match_id=uuid.UUID(int=ordinal * 1000 + number),
                        event_id=event_id,
                        season=SEASON,
                        event_code=f"USPA{ordinal}",
                        event_type="Qualifier",
                        event_date_start=day,
                        level="QUALIFICATION" if number <= QUALS else "PLAYOFF",
                        series=0,
                        match_number=number,
                        event_match_ordinal=number,
                        alliance=alliance,
                        score=0.0,
                        opponent_score=0.0,
                        score_auto=0.0,
                        score_no_foul=float(rng.randint(20, 160)),
                        team_numbers=tuple(pair),
                        surrogates=(False, False),
                        no_shows=(False, False),
                        dqs=(False, False),
                    )
                )
    return events, rows


class _Warehouse:
    """The reads the pipeline makes, served from memory, with a batch run's rows standing in for ``derived``."""

    def __init__(
        self,
        events: list[EventRow],
        rows: list[MatchRow],
        *,
        components: Sequence[dict[str, Any]] = (),
        breakdowns: dict[tuple[uuid.UUID, str], dict[str, float]] | None = None,
    ) -> None:
        self._events = events
        self._rows = rows
        self._components = list(components)
        self._breakdowns = breakdowns or {}
        self.batch_rows: list[EpaRow] = []

    def events(self, season: int) -> list[EventRow]:
        return self._events

    def season_matches(self, season: int, *, levels: Sequence[str]) -> list[MatchRow]:
        return [row for row in self._rows if row.level in levels]

    def event_matches(self, event_id: uuid.UUID, *, levels: Sequence[str]) -> list[MatchRow]:
        return [row for row in self._rows if row.event_id == event_id and row.level in levels]

    def fittable_components(self, season: int) -> list[dict[str, Any]]:
        return self._components

    def season_breakdowns(
        self, season: int, columns: Sequence[str], *, levels: Sequence[str]
    ) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
        return self._breakdowns

    def breakdowns(
        self, season: int, event_id: uuid.UUID, columns: Sequence[str]
    ) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
        return self._breakdowns

    def batch_fit_run(self, model: str, season: int, model_version: str) -> uuid.UUID | None:
        return uuid.UUID(int=0) if season == SEASON and self.batch_rows else None

    def carried_epa(
        self, fit_run_id: uuid.UUID, *, before_ordinal: int | None = None, teams: Sequence[int] | None = None
    ) -> dict[int, CarriedEpaRow]:
        ordinal = {event.event_id: event.event_ordinal for event in self._events}
        left = [(ordinal[r.event_id], r) for r in self.batch_rows if r.tag == TAG_POST_EVENT and r.event_id is not None]
        carried: dict[int, CarriedEpaRow] = {}
        for event_ordinal, row in sorted(left, key=lambda pair: pair[0]):
            if before_ordinal is not None and event_ordinal >= before_ordinal:
                continue
            if teams is None or row.team_number in teams:
                carried[row.team_number] = CarriedEpaRow(
                    row.team_number, row.epa_scaled, row.epa_norm, dict(row.components)
                )
        return carried


def _missing(rows: Sequence[MatchRow], fraction: float) -> int:
    """How many alliance-rows to break so the season misses ``fraction`` of them."""
    count = int(len(rows) * fraction)
    assert count >= 1, "the synthetic season is too small to miss this fraction of its rows"
    return count


def _breakdowns(rows: Sequence[MatchRow], *, broken: int) -> dict[tuple[uuid.UUID, str], dict[str, float]]:
    """Each alliance's score split between the two components, the first ``broken`` rows off by ten points."""
    out: dict[tuple[uuid.UUID, str], dict[str, float]] = {}
    for index, row in enumerate(rows):
        auto = row.score_no_foul / 3.0
        teleop = row.score_no_foul - auto + (10.0 if index < broken else 0.0)
        out[(row.match_id, row.alliance)] = {"auto_points": auto, "teleop_points": teleop}
    return out


# ---------------------------------------------------------------------------------------------------------- scales


def test_an_open_season_ignores_its_own_scores_until_it_closes() -> None:
    narrow, wide = [10.0, 20.0] * 100, [1000.0, 2000.0] * 100
    live = season_scale(SEASON, narrow, season_complete=False, previous=PREVIOUS)
    assert live.provisional
    assert live == season_scale(SEASON, wide, season_complete=False, previous=PREVIOUS)
    assert not season_scale(SEASON, narrow, season_complete=True, previous=PREVIOUS).provisional


def test_the_start_scale_is_borrowed_until_its_window_fills() -> None:
    scores = [10.0, 20.0] * 100
    assert init_scale(SEASON, scores[:99], window=100, previous=PREVIOUS).provisional
    assert not init_scale(SEASON, scores, window=100, previous=PREVIOUS).provisional


# ------------------------------------------------------------------------------------------------- partition gate


def test_a_partition_that_misses_a_handful_of_rows_still_loads() -> None:
    events, rows = _season()
    broken = _missing(rows, TOLERANCE / 2)
    warehouse = _Warehouse(events, rows, components=PHASE_COMPONENTS, breakdowns=_breakdowns(rows, broken=broken))
    assert len(load_season(cast(Warehouse, warehouse), SEASON).report.failed_keys) == broken


def test_a_partition_that_misses_a_structured_fraction_is_refused() -> None:
    events, rows = _season()
    warehouse = _Warehouse(
        events, rows, components=PHASE_COMPONENTS, breakdowns=_breakdowns(rows, broken=_missing(rows, TOLERANCE * 10))
    )
    with pytest.raises(ValueError, match="rule-pack bug"):
        load_season(cast(Warehouse, warehouse), SEASON)


def test_strict_refuses_even_a_handful_of_missed_rows() -> None:
    events, rows = _season()
    broken = _missing(rows, TOLERANCE / 2)
    warehouse = _Warehouse(events, rows, components=PHASE_COMPONENTS, breakdowns=_breakdowns(rows, broken=broken))
    with pytest.raises(ValueError, match="strict"):
        load_season(cast(Warehouse, warehouse), SEASON, strict=True)


# ------------------------------------------------------------------------------------------------------- live path


def test_a_live_event_replays_to_what_the_batch_run_wrote_for_it() -> None:
    events, rows = _season()
    warehouse = _Warehouse(events, rows)
    batch = run_season(cast(Warehouse, warehouse), SEASON, match_grain=True).result
    warehouse.batch_rows = batch.rows
    third = events[-1].event_id

    live = replay_live_event(cast(Warehouse, warehouse), SEASON, third, model_version=VERSION)

    def at_third(result_rows: Sequence[EpaRow]) -> list[tuple[int, str, int | None, float, dict[str, float]]]:
        return sorted(
            (r.team_number, r.tag, r.as_of_match, r.epa_scaled, r.components)
            for r in result_rows
            if r.event_id == third and r.tag in (TAG_PRE_EVENT, TAG_MATCH)
        )

    assert at_third(live.rows) == at_third(batch.rows)
    assert sorted(r.team_number for r in live.rows if r.tag == TAG_SEASON_START) == [13, 14]
    assert not any(r.tag == TAG_POST_EVENT for r in live.rows)


def test_a_live_event_refuses_a_seed_fitted_under_a_different_decomposition() -> None:
    events, rows = _season()
    warehouse = _Warehouse(events, rows)
    warehouse.batch_rows = run_season(cast(Warehouse, warehouse), SEASON).result.rows
    phase = resolve_partition(SEASON, ("autoPoints", "teleopPoints"), PHASE_COMPONENTS)

    with pytest.raises(ValueError, match="different decomposition"):
        replay_live_event(
            cast(Warehouse, warehouse), SEASON, events[-1].event_id, model_version=VERSION, partition=phase
        )
