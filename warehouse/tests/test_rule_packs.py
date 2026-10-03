"""Rule-pack parsing, validation and generation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from warehouse import views
from warehouse.rules import generate, loader
from warehouse.rules.model import BonusRp, Component, RulePack, to_column_name
from warehouse.rules.partition import resolve_partition, select_partition
from warehouse.rules.validate import BreakdownValidationError, validate_breakdown

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_PACKS = FIXTURES / "rule_packs"
FIXTURE_OPENAPI = FIXTURES / "openapi_scores.json"


def _by_season() -> dict[int, RulePack]:
    return {p.season: p for p in loader.discover()}


def _synthetic_components() -> list[Component]:
    return RulePack.from_file(FIXTURE_PACKS / "9999_synthetic.toml").components


def _openapi_components(season: int = 9999) -> dict[str, Component]:
    document = generate.load_openapi_document(FIXTURE_OPENAPI)
    return {c.name: c for c in generate.components_from_openapi(document, season)}


# ------------------------------------------------------------------- parsing


def test_a_pack_file_parses_into_its_typed_objects() -> None:
    pack = RulePack.from_file(FIXTURE_PACKS / "9999_synthetic.toml")
    assert pack.source_file == "9999_synthetic.toml"
    assert [(b.min_teams, b.max_teams, b.alliances) for b in pack.structure.alliance_brackets] == [
        (4, 12, 2),
        (13, 40, 4),
    ]
    by_name = {c.name: c for c in pack.components}
    assert by_name["autoWidgetPoints"].partition_group == "phase"
    assert by_name["preFlangeTotal"].recovered_from == ["autoWidgetPoints", "teleopWidgetPoints"]


def test_a_pack_that_declares_nothing_optional_takes_the_defaults() -> None:
    pack = RulePack.model_validate({"season": 1, "game": "X", "components": [{"name": "widgets", "kind": "numeric"}]})
    assert pack.ranking.rp_win is None
    assert pack.ranking.tiebreakers == []
    assert (pack.structure.alliance_size, pack.structure.playoff_implemented) == (2, False)
    assert pack.components[0].partition_group is None
    assert pack.components[0].recovered_from == []


def test_column_names_are_derived_from_the_published_camel_case() -> None:
    assert to_column_name("autoArtifactPoints") == "auto_artifact_points"
    assert to_column_name("robot1Teleop") == "robot1_teleop"
    assert to_column_name("dcJunctionConePoints") == "dc_junction_cone_points"
    assert to_column_name("teleopRPScored") == "teleop_rp_scored"


def test_every_shipped_pack_parses() -> None:
    """A pack file that stops parsing, or goes missing, fails here rather than at ingest."""
    assert {p.season for p in loader.discover()} == set(loader.SEASONS)


def test_declared_tiebreak_components_exist_in_the_pack() -> None:
    """A tiebreaker naming a missing component evaluates to zero for every team."""
    for pack in loader.discover():
        names = {c.name for c in pack.components}
        for key in pack.ranking.tiebreakers:
            _, _, component = key.partition(":")
            if component:
                assert component in names, f"{pack.season}: {component!r} not a component"


# ---------------------------------------------------------------- rejections


def test_a_pack_with_colliding_column_names_is_rejected() -> None:
    with pytest.raises(ValueError, match="both map to column"):
        RulePack.model_validate(
            {
                "season": 1,
                "game": "X",
                "components": [
                    {"name": "autoPoints", "kind": "numeric"},
                    {"name": "auto_points", "kind": "numeric"},
                ],
            }
        )


def test_alliance_brackets_must_not_overlap() -> None:
    with pytest.raises(ValueError, match="ascending and non-overlapping"):
        RulePack.model_validate(
            {
                "season": 1,
                "game": "X",
                "structure": {
                    "alliance_brackets": [
                        {"min_teams": 4, "max_teams": 20, "alliances": 2},
                        {"min_teams": 11, "max_teams": 40, "alliances": 6},
                    ]
                },
            }
        )


def _bonus_rp(thresholds: dict[str, int]) -> BonusRp:
    return BonusRp.model_validate({"component": "flagRP", "sum_of": ["flagPoints"], "thresholds": thresholds})


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        (None, 10),
        ("Qualifier", 10),
        ("League Tournament", 10),
        ("Super Qualifier", 10),
        ("Championship", 20),
        ("FIRST Championship", 30),
    ],
)
def test_a_bonus_rp_threshold_follows_the_rp_level_not_the_tier(event_type: str | None, expected: int) -> None:
    assert _bonus_rp({"REG": 10, "RCMP": 20, "CMP": 30}).threshold(event_type) == expected
    assert _bonus_rp({"REG": 10}).threshold(event_type) == 10


def test_a_threshold_key_that_is_not_an_rp_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="thresholds"):
        _bonus_rp({"REG": 16, "Championship": 21})


def test_a_recovered_component_must_be_numeric_and_derived() -> None:
    with pytest.raises(ValueError, match="only numeric components"):
        Component.model_validate({"name": "x", "kind": "boolean", "is_derived": True, "recovered_from": ["a"]})
    with pytest.raises(ValueError, match="never fitted"):
        Component.model_validate({"name": "x", "kind": "numeric", "recovered_from": ["a"]})


def test_a_recovery_summand_must_be_declared_by_the_pack() -> None:
    with pytest.raises(ValueError, match="does not declare"):
        RulePack.model_validate(
            {
                "season": 1,
                "game": "X",
                "components": [{"name": "total", "kind": "numeric", "is_derived": True, "recovered_from": ["missing"]}],
            }
        )


def _state_scored(**overrides: object) -> dict[str, object]:
    return {
        "name": "autoParkPoints",
        "kind": "numeric",
        "state_scoring": {"states_from": ["robot1Auto", "robot2Auto"], "points": {"PARKED": 3}},
        **overrides,
    }


def test_a_state_scored_component_is_a_numeric_alliance_leaf_that_is_fitted() -> None:
    component = Component.model_validate(_state_scored())
    assert component.is_fittable
    for bad in ({"kind": "boolean"}, {"level": "team"}, {"is_derived": True}):
        with pytest.raises(ValueError, match="numeric alliance leaf"):
            Component.model_validate(_state_scored(**bad))


def test_state_scoring_reads_states_from_declared_team_level_enums() -> None:
    robots = [{"name": f"robot{i}Auto", "level": "team", "kind": "enum"} for i in (1, 2)]
    pack = RulePack.model_validate({"season": 1, "game": "X", "components": [*robots, _state_scored()]})
    assert pack.components[-1].state_scoring is not None
    with pytest.raises(ValueError, match="does not declare"):
        RulePack.model_validate({"season": 1, "game": "X", "components": [robots[0], _state_scored()]})
    wrong = [{"name": "robot1Auto", "level": "team", "kind": "enum"}, {"name": "robot2Auto", "kind": "numeric"}]
    with pytest.raises(ValueError, match="cannot be scored from"):
        RulePack.model_validate({"season": 1, "game": "X", "components": [*wrong, _state_scored()]})


def test_into_the_deep_prices_auto_parking_from_the_robot_states() -> None:
    pack = _by_season()[2024]
    park = next(c for c in pack.components if c.name == "autoParkPoints")
    assert park.is_fittable and park.is_subtotal and park.state_scoring is not None
    assert park.state_scoring.states_from == ["robot1Auto", "robot2Auto"]
    assert park.state_scoring.points == {"OBSERVATION_ZONE": 3, "ASCENT": 3}


def test_only_a_numeric_alliance_subtotal_belongs_to_a_phase() -> None:
    Component.model_validate({"name": "autoPoints", "kind": "numeric", "is_subtotal": True, "phase": "auto"})
    with pytest.raises(ValueError, match="belongs to a phase"):
        Component.model_validate({"name": "autoCones", "kind": "numeric", "phase": "auto"})
    with pytest.raises(ValueError, match="belongs to a phase"):
        Component.model_validate({"name": "robot1Auto", "kind": "enum", "level": "team", "phase": "auto"})


def _row(name: str, group: str | None) -> dict[str, object]:
    return {"name": name, "column_name": to_column_name(name), "partition_group": group}


def test_a_season_fits_its_leaf_group_when_it_declares_both() -> None:
    both = [_row("autoPoints", "phase"), _row("autoConePoints", "leaf"), _row("autoNavPoints", "leaf")]
    assert select_partition(1, both) == ("autoConePoints", "autoNavPoints")
    assert select_partition(1, both, group="phase") == ("autoPoints",)
    with pytest.raises(ValueError, match="not 'mechanism'"):
        select_partition(1, both, group="mechanism")


def test_a_resolved_partition_carries_each_name_with_its_column() -> None:
    partition = resolve_partition(1, [_row("autoNavPoints", "leaf"), _row("autoConePoints", "leaf")])
    assert partition.names == ("autoConePoints", "autoNavPoints")
    assert partition.columns == ("auto_cone_points", "auto_nav_points")


def test_a_season_with_one_other_group_fits_it_and_with_two_other_groups_must_choose() -> None:
    assert select_partition(1, [_row("autoPoints", "phase")]) == ("autoPoints",)
    assert select_partition(1, [_row("autoPoints", None)]) == ()
    with pytest.raises(ValueError, match="decision, not a sort order"):
        select_partition(1, [_row("a", "phase"), _row("b", "mechanism")])


@pytest.mark.parametrize("season", [2022, 2023, 2024, 2025])
def test_every_season_declares_its_leaves_with_a_phase_each(season: int) -> None:
    pack = _by_season()[season]
    leaves = [c for c in pack.components if c.partition_group == "leaf"]
    assert leaves and all(c.is_fittable and c.is_subtotal and c.phase in ("auto", "teleop") for c in leaves)
    assert {c.phase for c in leaves} == {"auto", "teleop"}
    assert all(c.phase is None for c in pack.components if c.partition_group != "leaf")


def test_the_phase_view_sums_each_phase_from_its_leaves() -> None:
    label, sql = views.phase_sum_view_sql(2024, {"auto": ["auto_sample_points", "auto_park_points"], "teleop": ["t"]})
    assert label == "generated:v_phase_points_2024"
    assert "COALESCE(b.auto_sample_points, 0) + COALESCE(b.auto_park_points, 0)" in sql
    assert "FROM pub.v_breakdown_2024 b" in sql
    _, only_auto = views.phase_sum_view_sql(2025, {"auto": ["a"]})
    assert "(0)::double precision AS teleop_sum" in only_auto


def test_the_unratable_view_checks_each_phased_season_and_is_empty_without_one() -> None:
    _, sql = views.unratable_view_sql([2024, 2025])
    assert "JOIN pub.v_phase_points_2024 p" in sql and "JOIN pub.v_phase_points_2025 p" in sql
    assert sql.count("UNION ALL") == 1
    assert "NOT mt.no_show" in sql
    _, empty = views.unratable_view_sql([])
    assert "WHERE false" in empty and "v_phase_points" not in empty


# ---------------------------------------------------------------- validation


def test_a_declared_component_with_the_wrong_shape_is_an_error() -> None:
    result = validate_breakdown({"flangeRP": 3}, _synthetic_components())
    assert not result.ok
    assert "expected boolean" in result.errors[0]


def test_an_undeclared_key_is_ignored() -> None:
    result = validate_breakdown({"autoWidgetPoints": 10, "brandNewThing": 4}, _synthetic_components())
    assert result.ok
    assert result.unknown_keys == ["brandNewThing"]


def test_the_vestigial_team_key_is_not_reported_forever() -> None:
    result = validate_breakdown({"team": 0}, _synthetic_components())
    assert result.ok
    assert result.unknown_keys == []


def test_a_null_value_is_not_a_type_error() -> None:
    """A component the API declares but leaves unset says nothing about its type."""
    result = validate_breakdown({"flangeRP": None, "gizmoLatticeState": None}, _synthetic_components())
    assert result.ok


def test_a_valid_breakdown_passes() -> None:
    result = validate_breakdown(
        {
            "autoWidgetPoints": 42,
            "teleopWidgetPoints": 8,
            "robot1Sprocket": 7,
            "flangeRP": True,
            "robot2Perch": "HIGH",
            "gizmoLatticeState": ["A", "B"],
            "totalPoints": 200,
        },
        _synthetic_components(),
    )
    assert result.ok, result.errors


def test_raise_if_invalid_names_the_context_it_was_given() -> None:
    result = validate_breakdown({"flangeRP": 3}, _synthetic_components())
    with pytest.raises(BreakdownValidationError, match="match 17 red"):
        result.raise_if_invalid("match 17 red")


# ---------------------------------------------------------------- generation


def test_generation_reads_every_kind_off_the_document() -> None:
    components = _openapi_components()
    kinds = {name: c.kind for name, c in components.items()}
    assert kinds["autoWidgetPoints"] == "numeric"
    assert kinds["widgetTally"] == "numeric"
    assert kinds["flangeRP"] == "boolean"
    assert kinds["gizmoLatticeState"] == "array"
    assert kinds["robot2Perch"] == "enum"
    assert kinds["sprocketMode"] == "enum"
    assert kinds["widgetGrade"] == "enum"


def test_the_side_label_and_the_vestigial_team_key_are_not_components() -> None:
    assert {"alliance", "team"}.isdisjoint(_openapi_components())


def test_derived_totals_are_marked_and_never_fittable() -> None:
    components = _openapi_components()
    for name in ("totalPoints", "foulPointsCommitted"):
        assert components[name].is_derived, name
        assert not components[name].is_fittable, name
        assert not components[name].is_subtotal, name
    assert components["autoWidgetPoints"].is_subtotal


def test_per_robot_fields_are_team_level() -> None:
    """They live in the alliance blob but describe one robot."""
    components = _openapi_components()
    assert components["robot1Sprocket"].level == "team"
    assert components["robot2Perch"].level == "team"
    assert components["autoWidgetPoints"].level == "alliance"


def test_single_player_models_are_ignored() -> None:
    document = generate.load_openapi_document(FIXTURE_OPENAPI)
    assert generate.available_seasons(document) == [9998, 9999]
    assert "soloWidgetPoints" not in _openapi_components()


def test_a_season_the_document_does_not_describe_names_what_it_does() -> None:
    document = generate.load_openapi_document(FIXTURE_OPENAPI)
    with pytest.raises(KeyError, match="ScoreDetailAllianceModel_9998"):
        generate.components_from_openapi(document, 1997)


def test_rendered_toml_parses_back_into_the_same_components() -> None:
    components = list(_openapi_components().values())
    rendered = generate.render_components_toml(components)
    reparsed = RulePack.model_validate({"season": 9999, "game": "X", **tomllib.loads(rendered)})
    assert [c.model_dump() for c in reparsed.components] == [c.model_dump() for c in components]


# ------------------------------------------------------- the genericity test


@pytest.mark.db
def test_a_synthetic_pack_of_invented_components_flows_end_to_end(engine: Engine) -> None:
    with engine.begin() as conn:
        counts = loader.load_from_disk(conn, FIXTURE_PACKS)
        views.rebuild(conn)
    assert counts == {9999: 8}

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_schema = 'pub' AND table_name = 'v_breakdown_9999'
                    """
                )
            ).all()
            columns: dict[str, str] = {row.column_name: row.data_type for row in rows}
        assert columns["auto_widget_points"] == "double precision"
        assert columns["flange_rp"] == "boolean"
        assert columns["robot2_perch"] == "text"
        assert columns["gizmo_lattice_state"] == "jsonb"
        assert columns["pre_flange_total"] == "double precision"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM core.rule_pack_component WHERE season = 9999"))
            conn.execute(text("DELETE FROM core.rule_pack WHERE season = 9999"))
            conn.execute(text("DELETE FROM core.season WHERE season = 9999"))
            views.rebuild(conn)


@pytest.mark.db
def test_state_scoring_sums_what_each_robot_state_is_worth(engine: Engine) -> None:
    scoring = {"states_from": ["robot1Auto", "robot2Auto"], "points": {"OBSERVATION_ZONE": 3, "ASCENT": 3}}
    expression = views.state_scoring_sql(scoring, "::double precision")

    def score(breakdown: str) -> float:
        with engine.connect() as conn:
            value = conn.execute(
                text(f"SELECT {expression} FROM (SELECT CAST(:b AS jsonb) AS breakdown) mb"), {"b": breakdown}
            )
            return float(value.scalar_one())

    assert score('{"robot1Auto": "NONE", "robot2Auto": "NONE"}') == 0.0
    assert score('{"robot1Auto": "NONE", "robot2Auto": "OBSERVATION_ZONE"}') == 3.0
    assert score('{"robot1Auto": "ASCENT", "robot2Auto": "OBSERVATION_ZONE"}') == 6.0
    assert score('{"robot1Auto": "ASCENT"}') == 3.0
    assert score("{}") == 0.0
