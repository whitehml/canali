"""One event played through: qualification, alliance selection, the playoff, awards and advancement.

Every rating the event uses is the team's prediction as of the beginning of the simulation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from sim.advancement import Offers, TeamPoints, advancement_order, advancement_points, offers
from sim.award_model import AUTO_PREFIX, AwardField, AwardModel, HistoryFeatures
from sim.awards import draw_awards
from sim.match import SeasonModels, draw_matches
from sim.pick_model import PickField, PickModel
from sim.playoff import Alliance, playoff
from sim.schedule import Schedule
from sim.selection import ALLIANCE_SIZE, alliance_count, alliance_size, select_alliances
from sim.standings import MatchResults, RankingRules, Standings, standings
from warehouse.rules.model import RulePack
from warehouse.tier import EventTier, tier_for

Floats = npt.NDArray[np.float64]

AWARD_STAGES = frozenset({EventTier.REGULAR, EventTier.RCMP})

NO_FOUL = "preFoulTotal"
AUTO = "autoPoints"


@dataclass(frozen=True, slots=True)
class EventField:
    """An event's teams and what is known of them before it starts.

    Attributes:
        event_type: The event's type.
        predicted: Each team's predicted points per component, keyed by team, then column name.
        allied: Each team's alliance partners at earlier events this season.
        city: Each team's city, None when unknown.
        team_age: Each team's seasons since its rookie year.
        history: Each team's decayed award placings, as ``AwardField`` takes them.
        rookie: Teams in their rookie season.
        inspire_won_before: Teams that have won Inspire at an earlier event this season.
    """

    event_type: str | None
    predicted: Mapping[int, Mapping[str, float]]
    allied: Mapping[int, frozenset[int]]
    city: Mapping[int, str | None]
    team_age: Mapping[int, float]
    history: Mapping[int, Mapping[str, float]]
    rookie: frozenset[int]
    inspire_won_before: frozenset[int] = frozenset()

    def rating(self, team: int) -> float:
        return float(sum(self.predicted[team].values()))

    def auto_rating(self, team: int) -> float:
        return float(sum(v for k, v in self.predicted[team].items() if k.startswith(AUTO_PREFIX)))


@dataclass(frozen=True, slots=True)
class Slots:
    """The slots an event offers.

    Attributes:
        count: Slots offered.
        already_advanced: Teams that have already advanced to the level the slots lead to.
        first_championship: Of ``count``, the slots to FIRST Championship at a regional championship, else None.
    """

    count: int
    already_advanced: frozenset[int] = frozenset()
    first_championship: int | None = None


@dataclass(frozen=True, slots=True)
class EventProgress:
    """What an event has published so far.

    Attributes:
        played: Qualification matches already played, shaped ``(match,)``.
        results: Their results, shaped as the schedule's; rows of unplayed matches are ignored.
        alliances: Playoff alliances in seed order, captain first, or None before selection.
        decided: The winning seed of each playoff match played, keyed as ``playoff`` takes it,
            or None before the playoff.
    """

    played: npt.NDArray[np.bool_]
    results: MatchResults
    alliances: Sequence[Alliance] | None = None
    decided: Mapping[tuple[int, int], int] | None = None

    def merged(self, drawn: MatchResults) -> MatchResults:
        """``drawn`` with every played match replaced by its published result."""
        if self.played.shape != drawn.score.shape[:1]:
            raise ValueError(f"{self.played.shape[0]} matches published, {drawn.score.shape[0]} drawn")
        missing = sorted(
            (set(drawn.bonus) - set(self.results.bonus)) | (set(drawn.components) - set(self.results.components))
        )
        if missing:
            raise ValueError(f"published results lack {missing}")
        played = self.played[:, None]
        dq = None
        if self.results.dq is not None:
            dq = np.where(played[..., None], self.results.dq, False if drawn.dq is None else drawn.dq)
        return MatchResults(
            score=np.where(played, self.results.score, drawn.score),
            bonus={k: np.where(played, self.results.bonus[k], v) for k, v in drawn.bonus.items()},
            components={k: np.where(played, self.results.components[k], v) for k, v in drawn.components.items()},
            dq=dq,
        )


@dataclass(frozen=True, slots=True)
class EventModels:
    """The models an event is played with."""

    season: SeasonModels
    pick: PickModel
    awards: Sequence[AwardModel]


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """One playing of an event.

    Attributes:
        standings: Qualification standings.
        alliances: Playoff alliances in seed order, captain first.
        places: Each alliance's playoff finish, in seed order.
        awards: Each award's placed teams, 1st first; empty where awards are not simulated.
        points: Each team's advancement points.
        order: Teams in advancement order, best first.
        offers: The slots offered, or None when the event offers none.
    """

    standings: Standings
    alliances: list[Alliance]
    places: tuple[int, ...]
    awards: dict[str, tuple[int, ...]]
    points: dict[int, TeamPoints]
    order: list[int]
    offers: Offers | None


class EventCache:
    """Prepared calculations for read-only event inputs, scoped to one run or checkpoint batch.

    Changing input objects invalidates the dependent values. Callers must not mutate nested input data in place.
    Nothing here is part of a durable checkpoint or consumes random draws.
    """

    def __init__(self) -> None:
        self.field: EventField | None = None
        self.schedule: Schedule | None = None
        self.progress: EventProgress | None = None
        self.predictions: dict[str, Floats] | None = None
        self.partners: dict[int, frozenset[int]] | None = None
        self.tiebreakers: dict[int, tuple[float, ...]] | None = None
        self.fixed_results = False
        self.award_history: HistoryFeatures = {}

    def prepare(self, field: EventField, schedule: Schedule, progress: EventProgress | None) -> dict[str, Floats]:
        if self.field is not field:
            self.field = field
            self.predictions = None
            self.award_history = {}
        if self.schedule is not schedule:
            self.schedule = schedule
            self.predictions = None
            self.partners = None
            self.tiebreakers = None
        if self.progress is not progress:
            self.progress = progress
            self.tiebreakers = None
            self.fixed_results = progress is not None and bool(np.all(progress.played))
        if self.predictions is None:
            self.predictions = _alliance_predictions(field, schedule)
        return self.predictions


def simulate_event(
    field: EventField,
    schedule: Schedule,
    pack: RulePack,
    models: EventModels,
    rng: np.random.Generator,
    *,
    slots: Slots | None = None,
    progress: EventProgress | None = None,
    cache: EventCache | None = None,
) -> EventOutcome:
    """Play the event once on ``schedule``, every team of which ``field`` must predict, keeping the matches
    ``progress`` has already played."""
    advancement = pack.structure.advancement_points
    if advancement is None:
        raise ValueError(f"{pack.season}: the pack has no advancement points")

    cache = EventCache() if cache is None else cache
    predictions = cache.prepare(field, schedule, progress)
    results = draw_matches(models.season, pack, predictions, field.event_type, rng)
    if progress is not None:
        results = progress.merged(results)
    ranked = standings(schedule, results, RankingRules.from_pack(pack), rng)
    teams = tuple(int(t) for t in ranked.teams)
    rating = np.array([field.rating(t) for t in teams])

    if progress is not None and progress.alliances is not None:
        alliances = list(progress.alliances)
    else:
        if cache.partners is None:
            cache.partners = _match_mates(schedule)
        alliances = select_alliances(
            PickField(
                teams=teams,
                rating=rating,
                partners=cache.partners,
                allied=field.allied,
                city=field.city,
                team_age=field.team_age,
            ),
            models.pick,
            alliance_count(pack.structure, len(teams)),
            alliance_size(field.event_type),
            rng,
        )

    def play(red: Alliance, blue: Alliance, rng: np.random.Generator) -> tuple[float, float]:
        total = np.array([[sum(map(field.rating, _on_field(field, a))) for a in (red, blue)]])
        score = models.season.score.sample(total, rng) + models.season.score.foul_points(total.shape, rng)
        return float(score[0, 0]), float(score[0, 1])

    places = playoff(pack.structure, alliances, play, rng, decided=None if progress is None else progress.decided)

    stage = tier_for(field.event_type)
    awards: dict[str, tuple[int, ...]] = {}
    if stage in AWARD_STAGES:
        award_field = AwardField(
            teams=teams,
            rating=rating,
            auto_rating=np.array([field.auto_rating(t) for t in teams]),
            history=field.history,
            rookie=field.rookie,
            alliances=alliances,
        )
        awards = draw_awards(
            award_field,
            models.awards,
            stage,
            rng,
            inspire_won_before=field.inspire_won_before,
            history_features=cache.award_history,
        )

    points = advancement_points(teams, alliances, places, awards, advancement)
    tiebreakers = cache.tiebreakers
    if tiebreakers is None:
        tiebreakers = _advancement_tiebreakers(schedule, results)
        if cache.fixed_results:
            cache.tiebreakers = tiebreakers
    order = advancement_order(points, tiebreakers, rng)
    offered = (
        None
        if slots is None
        else offers(order, slots.already_advanced, slots.count, first_championship_slots=slots.first_championship)
    )
    return EventOutcome(ranked, alliances, places, awards, points, order, offered)


def _alliance_predictions(field: EventField, schedule: Schedule) -> dict[str, Floats]:
    """Each alliance's predicted points per column, shaped ``(match, alliance)``."""
    columns = sorted({c for t in np.unique(schedule.stations) for c in field.predicted[int(t)]})
    return {
        c: np.vectorize(lambda t, c=c: field.predicted[int(t)].get(c, 0.0), otypes=[np.float64])(schedule.stations).sum(
            axis=2
        )
        for c in columns
    }


