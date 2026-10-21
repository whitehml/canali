"""Client-side playoff comparisons conditional on a chosen captain, partner and seed."""

from time import monotonic

import numpy as np
from forecast import BATCH_ITERATIONS, BATCH_SECONDS
from pydantic import BaseModel, ConfigDict, Field
from snapshot import EventSnapshot

from sim.event import EventCache, _match_mates, simulate_event
from sim.pick_model import PickField
from sim.playoff import playoff
from sim.selection import alliance_count, alliance_size, select_alliances


class PartnerComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    captain: int = Field(gt=0)
    partners: tuple[int, int]
    seed: int = Field(ge=1, le=8)


def advance_partners(snapshot: EventSnapshot, comparison: PartnerComparison, checkpoint=None):
    field = snapshot.field
    count = alliance_count(snapshot.rule_pack.structure, len(field.predicted))
    chosen = {comparison.captain, *comparison.partners}
    if len(chosen) != 3 or not chosen <= field.predicted.keys():
        raise ValueError("choose a captain and two distinct field partners")
    if comparison.seed > count or alliance_size(field.event_type) != 2:
        raise ValueError("choose a valid seed in an event with two-team playoff alliances")
    if snapshot.progress and snapshot.progress.decided:
        raise ValueError("use a snapshot from before playoff matches were played")
    rng = np.random.default_rng(snapshot.seed)
    n = 0
    tally = np.zeros((2, 3), dtype=np.int64)
    if checkpoint:
        rng.bit_generator.state = checkpoint["rng"]
        n, tally = checkpoint["iterations"], np.array(checkpoint["tally"], dtype=np.int64)
    draw, models = snapshot.draw(), snapshot.models.build()
    progress = snapshot.progress.build() if snapshot.progress else None
    cache = EventCache()
    started = monotonic()
    for _ in range(min(BATCH_ITERATIONS, snapshot.max_iterations - n)):
        drawn_field, schedule = draw(rng)
        # shortcut: reuse full event draws for qualifications; extract a shared qualification helper if this grows slow.
        outcome = simulate_event(drawn_field, schedule, snapshot.rule_pack, models, rng, progress=progress, cache=cache)
        trial_seed = int(rng.integers(0, 2**63))
        mates = cache.partners if cache.partners is not None else _match_mates(schedule)
        for index, partner in enumerate(comparison.partners):
            candidate_rng = np.random.default_rng(trial_seed)
            remaining = tuple(int(t) for t in outcome.standings.teams if t not in (comparison.captain, partner))
            opponents = select_alliances(
                PickField(
                    teams=remaining,
                    rating=np.array([drawn_field.rating(t) for t in remaining]),
                    partners=mates,
                    allied=drawn_field.allied,
                    city=drawn_field.city,
                    team_age=drawn_field.team_age,
                ),
                models.pick,
                count - 1,
                2,
                candidate_rng,
            )
            opponents.insert(comparison.seed - 1, (comparison.captain, partner))

            def play(red, blue, match_rng, ratings=drawn_field):
                total = np.array([[sum(ratings.rating(t) for t in alliance) for alliance in (red, blue)]])
                scores = models.season.score.sample(total, match_rng) + models.season.score.foul_points(
                    total.shape, match_rng
                )
                return float(scores[0, 0]), float(scores[0, 1])

            place = playoff(snapshot.rule_pack.structure, opponents, play, candidate_rng)[comparison.seed - 1]
            tally[index] += (place == 1, place <= 2, place)
        n += 1
        if monotonic() - started >= BATCH_SECONDS:
            break
    complete = n >= snapshot.max_iterations
    return {"iterations": n, "rng": rng.bit_generator.state, "tally": tally.tolist()}, {
        "status": "complete" if complete else "running",
        "iterations": n,
        "result": {
            "iterations": n,
            "partners": [
                {
                    "partner": partner,
                    "p_win": float(tally[i, 0] / n),
                    "p_final": float(tally[i, 1] / n),
                    "mean_place": float(tally[i, 2] / n),
                    "standard_error": float(np.sqrt(np.max(tally[i, :2] / n * (1 - tally[i, :2] / n)) / n)),
                }
                for i, partner in enumerate(comparison.partners)
            ],
        }
        if complete
        else None,
    }
