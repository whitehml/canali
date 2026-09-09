"""Payload to row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from warehouse.ingest import transforms
from warehouse.testing import generate_event

EVENT = uuid.UUID("00000000-0000-4000-8000-000000000001")


# ---------------------------------------------------------------------------------------------------------- time


def test_a_timestamp_reads_as_a_naive_wall_clock() -> None:
    local = transforms.parse_local("2026-01-10T09:00:00")

    assert local == datetime(2026, 1, 10, 9, 0)
    assert local is not None and local.tzinfo is None
    assert transforms.parse_local("2026-01-10T09:00:00Z") == local


def test_an_unreadable_timestamp_is_absent_rather_than_fatal() -> None:
    assert transforms.parse_local(None) is None
    assert transforms.parse_local("") is None
    assert transforms.parse_local("not a time") is None


def test_utc_is_derived_from_the_event_timezone() -> None:
    utc, local = transforms.timestamp_pair("2026-01-10T09:00:00", "America/New_York")
    assert local == datetime(2026, 1, 10, 9, 0)
    assert utc == datetime(2026, 1, 10, 14, 0, tzinfo=UTC)


def test_an_unusable_timezone_falls_back_to_a_constant_and_stays_flagged() -> None:
    assert transforms.resolve_timezone(None, "America/New_York") == ("America/New_York", True)
    assert transforms.resolve_timezone("Mars/Olympus", "America/New_York") == ("America/New_York", True)
    assert transforms.resolve_timezone("America/Chicago", "America/New_York") == ("America/Chicago", False)


# -------------------------------------------------------------------------------------------------------- events


@pytest.mark.parametrize(("remote", "hybrid"), [(True, False), (False, True), (True, True)])
def test_a_remote_or_hybrid_event_never_enters_the_corpus(remote: bool, hybrid: bool) -> None:
    event = generate_event(remote=remote, hybrid=hybrid)
    assert transforms.event_rows(event.season, event.events, "America/New_York") == []


def test_a_blank_code_normalizes_to_null() -> None:
    event = generate_event()
    row = transforms.event_rows(event.season, event.events, "America/New_York")[0]

    assert row["league_code"] is None
    assert row["region_code"] == "USPA"


def test_an_event_with_no_start_date_is_dropped() -> None:
    payload = {"events": [{"eventId": str(EVENT), "code": "X", "dateStart": None}]}
    assert transforms.event_rows(2025, payload, "UTC") == []


def test_an_absent_payload_is_no_rows_rather_than_a_failure() -> None:
    assert transforms.event_rows(2025, None, "UTC") == []
    assert transforms.event_rows(2025, {}, "UTC") == []


def test_the_event_type_prefers_the_name_over_the_numeric_code() -> None:
    row = {"eventId": str(EVENT), "code": "X", "dateStart": "2026-01-10T00:00:00", "type": "1"}
    assert transforms.event_rows(2025, {"events": [row]}, "UTC")[0]["type"] == "1"
    assert transforms.event_rows(2025, {"events": [{**row, "typeName": "Qualifier"}]}, "UTC")[0]["type"] == "Qualifier"


# --------------------------------------------------------------------------------------------------------- teams


def test_team_identity_and_the_season_row_come_off_one_payload() -> None:
    event = generate_event()
    identity = transforms.team_rows(event.teams)
    seasonal = transforms.team_season_rows(event.season, event.teams)

    assert set(identity[0]) == {"team_number", "rookie_year"}
    assert seasonal[0]["season"] == event.season
    assert seasonal[0]["home_region"] == "USPA"
    assert transforms.event_team_numbers(event.teams) == event.team_numbers


def test_a_rookie_year_of_zero_is_absent_rather_than_the_year_zero() -> None:
    assert transforms.team_rows({"teams": [{"teamNumber": 1, "rookieYear": 0}]})[0]["rookie_year"] is None


# ------------------------------------------------------------------------------------------------------- matches


@pytest.mark.parametrize(
    ("published", "canonical"),
    [
        ("QUALIFICATION", "QUALIFICATION"),
        ("qual", "QUALIFICATION"),
        ("Quals", "QUALIFICATION"),
        ("PLAYOFF", "PLAYOFF"),
        ("SEMIFINAL", "SEMIFINAL"),
        ("Semis", "SEMIFINAL"),
        ("Finals", "FINAL"),
        ("NONE", "OTHER"),
        (None, "OTHER"),
        ("something new", "OTHER"),
    ],
)
def test_a_tournament_level_normalizes_onto_the_enum(published: str | None, canonical: str) -> None:
    assert transforms.normalize_level(published) == canonical


def test_a_schedule_slot_carries_its_flags_and_its_side() -> None:
    event = generate_event()
    rows = transforms.hybrid_match_rows(EVENT, "America/New_York", event.hybrid_qual)

    assert len(rows) == len(event.hybrid_qual["schedule"])
    slots = rows[0]["teams"]
    assert [s["station"] for s in slots] == ["Red1", "Red2", "Blue1", "Blue2"]
    assert [s["alliance"] for s in slots] == ["RED", "RED", "BLUE", "BLUE"]


def test_a_slot_flag_is_read_off_the_slot_it_is_published_on() -> None:
    payload = {
        "schedule": [
            {
                "tournamentLevel": "QUALIFICATION",
                "matchNumber": 1,
                "teams": [
                    {"station": "Red1", "teamNumber": 11, "surrogate": True},
                    {"station": "Red2", "teamNumber": 12, "noShow": True},
                    {"station": "Blue1", "teamNumber": 21, "dq": True},
                    {"station": "Blue2", "teamNumber": 22, "onField": False},
                ],
            }
        ]
    }
    slots = {s["station"]: s for s in transforms.hybrid_match_rows(EVENT, "UTC", payload)[0]["teams"]}

    assert slots["Red1"]["surrogate"] and not slots["Red2"]["surrogate"]
    assert slots["Red2"]["no_show"] and not slots["Red1"]["no_show"]
    assert slots["Blue1"]["dq"] and not slots["Red1"]["dq"]
    assert slots["Red1"]["on_field"] and not slots["Blue2"]["on_field"]


def test_a_slot_with_no_team_is_dropped_rather_than_written_as_a_null() -> None:
    payload = {
        "schedule": [
            {
                "tournamentLevel": "QUALIFICATION",
                "matchNumber": 1,
                "teams": [{"station": "Red1", "teamNumber": 11}, {"station": "Red2", "teamNumber": None}],
            }
        ]
    }
    assert [s["team_number"] for s in transforms.hybrid_match_rows(EVENT, "UTC", payload)[0]["teams"]] == [11]


def test_only_a_changed_result_signals_a_replay() -> None:
    stored: dict[str, Any] = {
        "score_red_final": 100,
        "score_blue_final": 90,
        "score_red_auto": 10,
        "score_blue_auto": 10,
        "score_red_foul": 0,
        "score_blue_foul": 0,
        "teams": [{"station": "Red1", "team_number": 1, "surrogate": False, "no_show": False, "dq": False}],
    }

    assert not transforms.results_differ(stored, {**stored, "modified_on_utc": "later"})
    assert transforms.results_differ(stored, {**stored, "score_red_final": 117})
    assert transforms.results_differ(stored, {**stored, "teams": [{**stored["teams"][0], "team_number": 2}]})
    assert transforms.results_differ(stored, {**stored, "teams": [{**stored["teams"][0], "dq": True}]})


def test_the_slot_comparison_does_not_depend_on_the_order_slots_arrive_in() -> None:
    slots = [
        {"station": "Red1", "team_number": 1, "surrogate": False, "no_show": False, "dq": False},
        {"station": "Blue1", "team_number": 2, "surrogate": False, "no_show": False, "dq": False},
    ]
    stored = {"teams": slots}
    assert not transforms.results_differ(stored, {"teams": list(reversed(slots))})


def test_a_breakdown_is_yielded_per_side_without_the_side_label() -> None:
    event = generate_event()
    rows = list(transforms.score_breakdowns(event.scores_qual))

    assert len(rows) == 2 * len(event.scores_qual["matchScores"])
    level, series, number, alliance, breakdown = rows[0]
    assert (level, series, number, alliance) == ("QUALIFICATION", 0, 1, "RED")
    assert "alliance" not in breakdown
    assert "totalPoints" in breakdown


def test_a_breakdown_for_neither_side_is_skipped() -> None:
    payload = {"matchScores": [{"matchLevel": "QUALIFICATION", "matchNumber": 1, "alliances": [{"alliance": "Solo"}]}]}
    assert list(transforms.score_breakdowns(payload)) == []


# ------------------------------------------------------------------------------------------------------ rankings


def test_all_six_sort_orders_are_carried() -> None:
    payload = {"rankings": [{"teamNumber": 1, "rank": 1, **{f"sortOrder{i}": float(i) for i in range(1, 7)}}]}
    row = transforms.ranking_rows(EVENT, payload)[0]

    assert [row[f"sort_order_{i}"] for i in range(1, 7)] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


# -------------------------------------------------------------------------------------------------------- awards


def test_only_a_judged_team_award_with_a_winner_is_stored() -> None:
    rows = transforms.award_rows(
        EVENT,
        {
            "awards": [
                {"awardId": 11, "series": 1, "teamNumber": 25650},
                {"awardId": 6, "series": 1, "teamNumber": None},
                {"awardId": 2, "series": 1, "teamNumber": 21324},
                {"awardId": 10, "series": 1, "teamNumber": 8393},
                {"awardId": 13, "series": 1, "teamNumber": 7244},
            ]
        },
    )

    assert [(r["award_code"], r["team_number"]) for r in rows] == [(11, 25650)]
    assert rows[0]["source"] == "ftc_events"


def test_a_repeated_award_key_is_deduplicated_rather_than_fatal() -> None:
    payload = {"awards": [{"awardId": 9, "series": 1, "teamNumber": 5}, {"awardId": 9, "series": 1, "teamNumber": 6}]}
    rows = transforms.award_rows(EVENT, payload)

    assert [r["team_number"] for r in rows] == [6]


def test_a_series_distinguishes_the_places_of_one_award() -> None:
    payload = {"awards": [{"awardId": 9, "series": s, "teamNumber": s} for s in (1, 2, 3)]}
    assert [r["series"] for r in transforms.award_rows(EVENT, payload)] == [1, 2, 3]


def test_ftcscouts_award_names_map_onto_firsts_codes() -> None:
    rows = transforms.scout_award_rows(
        EVENT,
        [
            {"type": "Inspire", "placement": 1, "teamNumber": 25650},
            {"type": "Inspire", "placement": 2, "teamNumber": 14423},
            {"type": "Think", "placement": 1, "teamNumber": 8393},
        ],
    )

    assert [(r["award_code"], r["series"], r["team_number"]) for r in rows] == [
        (11, 1, 25650),
        (11, 2, 14423),
        (9, 1, 8393),
    ]
    assert {r["source"] for r in rows} == {"ftcscout"}


def test_an_unmapped_award_type_is_skipped_rather_than_invented() -> None:
    rows = transforms.scout_award_rows(
        EVENT,
        [
            {"type": "Inspire", "placement": 1, "teamNumber": 5},
            {"type": "SomethingFtcScoutAddedLater", "placement": 1, "teamNumber": 6},
            {"type": "Winner", "placement": 1, "teamNumber": 7},
        ],
    )

    assert [r["award_code"] for r in rows] == [11]


def test_the_two_suppliers_narrow_to_the_same_set_of_awards() -> None:
    assert set(transforms.SCOUT_AWARD_TYPES.values()) <= transforms.JUDGED_TEAM_AWARD_CODES


# --------------------------------------------------------------------------------------------------- advancement


def test_advancement_points_take_the_first_element_and_keep_the_rest_verbatim() -> None:
    payload = [{"team": 25650, "points": [136, 60, 40, 20, 16, 230.2]}, {"team": 8393, "points": [89]}]
    rows = transforms.advancement_points_rows(EVENT, payload)

    assert [(r["team_number"], r["points"]) for r in rows] == [(25650, 136.0), (8393, 89.0)]
    assert rows[0]["detail"]["points"][5] == 230.2


def test_advancement_reads_team_rather_than_teamnumber() -> None:
    payload = {"advancement": [{"team": 25650, "slot": 1, "criteria": "Points", "declined": False, "status": "FIRST"}]}
    rows = transforms.advancement_slot_rows(EVENT, payload)

    assert rows[0]["team_number"] == 25650
    assert set(rows[0]) == {"event_id", "slot", "team_number", "status"}


def test_the_advancement_header_carries_the_quota_nothing_else_records() -> None:
    payload = {"advancesTo": "USPACMP", "slots": 15, "fcmpReserved": 6, "advancement": []}

    assert transforms.event_advancement_row(EVENT, payload) == {
        "event_id": EVENT,
        "advances_to": "USPACMP",
        "slots": 15,
        "slots_first_championship": 6,
    }
    assert transforms.event_advancement_row(EVENT, None) is None


# ----------------------------------------------------------------------------------------------------- alliances


def test_a_seated_alliance_reads_the_team_out_of_each_slot_object() -> None:
    event = generate_event()
    rows = transforms.playoff_alliance_rows(EVENT, event.alliances)

    assert [r["alliance_number"] for r in rows] == [1, 2]
    assert rows[0]["captain"] == event.alliances["alliances"][0]["captain"]["teamNumber"]
    assert rows[0]["round2"] is None


def test_the_third_pick_and_the_backup_are_discarded() -> None:
    payload = {
        "alliances": [
            {
                "number": 1,
                "captain": {"teamNumber": 1},
                "round1": {"teamNumber": 2},
                "round2": {"teamNumber": 3},
                "round3": {"teamNumber": 4},
                "backup": {"teamNumber": 5},
                "backupReplaced": 2,
            }
        ]
    }
    rows = transforms.playoff_alliance_rows(EVENT, payload)

    assert set(rows[0]) == {"event_id", "alliance_number", "name", "captain", "round1", "round2"}
    assert (rows[0]["captain"], rows[0]["round1"], rows[0]["round2"]) == (1, 2, 3)


def test_a_pick_takes_its_ordinal_from_the_published_index() -> None:
    payload = {
        "selections": [
            {"index": 6, "team": 3, "result": "ACCEPT"},
            {"index": 4, "team": 1, "result": "CAPTAIN"},
            {"index": 5, "team": 2, "result": "DECLINE"},
        ]
    }
    rows = transforms.alliance_pick_rows(EVENT, payload)

    assert [(r["pick_ordinal"], r["team_number"], r["action"]) for r in rows] == [
        (4, 1, "CAPTAIN"),
        (5, 2, "DECLINE"),
        (6, 3, "ACCEPT"),
    ]
    assert "alliance_number" not in rows[0]


def test_a_log_with_no_index_falls_back_to_the_order_it_arrived_in() -> None:
    payload = {"selections": [{"team": 1, "result": "CAPTAIN"}, {"team": 2, "result": "ACCEPT"}]}

    assert [r["pick_ordinal"] for r in transforms.alliance_pick_rows(EVENT, payload)] == [0, 1]


def test_a_selection_result_the_enum_does_not_hold_is_dropped() -> None:
    payload = {
        "selections": [
            {"index": 0, "team": 1, "result": "CAPTAIN"},
            {"index": 1, "team": 2, "result": "SOMETHING_NEW"},
            {"index": 2, "team": None, "result": "ACCEPT"},
        ]
    }
    assert [r["team_number"] for r in transforms.alliance_pick_rows(EVENT, payload)] == [1]


def test_an_event_with_no_playoff_publishes_an_empty_selection() -> None:
    assert transforms.playoff_alliance_rows(EVENT, {"alliances": [], "count": 0}) == []
    assert transforms.alliance_pick_rows(EVENT, {"selections": [], "count": 0}) == []


# ------------------------------------------------------------------------------------------------------ ftcscout


def _scout_match(level: str = "Quals", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hasBeenPlayed": True,
        "tournamentLevel": level,
        "series": 0,
        "matchNum": 1,
        "description": "Q-1",
        "scheduledStartTime": "2026-01-10T14:00:00.000Z",
        "teams": [
            {"teamNumber": 11, "alliance": "Red", "station": "One", "surrogate": False, "noShow": False, "dq": False},
            {"teamNumber": 12, "alliance": "Red", "station": "Two", "surrogate": True, "noShow": False, "dq": False},
            {"teamNumber": 21, "alliance": "Blue", "station": "One", "surrogate": False, "noShow": True, "dq": False},
            {"teamNumber": 22, "alliance": "Blue", "station": "Two", "surrogate": False, "noShow": False, "dq": True},
        ],
        "scores": {
            "red": {"totalPoints": 120, "autoPoints": 30, "penaltyPointsCommitted": 5},
            "blue": {"totalPoints": 95, "autoPoints": 20, "penaltyPointsCommitted": 0},
        },
    }
    return base | overrides


@pytest.mark.parametrize(
    ("scout", "canonical"),
    [("Quals", "QUALIFICATION"), ("Semis", "SEMIFINAL"), ("Finals", "FINAL"), ("DoubleElim", "PLAYOFF")],
)
def test_ftcscouts_four_level_vocabulary_translates_to_the_enum(scout: str, canonical: str) -> None:
    assert transforms.scout_match_rows(EVENT, "UTC", [_scout_match(scout)])[0]["level"] == canonical


def test_a_level_ftcscout_spells_some_other_way_is_dropped_rather_than_guessed() -> None:
    assert transforms.scout_match_rows(EVENT, "UTC", [_scout_match("Practice")]) == []


def test_a_split_alliance_and_station_compose_into_one_slot_label() -> None:
    slots = transforms.scout_match_rows(EVENT, "UTC", [_scout_match()])[0]["teams"]

    assert [s["station"] for s in slots] == ["Red1", "Red2", "Blue1", "Blue2"]
    assert [s["alliance"] for s in slots] == ["RED", "RED", "BLUE", "BLUE"]


def test_the_per_slot_flags_survive_the_translation() -> None:
    by_station = {s["station"]: s for s in transforms.scout_match_rows(EVENT, "UTC", [_scout_match()])[0]["teams"]}

    assert (by_station["Red2"]["surrogate"], by_station["Blue1"]["no_show"], by_station["Blue2"]["dq"]) == (
        True,
        True,
        True,
    )


def test_ftcscout_publishes_utc_which_is_the_inverse_of_the_ftc_events_path() -> None:
    row = transforms.scout_match_rows(EVENT, "America/New_York", [_scout_match()])[0]

    assert row["start_time_utc"] == datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
    assert row["start_time_local"] == datetime(2026, 1, 10, 9, 0)
    assert row["start_time_local"].tzinfo is None
    assert row["modified_on_utc"] is None


@pytest.mark.parametrize(
    "match",
    [
        _scout_match(hasBeenPlayed=False),
        _scout_match(scores={}),
        _scout_match(teams=[{"teamNumber": 11, "alliance": "Solo", "station": "Solo"}]),
    ],
    ids=["unplayed", "unscored", "solo"],
)
def test_a_match_that_is_not_a_two_team_alliance_result_is_skipped(match: dict[str, Any]) -> None:
    assert transforms.scout_match_rows(EVENT, "UTC", [match]) == []


def _with_roles(match: dict[str, Any], roles: tuple[str, ...]) -> dict[str, Any]:
    for slot, role in zip(match["teams"], roles, strict=True):
        slot["allianceRole"] = role
    return match


SEATED = ("Captain", "FirstPick", "Captain", "FirstPick")


def test_an_alliance_role_is_a_playoff_fact_even_when_a_qualification_slot_carries_one() -> None:
    rows = transforms.scout_alliance_role_rows(
        EVENT,
        [_with_roles(_scout_match(), SEATED), _with_roles(_scout_match("DoubleElim", series=1), SEATED)],
    )

    assert [(r["station"], r["team_number"], r["alliance_role"]) for r in rows] == [
        ("Red1", 11, "Captain"),
        ("Red2", 12, "FirstPick"),
        ("Blue1", 21, "Captain"),
        ("Blue2", 22, "FirstPick"),
    ]


def test_a_role_the_enum_does_not_hold_is_dropped_rather_than_written() -> None:
    playoff = _with_roles(_scout_match("DoubleElim", series=1), ("Captain", "ThirdPick", "Captain", "FirstPick"))

    assert [r["team_number"] for r in transforms.scout_alliance_role_rows(EVENT, [playoff])] == [11, 21, 22]


def test_a_slot_ftcscout_has_no_role_for_is_dropped_rather_than_written_null() -> None:
    assert transforms.scout_alliance_role_rows(EVENT, [_scout_match("DoubleElim", series=1)]) == []
