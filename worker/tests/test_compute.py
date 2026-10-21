"""Independent numerical checks for the prepared-input execution path."""

import json
from pathlib import Path

import pytest
from compute import compute, validate


@pytest.fixture
def event():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "event.json").read_text())


def test_ratings_match_the_closed_form_for_two_disjoint_alliances(event):
    result = compute(event)
    ratings = {team["number"]: team for team in result["teams"]}
    for team in (1, 2):
        assert ratings[team]["epa"] == pytest.approx(57)
        assert ratings[team]["pridge"] == pytest.approx(50 + 20 / (2 + 1.28))
    for team in (3, 4):
        assert ratings[team]["epa"] == pytest.approx(43)
        assert ratings[team]["pridge"] == pytest.approx(50 - 20 / (2 + 1.28))
    assert result == compute(event)
    forecast = result["forecasts"][0]
    assert forecast["p_red_win"] + forecast["p_blue_win"] + forecast["p_tie"] == pytest.approx(1)


def test_finite_negative_priors_from_native_epa_are_accepted(event):
    event["teams"][0]["epa"] = -10
    result = compute(event)
    assert result["teams"][0]["epa"] == pytest.approx(18)


def test_opponent_adjustment_uses_both_alliances_before_either_update(event):
    event["m"]["values"] = [0.5]
    ratings = compute(event)["teams"]
    assert [team["epa"] for team in ratings] == pytest.approx([57, 57, 43, 43])


def test_unplayed_event_keeps_the_supplied_priors(event):
    event["matches"][0]["red_score"] = event["matches"][0]["blue_score"] = None
    assert all(team["epa"] == team["pridge"] == 50 for team in compute(event)["teams"])


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1, True])
def test_invalid_scores_are_rejected(event, score):
    event["matches"][0]["red_score"] = score
    with pytest.raises(ValueError):
        validate(event)


def test_unknown_or_repeated_scheduled_teams_are_rejected(event):
    event["matches"][0]["red"] = [1, 1]
    with pytest.raises(ValueError, match="distinct"):
        validate(event)


def test_a_correction_replays_from_the_supplied_event_priors(event):
    compute(event)
    event["matches"][0]["red_score"] = 100
    result = compute(event)
    assert result["teams"][0]["epa"] == 50
