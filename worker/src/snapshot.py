"""Validated, warehouse-independent inputs for the complete event simulator."""

from __future__ import annotations

from typing import Literal, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sim.award_model import MODELLED_AWARDS, AwardModel, model_for
from sim.event import EventField, EventModels, EventProgress, Slots
from sim.match import SeasonModels
from sim.pick_model import PickModel
from sim.playoff import BRACKETS, DOUBLE_ELIMINATION
from sim.rp_probability import RpCurve
from sim.rp_source import FloorDivide, Identity, Ratio, rp_sources
from sim.runner import fixed_field, generated_field
from sim.schedule import Schedule
from sim.score import ScoreModel
from sim.selection import alliance_count, alliance_size
from sim.standings import MatchResults, RankingRules
from warehouse.rules.model import RulePack, to_column_name
from warehouse.tier import EventTier, tier_for


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RecoveryInput(InputModel):
    kind: Literal["identity", "floor_divide", "ratio"] = "identity"
    value: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_value(self) -> Self:
        if (self.kind == "identity") != (self.value is None):
            raise ValueError("identity has no value; floor_divide and ratio require a positive value")
        return self

    def build(self):
        if self.kind == "identity":
            return Identity()
        return FloorDivide(self.value) if self.kind == "floor_divide" else Ratio(self.value)


class CurveInput(InputModel):
    shift: float
    log_sd: float = Field(ge=-20, le=20)
    log_sd_slope: float
    cap: float = Field(default=1, ge=0, le=1)
    correlation: float = Field(default=0, gt=-1, lt=1)
    recovery: RecoveryInput = Field(default_factory=RecoveryInput)

    def build(self) -> RpCurve:
        return RpCurve(**self.model_dump(exclude={"recovery"}), recovery=self.recovery.build())


class ModelsInput(InputModel):
    score: ScoreModel
    rp_curves: dict[str, CurveInput]
    tiebreaker_shares: dict[str, float]
    pick: PickModel
    awards: list[AwardModel]

    def build(self) -> EventModels:
        return EventModels(
            SeasonModels(
                self.score,
                {name: curve.build() for name, curve in self.rp_curves.items()},
                self.tiebreaker_shares,
            ),
            self.pick,
            self.awards,
        )


class ScheduleInput(InputModel):
    stations: list[list[list[int]]] = Field(min_length=1, max_length=1000)
    surrogate: list[list[list[bool]]] | None = None

    def build(self) -> Schedule:
        teams = np.array(self.stations, dtype=np.int64)
        if teams.shape[1:] != (2, 2):
            raise ValueError("qualification schedule must have two alliances of two stations")
        return Schedule(
            teams, np.zeros(teams.shape, dtype=bool) if self.surrogate is None else np.array(self.surrogate)
        )


class DecidedInput(InputModel):
    series: int = Field(ge=1)
    game: int = Field(ge=1, le=3)
    winner: int = Field(ge=1, le=8)


class ProgressInput(InputModel):
    played: list[bool]
    score: list[list[float]]
    bonus: dict[str, list[list[bool]]]
    components: dict[str, list[list[float]]]
    dq: list[list[list[bool]]] | None = None
    alliances: list[tuple[int, ...]] | None = None
    decided: list[DecidedInput] = Field(default_factory=list)

    def build(self) -> EventProgress:
        return EventProgress(
            np.array(self.played, dtype=bool),
            MatchResults(
                np.array(self.score, dtype=float),
                {name: np.array(values, dtype=bool) for name, values in self.bonus.items()},
                {name: np.array(values, dtype=float) for name, values in self.components.items()},
                None if self.dq is None else np.array(self.dq, dtype=bool),
            ),
            self.alliances,
            {(match.series, match.game): match.winner for match in self.decided},
        )


class SeasonPrior(InputModel):
    season: int = Field(ge=2015)
    revision: int = Field(ge=1)


class MatchReference(InputModel):
    level: Literal["QUALIFICATION", "PLAYOFF", "SEMIFINAL", "FINAL"]
    number: int = Field(ge=1)
    series: int | None = Field(default=None, ge=1)


