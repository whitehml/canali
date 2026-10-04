"""Qualification standings: ranking points, ranking score and tiebreakers from a season's rule pack.

A team's ranking score is its average ranking points over its non-surrogate appearances. An ``avg`` tiebreaker averages
a component over the same appearances and a ``max`` tiebreaker takes its highest value. A disqualified appearance counts
as played and scores zero in every sort order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self, cast

import numpy as np
import numpy.typing as npt

from sim.schedule import Schedule
from warehouse.rules.model import RulePack

Statistic = Literal["avg", "max", "random"]

RANK_SCORE = "rank_score"


@dataclass(frozen=True, slots=True)
class Tiebreaker:
    """A sort order after the ranking score: ``avg:<component>``, ``max:<component>`` or ``random``."""

    statistic: Statistic
    component: str | None = None

    @classmethod
    def parse(cls, spec: str) -> Self:
        if spec == "random":
            return cls("random")
        statistic, _, component = spec.partition(":")
        if statistic not in ("avg", "max") or not component:
            raise ValueError(f"unknown tiebreaker {spec!r}")
        return cls(cast(Statistic, statistic), component)


@dataclass(frozen=True, slots=True)
class RankingRules:
    """How a season awards ranking points and orders teams."""

    rp_win: int
    rp_tie: int
    rp_loss: int
    bonus_rp: Mapping[str, int]
    tiebreakers: tuple[Tiebreaker, ...]

    @classmethod
    def from_pack(cls, pack: RulePack) -> Self:
        ranking = pack.ranking
        if ranking.formula != RANK_SCORE:
            raise ValueError(f"{pack.season}: ranking formula {ranking.formula!r} is not {RANK_SCORE!r}")
        if ranking.rp_win is None or ranking.rp_tie is None:
            raise ValueError(f"{pack.season}: the pack does not give ranking points for a win and a tie")
        if not ranking.tiebreakers:
            raise ValueError(f"{pack.season}: the pack lists no tiebreakers")
        tiebreakers = tuple(Tiebreaker.parse(spec) for spec in ranking.tiebreakers)
        if not any(t.statistic == "random" for t in tiebreakers):
            tiebreakers = (*tiebreakers, Tiebreaker("random"))
        return cls(
            rp_win=ranking.rp_win,
            rp_tie=ranking.rp_tie,
            rp_loss=ranking.rp_loss,
            bonus_rp={bonus.component: bonus.rp for bonus in ranking.bonus_rp},
            tiebreakers=tiebreakers,
        )

    @property
    def components(self) -> tuple[str, ...]:
        """Components the tiebreakers sort on."""
        return tuple(t.component for t in self.tiebreakers if t.component is not None)

    def alliance_rp(self, results: MatchResults) -> npt.NDArray[np.int64]:
        """Ranking points each alliance earns, shaped ``(match, alliance)``."""
        own = results.score
        other = own[:, ::-1]
        rp = np.where(own > other, self.rp_win, np.where(own == other, self.rp_tie, self.rp_loss))
        for component, points in self.bonus_rp.items():
            rp = rp + points * results.bonus[component]
        return rp.astype(np.int64)


@dataclass(frozen=True, slots=True)
class MatchResults:
    """Qualification results in schedule order, shaped ``(match, alliance)``.

    Attributes:
        score: Final score, fouls included.
        bonus: Whether the alliance earned each bonus ranking point, keyed by its rule-pack component.
        components: Values of the components the tiebreakers sort on, keyed by rule-pack name.
        dq: Disqualified appearances, shaped like the schedule's stations; None when there are none.
    """

    score: npt.NDArray[np.float64]
    bonus: Mapping[str, npt.NDArray[np.bool_]]
    components: Mapping[str, npt.NDArray[np.float64]]
    dq: npt.NDArray[np.bool_] | None = None


@dataclass(frozen=True, slots=True)
class Standings:
    """Teams in rank order.

    Attributes:
        teams: Team numbers, first place first.
        sort_orders: The ranking score, then each tiebreaker's value, shaped ``(team, sort order)``.
        matches_played: Non-surrogate appearances per team.
    """

    teams: npt.NDArray[np.int64]
    sort_orders: npt.NDArray[np.float64]
    matches_played: npt.NDArray[np.int64]


def standings(schedule: Schedule, results: MatchResults, rules: RankingRules, rng: np.random.Generator) -> Standings:
    """Rank a schedule's teams on its results."""
    if results.score.shape != schedule.stations.shape[:2]:
        raise ValueError(f"results are shaped {results.score.shape}, the schedule {schedule.stations.shape[:2]}")
    missing = (set(rules.components) - set(results.components)) | (set(rules.bonus_rp) - set(results.bonus))
    if missing:
        raise ValueError(f"results lack {sorted(missing)}")

    teams, index = np.unique(schedule.stations, return_inverse=True)
    index = index.reshape(schedule.stations.shape)
    counted = ~schedule.surrogate
    zeroed = counted if results.dq is None else counted & ~results.dq
    team_of = index[counted]
    played = np.bincount(team_of, minlength=len(teams))

    def per_appearance(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        spread = np.broadcast_to(np.asarray(values, dtype=np.float64)[..., None], schedule.stations.shape)
        return np.where(zeroed, spread, 0.0)[counted]

    def average(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        total = np.bincount(team_of, weights=per_appearance(values), minlength=len(teams))
        return np.divide(total, played, out=np.zeros(len(teams)), where=played > 0)

    def highest(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        best = np.zeros(len(teams))
        np.maximum.at(best, team_of, per_appearance(values))
        return best

    columns = [average(rules.alliance_rp(results).astype(np.float64))]
    for tiebreaker in rules.tiebreakers:
        if tiebreaker.component is None:
            columns.append(rng.random(len(teams)))
        else:
            values = results.components[tiebreaker.component]
            columns.append(average(values) if tiebreaker.statistic == "avg" else highest(values))

    sort_orders = np.column_stack(columns)
    order = np.lexsort(-sort_orders.T[::-1])
    return Standings(teams=teams[order], sort_orders=sort_orders[order], matches_played=played[order])
