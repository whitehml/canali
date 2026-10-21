import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import partner_comparison
from partner_comparison import PartnerComparison, advance_partners
from snapshot import DecidedInput, EventSnapshot


def test_partners_reserves_the_pair_and_seed_and_resumes_without_mutating_inputs(monkeypatch):
    source = Path(__file__).parents[2] / "worker/fixtures/forecast.json"
    snapshot = EventSnapshot.model_validate_json(source.read_text()).model_copy(
        update={"min_iterations": 64, "max_iterations": 64}
    )
    before = snapshot.model_dump_json()
    comparison = PartnerComparison(captain=1, partners=(2, 3), seed=2)
    real_playoff = partner_comparison.playoff
    seen = []

    def inspect(structure, alliances, play, rng):
        assert alliances[1][0] == 1 and alliances[1][1] in (2, 3)
        flattened = [t for alliance in alliances for t in alliance]
        assert len(flattened) == len(set(flattened))
        assert set(flattened) <= snapshot.field.predicted.keys()
        seen.append(alliances[1])
        return real_playoff(structure, alliances, play, rng)

    monkeypatch.setattr(partner_comparison, "playoff", inspect)

    def finish(request):
        checkpoint = None
        while True:
            checkpoint, report = advance_partners(snapshot, request, checkpoint)
            if report["status"] == "complete":
                return report["result"]

    result = finish(comparison)
    reversed_result = finish(comparison.model_copy(update={"partners": (3, 2)}))
    assert result["iterations"] == 64 and len(seen) == 64 * 4
    assert result["partners"] == list(reversed(reversed_result["partners"]))
    for row in result["partners"]:
        assert 0 <= row["p_win"] <= row["p_final"] <= 1
        assert 1 <= row["mean_place"] <= 4 and row["standard_error"] >= 0
    assert snapshot.model_dump_json() == before
    for request in (
        comparison.model_copy(update={"partners": (2, 2)}),
        comparison.model_copy(update={"partners": (2, 999999)}),
        comparison.model_copy(update={"seed": 8}),
    ):
        with pytest.raises(ValueError):
            advance_partners(snapshot, request)
    for unsupported in (
        snapshot.model_copy(update={"field": replace(snapshot.field, event_type="FIRST Championship")}),
        snapshot.model_copy(
            update={
                "progress": snapshot.progress.model_copy(update={"decided": [DecidedInput(series=1, game=1, winner=1)]})
            }
        ),
    ):
        with pytest.raises(ValueError):
            advance_partners(unsupported, comparison)
