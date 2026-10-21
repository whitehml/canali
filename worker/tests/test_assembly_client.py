"""The native assembler fetches live progress and preserves historical qualification cutoffs."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("assemble_client", Path(__file__).parents[1] / "assemble_forecast.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def client():
    calls = []
    alliances = {"alliances": [{"number": 1}]}
    schedule = {"schedule": [{"scoreRedFinal": 100, "scoreBlueFinal": 80}]}

    def hybrid(_season, _code, level):
        calls.append(("schedule", level))
        return SimpleNamespace(status=200, data=schedule)

    def scores(_season, _code, level):
        calls.append(("scores", level))
        return SimpleNamespace(status=200, data={"matchScores": []})

    return (
        SimpleNamespace(
            alliances=lambda *_: SimpleNamespace(status=200, data=alliances), hybrid_schedule=hybrid, scores=scores
        ),
        calls,
        schedule,
    )


def test_live_stage_fetches_alliances_playoff_schedule_and_breakdowns():
    first, calls, _ = client()
    result = module.stage_options(first, 2025, "A")
    assert result["selection_published"]
    assert result["alliances"]["alliances"]
    assert calls == [("schedule", "playoff"), ("scores", "playoff")]


def test_historical_stage_never_fetches_future_playoffs():
    first, calls, _ = client()
    assert module.stage_options(first, 2025, "A", historical=True) == {"selection_published": True}
    assert not calls
    result = module.stage_options(first, 2025, "A", after_qualifications=True)
    assert result == {"selection_published": True, "through": 1}
    assert calls == [("schedule", "qual")]


def test_failed_upstream_stage_never_silently_drops_published_progress():
    first, _, _ = client()
    first.scores = lambda *_: SimpleNamespace(status=503)
    with pytest.raises(SystemExit, match="503"):
        module.stage_options(first, 2025, "A")
