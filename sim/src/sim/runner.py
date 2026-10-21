"""An event simulated until its advancement chances converge."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypedDict

import numpy as np

from sim.convergence import MIN_ITERATIONS, TOLERANCE, Convergence, tracked_places
from sim.event import EventCache, EventField, EventModels, EventOutcome, EventProgress, Slots, simulate_event
from sim.schedule import Schedule, generated_schedule
from sim.selection import alliance_count
from warehouse.rules.model import RulePack

DrawField = Callable[[np.random.Generator], tuple[EventField, Schedule]]


@dataclass(frozen=True, slots=True)
class TeamSummary:
    """One team's chances at the event.

    Attributes:
        p_attend: Chance the team is in the field.
        mean_rank: Average qualification rank when it attends, None if it never does.
        rank_probabilities: Chance of each qualification rank when it attends, first place first; None if it never does.
        p_captain: Chance it captains an alliance.
        p_selected: Chance a captain picks it.
        p_win: Chance its alliance wins the playoff.
        p_advance: Chance it is offered a slot, None when the event offers none.
        p_advance_first_championship: Chance the slot is FIRST Championship's, None outside regional championships.
    """

    p_attend: float
    mean_rank: float | None
    rank_probabilities: tuple[float, ...] | None
    p_captain: float
    p_selected: float
    p_win: float
    p_advance: float | None
    p_advance_first_championship: float | None


@dataclass(frozen=True, slots=True)
class EventSummary:
    """An event's converged simulation.

    Attributes:
        iterations: Iterations run.
        standard_error: The largest standard error among the tracked advancement chances.
        teams: Each team's chances, for every team that attended at least once.
    """

    iterations: int
    standard_error: float
    teams: dict[int, TeamSummary]


def fixed_field(field: EventField, schedule: Schedule) -> DrawField:
    """The same field on the same schedule every iteration."""
    return lambda _rng: (field, schedule)


def generated_field(field: EventField, teams: Sequence[int], matches_per_team: int) -> DrawField:
    """The same field on a freshly generated schedule every iteration."""
    return lambda rng: (field, generated_schedule(teams, matches_per_team, rng))


def run_event(
    draw: DrawField,
    pack: RulePack,
    models: EventModels,
    rng: np.random.Generator,
    *,
    slots: Slots | None = None,
    progress: EventProgress | None = None,
    tolerance: float = TOLERANCE,
    min_iterations: int = MIN_ITERATIONS,
) -> EventSummary:
    """Simulate the event until ``Convergence`` is met, the positions it tracks set by the first iteration's field."""
    accumulator: EventAccumulator | None = None
    cache = EventCache()
    while accumulator is None or not accumulator.convergence.converged(
        tolerance=tolerance, min_iterations=min_iterations
    ):
        field, schedule = draw(rng)
        outcome = simulate_event(field, schedule, pack, models, rng, slots=slots, progress=progress, cache=cache)
        if accumulator is None:
            teams = np.unique(schedule.stations).tolist()
            accumulator = EventAccumulator(tracked_places(teams, alliance_count(pack.structure, len(teams)), slots))
        accumulator.add(outcome, first_championship=slots is not None and slots.first_championship is not None)
    return accumulator.summary(slots)


class AccumulatorSnapshot(TypedDict):
    """JSON-compatible counts, without retaining individual simulated outcomes."""

    iterations: int
    places: int
    convergence: dict[str, list[int]]
    counters: dict[str, dict[str, int]]
    ranks: dict[str, dict[str, int]]


class EventAccumulator:
    """Event outcome counts that can be checkpointed between Monte Carlo batches."""

    def __init__(self, places: int) -> None:
        self.convergence = Convergence(places)
        self.tally = _Tally()

    def add(self, outcome: EventOutcome, *, first_championship: bool) -> None:
        self.convergence.add(outcome.order)
        self.tally.add(outcome, first_championship=first_championship)

    def summary(self, slots: Slots | None) -> EventSummary:
        return self.tally.summary(self.convergence.standard_error(), slots)

    def snapshot(self) -> AccumulatorSnapshot:
        return {
            "iterations": self.tally.iterations,
            "places": self.convergence.places,
            "convergence": self.convergence.counts(),
            "counters": {
                name: {str(team): count for team, count in getattr(self.tally, name).items()}
                for name in ("attend", "captain", "selected", "win", "advance", "first_championship")
            },
            "ranks": {
                str(team): {str(rank): count for rank, count in counts.items()}
                for team, counts in self.tally.ranks.items()
            },
        }

    @classmethod
    def restore(cls, snapshot: AccumulatorSnapshot) -> EventAccumulator:
        accumulator = cls(snapshot["places"])
        accumulator.convergence.restore_counts(snapshot["iterations"], snapshot["convergence"])
        accumulator.tally.iterations = snapshot["iterations"]
        for name, counts in snapshot["counters"].items():
            if name not in ("attend", "captain", "selected", "win", "advance", "first_championship"):
                raise ValueError(f"unknown event counter {name}")
            setattr(accumulator.tally, name, Counter({int(team): count for team, count in counts.items()}))
        accumulator.tally.ranks = {
            int(team): Counter({int(rank): count for rank, count in counts.items()})
            for team, counts in snapshot["ranks"].items()
        }
        return accumulator


class _Tally:
    """Counts across iterations."""

    def __init__(self) -> None:
        self.iterations = 0
        self.attend: Counter[int] = Counter()
        self.ranks: dict[int, Counter[int]] = {}
        self.captain: Counter[int] = Counter()
        self.selected: Counter[int] = Counter()
        self.win: Counter[int] = Counter()
        self.advance: Counter[int] = Counter()
        self.first_championship: Counter[int] = Counter()

    def add(self, outcome: EventOutcome, *, first_championship: bool) -> None:
        self.iterations += 1
        for rank, team in enumerate(outcome.standings.teams.tolist(), start=1):
            self.attend[team] += 1
            self.ranks.setdefault(team, Counter())[rank] += 1
        for alliance, place in zip(outcome.alliances, outcome.places, strict=True):
            self.captain[alliance[0]] += 1
            self.selected.update(alliance[1:])
            if place == 1:
                self.win.update(alliance)
        if outcome.offers is not None:
            self.advance.update(outcome.offers.advancing)
            self.advance.update(outcome.offers.premier)
            if first_championship:
                self.first_championship.update(outcome.offers.advancing)

    def summary(self, standard_error: float, slots: Slots | None) -> EventSummary:
        n = self.iterations
        teams = {}
        for team, attended in self.attend.items():
            ranks = self.ranks[team]
            teams[team] = TeamSummary(
                p_attend=attended / n,
                mean_rank=sum(r * c for r, c in ranks.items()) / attended,
                rank_probabilities=tuple(ranks[r] / attended for r in range(1, max(ranks) + 1)),
                p_captain=self.captain[team] / n,
                p_selected=self.selected[team] / n,
                p_win=self.win[team] / n,
                p_advance=None if slots is None else self.advance[team] / n,
                p_advance_first_championship=(
                    None if slots is None or slots.first_championship is None else self.first_championship[team] / n
                ),
            )
        return EventSummary(iterations=n, standard_error=standard_error, teams=teams)
