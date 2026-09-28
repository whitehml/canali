"""The cross-season norm rating: the shape a season's field is mapped onto."""

from __future__ import annotations

import random

import pytest

from epa.norm import build_norm_map


def _field(teams: int = 600, seed: int = 7) -> list[float]:
    """A right-skewed season of final ratings."""
    rng = random.Random(seed)
    return [max(0.0, rng.lognormvariate(3.2, 0.7)) for _ in range(teams)]


# --------------------------------------------------------------------------------------------------------- the map


def test_a_field_too_small_to_rank_within_is_refused() -> None:
    with pytest.raises(ValueError, match="no population to rank within"):
        build_norm_map(_field(teams=100))


def test_the_map_never_sends_a_higher_rating_to_a_lower_norm() -> None:
    mapping = build_norm_map(_field())
    assert list(mapping.norms) == sorted(mapping.norms)

    field = sorted(_field())
    rng = random.Random(11)
    probes = sorted(rng.uniform(field[0], field[-1]) for _ in range(2000))
    norms = [mapping(value) for value in probes]
    assert norms == sorted(norms)


def test_the_map_is_flat_outside_the_ratings_it_was_fitted_on() -> None:
    field = _field()
    mapping = build_norm_map(field)
    assert mapping(min(field) - 1000.0) == mapping.norms[0]
    assert mapping(max(field) + 1000.0) == mapping.norms[-1]
