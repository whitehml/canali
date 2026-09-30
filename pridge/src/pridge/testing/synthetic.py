"""Synthetic events for exercising code paths and catching invalid states."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import numpy as np

from pridge.design import RowKey
from pridge.prior import EpaPrior
from pridge.tune import EventSample
from warehouse.client import MatchRow

COMPONENTS = ("autoPoints", "teleopPoints")
DEFAULT_EVENT = uuid.UUID(int=1)


@dataclass(frozen=True, slots=True)
class SyntheticEvent:
    """One event and the values it was generated from.

    `truth` is each team's total contribution and `component_truth` its split by component. The prior's total is the
    sum of its components, and each component response sums to the alliance score, so the partition is exact.
    """

    rows: list[MatchRow]
    prior: EpaPrior
    truth: dict[int, float]
    component_truth: dict[str, dict[int, float]]
    component_responses: dict[str, dict[RowKey, float]]


def match_row(
    ordinal: int,
    alliance: str,
    teams: tuple[int, ...],
    score_no_foul: float,
    *,
    event_id: uuid.UUID = DEFAULT_EVENT,
    event_type: str = "Qualifier",
    level: str = "QUALIFICATION",
    no_shows: tuple[bool, ...] | None = None,
    season: int = 2025,
) -> MatchRow:
    """One alliance row. Both alliances of a match share a `match_id`, derived from the event and ordinal."""
    return MatchRow(
        match_id=uuid.uuid5(event_id, f"match-{ordinal}"),
        event_id=event_id,
        season=season,
        event_code=f"E{event_id.int}",
        event_type=event_type,
        event_date_start=None,
        level=level,
        series=0,
        match_number=ordinal,
        event_match_ordinal=ordinal,
        alliance=alliance,
        score=score_no_foul,
        opponent_score=0.0,
        score_auto=0.0,
        score_no_foul=score_no_foul,
        team_numbers=teams,
        surrogates=(False,) * len(teams),
        no_shows=no_shows if no_shows is not None else (False,) * len(teams),
        dqs=(False,) * len(teams),
    )


def synthetic_event(
    n_matches: int = 24,
    n_teams: int = 12,
    seed: int = 4,
    *,
    event_id: uuid.UUID = DEFAULT_EVENT,
    event_type: str = "Qualifier",
    prior_noise: float = 12.0,
    score_noise: float = 6.0,
    components: tuple[str, ...] = COMPONENTS,
) -> SyntheticEvent:
    """An event of two-robot alliances drawn at random.

    A small `n_matches` gives a design with fewer independent rows than teams. The two noise levels keep residuals and
    leverages away from zero. An empty `components` makes the event total-only.
    """
    rng = np.random.default_rng(seed)
    teams = list(range(1, n_teams + 1))
    names = components or ("total",)
    spread = float(np.sqrt(len(names)))

    truth_by = {name: {t: float(rng.uniform(20, 60) / len(names)) for t in teams} for name in names}
    prior_by = {
        name: {t: truth_by[name][t] + float(rng.normal(0, prior_noise / spread)) for t in teams} for name in names
    }

    rows: list[MatchRow] = []
    responses: dict[str, dict[RowKey, float]] = {name: {} for name in components}
    for ordinal in range(1, n_matches + 1):
        picked = [int(t) for t in rng.choice(teams, size=4, replace=False)]
        for alliance, pair in (("RED", tuple(picked[:2])), ("BLUE", tuple(picked[2:]))):
            values = {
                name: sum(truth_by[name][t] for t in pair) + float(rng.normal(0, score_noise / spread))
                for name in names
            }
            row = match_row(ordinal, alliance, pair, sum(values.values()), event_id=event_id, event_type=event_type)
            rows.append(row)
            for name in components:
                responses[name][(row.match_id, alliance)] = values[name]

    prior = EpaPrior(
        event_id=event_id,
        total={t: sum(prior_by[name][t] for name in names) for t in teams},
        components={t: {name: prior_by[name][t] for name in components} for t in teams},
    )
    return SyntheticEvent(
        rows=rows,
        prior=prior,
        truth={t: sum(truth_by[name][t] for name in names) for t in teams},
        component_truth={name: truth_by[name] for name in components},
        component_responses=responses,
    )


def synthetic_samples(
    n_events: int = 4,
    seed: int = 4,
    *,
    event_type: str = "Qualifier",
    n_matches: int = 24,
    n_teams: int = 12,
    prior_noise: float = 12.0,
    score_noise: float = 6.0,
) -> list[EventSample]:
    """Several independent events of one type, shaped for the tuning and evaluation code."""
    out: list[EventSample] = []
    for i in range(n_events):
        event = synthetic_event(
            n_matches,
            n_teams,
            seed + i,
            event_id=uuid.UUID(int=i + 1),
            event_type=event_type,
            prior_noise=prior_noise,
            score_noise=score_noise,
        )
        out.append(EventSample(f"S{i + 1}", event.rows, event.prior))
    return out
