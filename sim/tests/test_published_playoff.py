"""Published results must follow the actual bracket, including ties and the final reset."""

import pytest

from sim.playoff import published_winners


def test_four_alliance_prefix_and_reset_final() -> None:
    games = [
        (1, 1, 4, 10, 5),
        (2, 2, 3, 10, 5),
        (3, 4, 3, 10, 5),
        (4, 1, 2, 10, 5),
        (5, 2, 4, 10, 5),
        (6, 1, 2, 5, 10),
        (7, 1, 2, 10, 5),
    ]
    assert published_winners(4, games)[7, 1] == 1
    for n in range(len(games)):
        assert len(published_winners(4, games[:n])) == n


@pytest.mark.parametrize(
    "games",
    [
        [(2, 2, 3, 10, 5)],
        [(1, 1, 3, 10, 5)],
        [(1, 1, 4, 10, 5), (2, 2, 3, 10, 5), (3, 1, 3, 10, 5)],
    ],
)
def test_rejects_missing_predecessors_and_wrong_opponents(games: list[tuple[int, int, int, float, float]]) -> None:
    with pytest.raises(ValueError):
        published_winners(4, games)


def test_tied_final_replay_does_not_count_as_a_decisive_game() -> None:
    assert published_winners(2, [(1, 1, 2, 10, 10), (1, 1, 2, 10, 5), (1, 1, 2, 10, 5)]) == {
        (1, 1): 1,
        (1, 2): 1,
    }
