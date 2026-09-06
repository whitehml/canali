"""Rule-pack parsing, validation and generation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from warehouse import views
from warehouse.rules import generate, loader
from warehouse.rules.model import Component, RulePack, to_column_name
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
