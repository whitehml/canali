"""The comparable predictions: the intersection of predictable alliance rows."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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
