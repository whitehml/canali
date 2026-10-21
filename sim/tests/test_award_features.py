"""Award preparation preserves the meaning and order of every feature column."""

from dataclasses import replace

import numpy as np

from sim.award_model import AwardField, HistoryFeatures


def test_award_features_keep_history_candidate_order_and_changing_alliance_seats() -> None:
    field = AwardField(
        teams=(1, 2, 3),
        rating=np.array([10.0, 20.0, 30.0]),
        auto_rating=np.array([5.0, 5.0, 5.0]),
        history={1: {"Think": 2.0, "Design": 3.0, "Connect": 4.0, "InspirePlaced": 5.0}},
        rookie=frozenset({3}),
        alliances=[(2, 1)],
    )
    history: HistoryFeatures = {}
    expected = np.array(
        [
            [np.sqrt(1.5), 0, 0, 0, 0, 0, 0, 1, 0, 0, 0],
            [-np.sqrt(1.5), 0, 2, 7, 3, 4, 5, 0, 0, 1, 1],
        ]
    )
    np.testing.assert_allclose(field.features("Think", [3, 1], history_features=history), expected)
    np.testing.assert_array_equal(field.features("Think", [1], history_features=history), field.features("Think", [1]))
    changed = replace(field, alliances=[(1, 3)])
    assert changed.features("Think", [1], history_features=history)[0, -3:].tolist() == [1, 0, 1]
    design = changed.features("Design", [1], history_features=history)[0]
    assert design[2:7].tolist() == [3, 6, 0, 6, 5]
