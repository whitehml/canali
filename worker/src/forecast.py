"""Bounded Monte Carlo steps using the shared event simulator and accumulator."""

from dataclasses import asdict
from time import monotonic

import numpy as np
from snapshot import EventSnapshot

from sim.convergence import tracked_places
from sim.event import EventCache, simulate_event
from sim.runner import EventAccumulator
from sim.selection import alliance_count

BATCH_ITERATIONS = 32
BATCH_SECONDS = 2.0


def advance(
    snapshot: EventSnapshot, checkpoint: dict | None, *, batch_size: int = BATCH_ITERATIONS
) -> tuple[dict, dict]:
    """Advance at most one batch; preserve the RNG state so batch boundaries do not change the run."""
    if not 1 <= batch_size <= BATCH_ITERATIONS:
        raise ValueError(f"batch_size must be between 1 and {BATCH_ITERATIONS}")
    rng = np.random.default_rng(snapshot.seed)
    if checkpoint:
        accumulator = EventAccumulator.restore(checkpoint["accumulator"])
        rng.bit_generator.state = checkpoint["rng"]
    else:
        teams = sorted(snapshot.field.predicted)
        places = tracked_places(teams, alliance_count(snapshot.rule_pack.structure, len(teams)), snapshot.slots)
        accumulator = EventAccumulator(places)
    draw = snapshot.draw()
    models = snapshot.models.build()
    progress = None if snapshot.progress is None else snapshot.progress.build()
    cache = EventCache()
    started = monotonic()
    for _ in range(batch_size):
        if accumulator.convergence.converged(tolerance=snapshot.tolerance, min_iterations=snapshot.min_iterations):
            break
        if accumulator.tally.iterations >= snapshot.max_iterations:
            break
        field, schedule = draw(rng)
        outcome = simulate_event(
            field, schedule, snapshot.rule_pack, models, rng, slots=snapshot.slots, progress=progress, cache=cache
        )
        accumulator.add(
            outcome, first_championship=snapshot.slots is not None and snapshot.slots.first_championship is not None
        )
        if monotonic() - started >= BATCH_SECONDS:
            break
    converged = accumulator.convergence.converged(tolerance=snapshot.tolerance, min_iterations=snapshot.min_iterations)
    exhausted = accumulator.tally.iterations >= snapshot.max_iterations
    status = "complete" if converged else "exhausted" if exhausted else "running"
    report = {
        "status": status,
        "iterations": accumulator.tally.iterations,
        "standard_error": accumulator.convergence.standard_error(),
        "result": asdict(accumulator.summary(snapshot.slots)) if converged else None,
    }
    return {"accumulator": accumulator.snapshot(), "rng": rng.bit_generator.state}, report
