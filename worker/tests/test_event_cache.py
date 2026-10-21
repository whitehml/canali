"""Prepared calculations must follow changed inputs without changing draws or outcomes."""

from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest
from snapshot import EventSnapshot

from sim.event import EventCache, simulate_event


@pytest.fixture
def snapshot():
    return EventSnapshot.model_validate_json((Path(__file__).parents[1] / "fixtures/forecast.json").read_text())


@pytest.mark.parametrize("stage", ["generated", "qualifications", "playoffs"])
def test_cached_draws_match_fresh_draws_and_random_state(snapshot, stage):
    if stage == "generated":
        snapshot = snapshot.model_copy(update={"schedule": None, "progress": None})
    elif stage == "playoffs":
        snapshot = snapshot.model_copy(update={"progress": snapshot.progress.model_copy(update={"played": [True] * 6})})
    before = snapshot.model_dump_json()
    draw, models = snapshot.draw(), snapshot.models.build()
    progress = snapshot.progress.build() if snapshot.progress else None
    rng, reference_rng = np.random.default_rng(21), np.random.default_rng(21)
    cache = EventCache()
    for _ in range(24):
        field, schedule = draw(rng)
        ref_field, ref_schedule = draw(reference_rng)
        actual = simulate_event(field, schedule, snapshot.rule_pack, models, rng, progress=progress, cache=cache)
        expected = simulate_event(ref_field, ref_schedule, snapshot.rule_pack, models, reference_rng, progress=progress)
        np.testing.assert_equal(asdict(actual), asdict(expected))
        assert rng.bit_generator.state == reference_rng.bit_generator.state
    assert snapshot.model_dump_json() == before


def test_reused_cache_follows_field_schedule_and_completed_result_corrections(snapshot):
    field, schedule = snapshot.draw()(np.random.default_rng(0))
    models = snapshot.models.build()
    progress = snapshot.progress.model_copy(update={"played": [True] * 6}).build()
    cache = EventCache()
    changed_field = replace(
        field,
        predicted={
            t: {c: v * (2 if t % 2 else 0.5) for c, v in values.items()} for t, values in field.predicted.items()
        },
        history={t: {"Inspire": float(t), "Think": 0.5, "InspirePlaced": 2.0} for t in field.predicted},
        rookie=frozenset(field.predicted),
    )
    changed_schedule = replace(
        schedule, stations=schedule.stations[::-1].copy(), surrogate=schedule.surrogate[::-1].copy()
    )
    changed_progress = replace(
        progress,
        results=replace(
            progress.results,
            components={name: values + 100 for name, values in progress.results.components.items()},
        ),
    )
    for current_field, current_schedule, current_progress in [
        (field, schedule, progress),
        (changed_field, schedule, progress),
        (changed_field, changed_schedule, progress),
        (changed_field, changed_schedule, changed_progress),
        (field, schedule, None),
    ]:
        rng, reference_rng = np.random.default_rng(42), np.random.default_rng(42)
        actual = simulate_event(
            current_field, current_schedule, snapshot.rule_pack, models, rng, progress=current_progress, cache=cache
        )
        expected = simulate_event(
            current_field, current_schedule, snapshot.rule_pack, models, reference_rng, progress=current_progress
        )
        np.testing.assert_equal(asdict(actual), asdict(expected))
        assert rng.bit_generator.state == reference_rng.bit_generator.state