def _match_mates(schedule: Schedule) -> dict[int, frozenset[int]]:
    """Every team each team shared a qualification match with, on either alliance."""
    mates: dict[int, set[int]] = defaultdict(set)
    for match in schedule.stations:
        present = {int(t) for t in match.ravel()}
        for t in present:
            mates[t] |= present - {t}
    return {t: frozenset(m) for t, m in mates.items()}


def _on_field(field: EventField, alliance: Alliance) -> Alliance:
    """The alliance's best-rated teams, as many as take the field."""
    return tuple(sorted(alliance, key=field.rating, reverse=True)[:ALLIANCE_SIZE])


def _advancement_tiebreakers(schedule: Schedule, results: MatchResults) -> dict[int, tuple[float, ...]]:
    """Each team's average no-foul and autonomous points, then its highest and second-highest no-foul scores, over
    its counted appearances."""
    missing = sorted({NO_FOUL, AUTO} - set(results.components))
    if missing:
        raise ValueError(f"results lack {missing}")
    no_foul: dict[int, list[float]] = defaultdict(list)
    auto: dict[int, list[float]] = defaultdict(list)
    for m, a, s in zip(*np.nonzero(~schedule.surrogate), strict=True):
        team = int(schedule.stations[m, a, s])
        no_foul[team].append(float(results.components[NO_FOUL][m, a]))
        auto[team].append(float(results.components[AUTO][m, a]))
    tiebreakers: dict[int, tuple[float, ...]] = {}
    for team, scores in no_foul.items():
        best = sorted(scores, reverse=True)
        tiebreakers[team] = (
            float(np.mean(scores)),
            float(np.mean(auto[team])),
            best[0],
            best[1] if len(best) > 1 else 0.0,
        )
    return tiebreakers
