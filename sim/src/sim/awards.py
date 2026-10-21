"""Judged awards after alliance selection."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from sim.award_model import INSPIRE, MODELLED_AWARDS, AwardField, AwardModel, HistoryFeatures, model_for
from warehouse.tier import EventTier

JUDGES_CHOICE = "JudgesChoice"

# Inspire places by field size: up to the first bound 1st only, up to the second 1st and 2nd, past it 1st to 3rd.
INSPIRE_PLACE_BOUNDS = (10, 20)


def inspire_places(n_teams: int) -> int:
    """How many Inspire places an event of ``n_teams`` gives."""
    return 1 + sum(n_teams > bound for bound in INSPIRE_PLACE_BOUNDS)


def draw_awards(
    field: AwardField,
    models: Sequence[AwardModel],
    stage: EventTier,
    rng: np.random.Generator,
    *,
    inspire_won_before: frozenset[int] = frozenset(),
    history_features: HistoryFeatures | None = None,
) -> dict[str, tuple[int, ...]]:
    """Each award's placed teams, 1st first.

    Inspire is placed first, then each other award's winner in turn from teams not yet placed, then Judges' Choice
    uniformly from those left. At REGULAR events a team that has already won Inspire this season can't win 1st again,
    but can place 2nd or 3rd.
    """
    placed: set[int] = set()
    awards: dict[str, tuple[int, ...]] = {}
    for award in MODELLED_AWARDS:
        model = model_for(models, award, stage)
        pool = [t for t in field.teams if t not in placed]
        places = inspire_places(len(field.teams)) if award == INSPIRE else 1
        winners: list[int] = []
        for place in range(1, places + 1):
            locked = (
                inspire_won_before if award == INSPIRE and stage is EventTier.REGULAR and place == 1 else frozenset()
            )
            eligible = [t for t in pool if t not in locked]
            if not eligible:
                break
            features = field.features(award, eligible, history_features=history_features)
            team = eligible[rng.choice(len(eligible), p=model.probabilities(features))]
            winners.append(team)
            pool.remove(team)
        placed.update(winners)
        awards[award] = tuple(winners)
    pool = [t for t in field.teams if t not in placed]
    awards[JUDGES_CHOICE] = (pool[rng.integers(len(pool))],) if pool else ()
    return awards
