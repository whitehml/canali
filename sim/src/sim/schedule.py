"""The qualification schedule an event plays."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

RED = 0
BLUE = 1


@dataclass(frozen=True, slots=True)
class Schedule:
    """A qualification schedule.

    Attributes:
        stations: Team at each station, shaped ``(match, alliance, station)``.
        surrogate: True if the appearance is a surrogate's.
    """

    stations: npt.NDArray[np.int64]
    surrogate: npt.NDArray[np.bool_]

    def __post_init__(self) -> None:
        if self.stations.ndim != 3 or self.stations.shape[1] != 2:
            raise ValueError(f"stations must be shaped (match, alliance, station), got {self.stations.shape}")
        if self.surrogate.shape != self.stations.shape:
            raise ValueError(f"surrogate is shaped {self.surrogate.shape}, stations {self.stations.shape}")

    @property
    def n_matches(self) -> int:
        return int(self.stations.shape[0])
