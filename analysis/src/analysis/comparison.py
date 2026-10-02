"""The comparable predictions: the intersection of predictable alliance rows."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from warehouse.tier import EventTier, tier_for

AllianceKey = tuple[uuid.UUID, str]


@dataclass(frozen=True, slots=True)
class Forecast:
    """One model's prediction of one alliance's no-foul score, made before the match."""

    match_id: uuid.UUID
    alliance: str
    season: int
    event_id: uuid.UUID
    event_type: str | None
    as_of_match: int
    predicted: float
    actual: float

    @property
    def key(self) -> AllianceKey:
        return (self.match_id, self.alliance)


@dataclass(frozen=True, slots=True)
class SharedRows:
    """The rows every model predicted."""

    models: tuple[str, ...]
    rows: tuple[Mapping[str, Forecast], ...]
    dropped: Mapping[str, int]

    @property
    def n(self) -> int:
        return len(self.rows)

    def of_tiers(self, tiers: Sequence[EventTier]) -> SharedRows:
        """The rows from events of the given tiers."""
        kept = tuple(row for row in self.rows if tier_for(next(iter(row.values())).event_type) in tiers)
        return SharedRows(models=self.models, rows=kept, dropped=self.dropped)

    def mse(self, model: str) -> float:
        """Mean squared error of one model over the shared rows."""
        if not self.rows:
            return float("nan")
        return sum((row[model].predicted - row[model].actual) ** 2 for row in self.rows) / len(self.rows)


def shared_rows(models: Mapping[str, Sequence[Forecast]]) -> SharedRows:
    """Intersect the models' forecasts on alliance key."""
    if not models:
        raise ValueError("no models to compare")
    indexed = {name: {f.key: f for f in forecasts} for name, forecasts in models.items()}
    for name, forecasts in models.items():
        if len(indexed[name]) != len(forecasts):
            raise ValueError(f"model {name!r} predicts some alliance more than once")

    names = tuple(indexed)
    shared = set.intersection(*(set(index) for index in indexed.values()))
    rows = []
    for key in sorted(shared, key=lambda k: (str(k[0]), k[1])):
        row = {name: indexed[name][key] for name in names}
        if len({f.actual for f in row.values()}) != 1:
            raise ValueError(f"models disagree on the actual score of {key}")
        rows.append(row)
    return SharedRows(
        models=names,
        rows=tuple(rows),
        dropped={name: len(index) - len(shared) for name, index in indexed.items()},
    )


@dataclass(frozen=True, slots=True)
class ScoreComparison:
    """Model `a` against model `b` on the same rows, as the difference in mean squared error."""

    a: str
    b: str
    rows: int
    events: int
    mse_a: float
    mse_b: float
    difference: float
    interval: tuple[float, float]

    @property
    def relative(self) -> float:
        """The difference as a fraction of model `b`'s error."""
        return self.difference / self.mse_b

    @property
    def a_wins(self) -> bool:
        """Lower error with an interval that excludes zero."""
        return self.interval[1] < 0.0


def compare(
    shared: SharedRows, a: str, b: str, *, samples: int = 2000, seed: int = 0, confidence: float = 0.95
) -> ScoreComparison:
    """Compare two models over every shared row."""
    return _compare(shared.rows, a, b, samples=samples, seed=seed, confidence=confidence)


def by_match(
    shared: SharedRows, a: str, b: str, *, samples: int = 2000, seed: int = 0, confidence: float = 0.95
) -> dict[int, ScoreComparison]:
    """Compare two models at each match index, on the alliances predicted from the fit at that index."""
    buckets: dict[int, list[Mapping[str, Forecast]]] = defaultdict(list)
    for row in shared.rows:
        buckets[row[a].as_of_match].append(row)
    return _compare_buckets(buckets, a, b, samples=samples, seed=seed, confidence=confidence)


def by_round(
    shared: SharedRows,
    rounds: Mapping[AllianceKey, int],
    a: str,
    b: str,
    *,
    samples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> dict[int, ScoreComparison]:
    """Compare two models in each round, on the alliances whose teams are all playing their nth match."""
    buckets: dict[int, list[Mapping[str, Forecast]]] = defaultdict(list)
    for row in shared.rows:
        round_ = rounds.get(row[a].key)
        if round_ is not None:
            buckets[round_].append(row)
    return _compare_buckets(buckets, a, b, samples=samples, seed=seed, confidence=confidence)


def _compare_buckets(
    buckets: Mapping[int, Sequence[Mapping[str, Forecast]]],
    a: str,
    b: str,
    *,
    samples: int,
    seed: int,
    confidence: float,
) -> dict[int, ScoreComparison]:
    return {
        label: _compare(rows, a, b, samples=samples, seed=seed, confidence=confidence)
        for label, rows in sorted(buckets.items())
    }


def _compare(
    rows: Sequence[Mapping[str, Forecast]], a: str, b: str, *, samples: int, seed: int, confidence: float
) -> ScoreComparison:
    """Events are resampled whole, since the alliances of one event share teams and are not independent."""
    by_event: dict[uuid.UUID, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        by_event[row[a].event_id].append(
            ((row[a].predicted - row[a].actual) ** 2, (row[b].predicted - row[b].actual) ** 2)
        )
    if not by_event:
        nan = float("nan")
        return ScoreComparison(a, b, 0, 0, nan, nan, nan, (nan, nan))

    squared_a = np.array([sum(x for x, _ in errors) for errors in by_event.values()])
    squared_b = np.array([sum(y for _, y in errors) for errors in by_event.values()])
    counts = np.array([len(errors) for errors in by_event.values()], dtype=np.float64)

    drawn = np.random.default_rng(seed).integers(0, len(counts), size=(samples, len(counts)))
    resampled = (squared_a[drawn].sum(axis=1) - squared_b[drawn].sum(axis=1)) / counts[drawn].sum(axis=1)
    tail = 100.0 * (1.0 - confidence) / 2.0
    low, high = np.percentile(resampled, [tail, 100.0 - tail])

    mse_a = float(squared_a.sum() / counts.sum())
    mse_b = float(squared_b.sum() / counts.sum())
    return ScoreComparison(
        a=a,
        b=b,
        rows=int(counts.sum()),
        events=len(counts),
        mse_a=mse_a,
        mse_b=mse_b,
        difference=mse_a - mse_b,
        interval=(float(low), float(high)),
    )