class EventSnapshot(InputModel):
    rule_pack: RulePack
    field: EventField
    models: ModelsInput
    schedule: ScheduleInput | None
    progress: ProgressInput | None = None
    slots: Slots | None = None
    matches_per_team: int = Field(default=5, ge=1, le=20)
    seed: int = Field(ge=0)
    tolerance: float = Field(default=0.01, gt=0, le=0.5)
    min_iterations: int = Field(default=1000, ge=1, le=100_000)
    max_iterations: int = Field(default=10_000, ge=1, le=100_000)
    season_prior: SeasonPrior | None = None
    as_of_match: int | None = Field(default=None, ge=0, le=1000)
    last_match: MatchReference | None

    @model_validator(mode="after")
    def check_event(self) -> Self:
        teams = set(self.field.predicted)
        if self.season_prior is not None and self.season_prior.season != self.rule_pack.season:
            raise ValueError("season dependency must match the rule pack")
        if not 4 <= len(teams) <= 128 or any(team <= 0 for team in teams):
            raise ValueError("field needs 4 to 128 positive team numbers")
        if any(not values for values in self.field.predicted.values()):
            raise ValueError("each team needs component ratings")
        if self.min_iterations > self.max_iterations:
            raise ValueError("min_iterations exceeds max_iterations")
        tier = tier_for(self.field.event_type)
        if tier is None:
            raise ValueError("event type has no rated tier")
        structure = self.rule_pack.structure
        alliances = alliance_count(structure, len(teams))
        if not structure.playoff_implemented or structure.playoff_structure != DOUBLE_ELIMINATION:
            raise ValueError("event needs an implemented double-elimination playoff")
        if alliances not in BRACKETS or alliances * alliance_size(self.field.event_type) > len(teams):
            raise ValueError("invalid alliance count for this field")
        if structure.advancement_points is None or structure.advancement_points.qualification_alpha <= 1:
            raise ValueError("advancement points require qualification_alpha > 1")
        self.check_models(tier)
        self.check_slots(tier)
        self.check_schedule(teams, alliances)
        return self

    def check_schedule(self, teams: set[int], alliances: int) -> None:
        if self.schedule is None:
            if self.progress is not None:
                raise ValueError("published progress requires a published schedule")
        else:
            schedule = self.schedule.build()
            if set(schedule.stations.ravel()) != teams:
                raise ValueError("schedule teams must match the field")
            if any(len(set(row.ravel())) != 4 for row in schedule.stations):
                raise ValueError("a team cannot appear twice in a match")
            if self.progress is not None:
                self.check_progress(schedule, alliances)

    def check_models(self, tier: EventTier) -> None:
        ranking = RankingRules.from_pack(self.rule_pack)
        required_components = {"preFoulTotal", "autoPoints", *ranking.components}
        if not required_components <= self.models.tiebreaker_shares.keys():
            raise ValueError(f"tiebreaker shares need {sorted(required_components)}")
        if any(share < 0 for share in self.models.tiebreaker_shares.values()):
            raise ValueError("tiebreaker shares must be nonnegative")
        sources = rp_sources(self.rule_pack)
        if set(sources) != set(self.models.rp_curves):
            raise ValueError("RP curves must match the rule pack's bonus RPs")
        columns = {to_column_name(name) for source in sources.values() for name in source.points_from}
        for values in self.field.predicted.values():
            if not columns <= values.keys():
                raise ValueError("team component ratings lack an RP source")
        for name, source in sources.items():
            curve = self.models.rp_curves[name].build()
            if type(curve.recovery) is not type(source.recovery):
                raise ValueError(f"{name} recovery differs from its declared source")
        score = self.models.score
        if score.slope <= 0 or score.dispersion <= 0 or score.dof <= 2 or score.skew <= 0:
            raise ValueError("invalid score model")
        if score.foul_mean < 0 or score.foul_variance < 0:
            raise ValueError("foul moments must be nonnegative")
        if tier in (EventTier.REGULAR, EventTier.RCMP):
            for award in MODELLED_AWARDS:
                model_for(self.models.awards, award, tier)

    def check_slots(self, tier: EventTier) -> None:
        if self.slots is not None:
            first = self.slots.first_championship
            if self.slots.count < 1 or self.slots.count > len(self.field.predicted):
                raise ValueError("slot count must be between one and the field size")
            if first is not None and (tier is not EventTier.RCMP or not 0 <= first <= self.slots.count):
                raise ValueError("FIRST Championship slots require a regional championship and valid count")

    def check_progress(self, schedule: Schedule, alliances: int) -> None:
        progress = self.progress.build()
        shape = schedule.stations.shape
        if progress.played.shape != shape[:1] or progress.results.score.shape != shape[:2]:
            raise ValueError("published results must match the schedule")
        required_bonus = set(self.models.rp_curves)
        if not required_bonus <= progress.results.bonus.keys():
            raise ValueError("published results lack bonus RPs")
        if not self.models.tiebreaker_shares.keys() <= progress.results.components.keys():
            raise ValueError("published results lack tiebreaker components")
        arrays = [*progress.results.bonus.values(), *progress.results.components.values()]
        if any(array.shape != shape[:2] for array in arrays):
            raise ValueError("published components must match the schedule")
        if np.any(progress.results.score < 0) or any(np.any(v < 0) for v in progress.results.components.values()):
            raise ValueError("published scores and components must be nonnegative")
        if progress.results.dq is not None and progress.results.dq.shape != shape:
            raise ValueError("disqualifications must match schedule stations")
        self.check_playoff(progress, alliances)

    def check_playoff(self, progress: EventProgress, alliances: int) -> None:
        if progress.alliances is not None:
            selected = [team for alliance in progress.alliances for team in alliance]
            size = alliance_size(self.field.event_type)
            if len(progress.alliances) != alliances or any(len(a) != size for a in progress.alliances):
                raise ValueError("published alliance sizes do not match the event")
            if len(set(selected)) != len(selected) or not set(selected) <= self.field.predicted.keys():
                raise ValueError("published alliances must contain distinct field teams")
            if not np.all(progress.played):
                raise ValueError("published alliances require completed qualifications")
        if progress.decided and progress.alliances is None:
            raise ValueError("published playoff results require alliances")
        if len(progress.decided) != len(self.progress.decided):
            raise ValueError("duplicate published playoff result")
        if any(winner > alliances for winner in (progress.decided or {}).values()):
            raise ValueError("published winning seed exceeds alliance count")

    def draw(self):
        if self.schedule is not None:
            return fixed_field(self.field, self.schedule.build())
        return generated_field(self.field, sorted(self.field.predicted), self.matches_per_team)
