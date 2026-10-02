"""Rebuilds the database's saved queries.

A view is a stored query that behaves like a table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from warehouse.rules.partition import RECONCILE_TOLERANCE

VIEWS_DIR = Path(__file__).resolve().parents[2] / "views"

MANAGED_SCHEMAS = ("raw", "core", "derived", "pub")

GRANTS_FILE = "grants.sql"

_CAST_BY_KIND: dict[str, str] = {
    "numeric": "::double precision",
    "boolean": "::boolean",
    "enum": "",
    "array": "",
}

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_JSON_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier from rule pack: {name!r}")
    return name


def _quote_json_key(name: str) -> str:
    if not _JSON_KEY_RE.match(name):
        raise ValueError(f"unsafe JSON key from rule pack: {name!r}")
    return name


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def state_scoring_sql(scoring: Mapping[str, Any], cast: str) -> str:
    """The sum, over each robot, of the points its published state is worth."""
    whens = " ".join(
        f"WHEN {_quote_literal(state)} THEN {float(value)!r}" for state, value in scoring["points"].items()
    )
    terms = [
        f"CASE mb.breakdown ->> '{_quote_json_key(source)}' {whens} ELSE 0 END" for source in scoring["states_from"]
    ]
    return f"(({' + '.join(terms)}){cast})"


def drop_all_views(conn: Connection) -> int:
    """Drop every view in the managed schemas. Returns the number dropped."""
    schemas = ", ".join(f"'{s}'" for s in MANAGED_SCHEMAS)
    rows = conn.execute(
        text(
            f"""
            SELECT schemaname, viewname FROM pg_views WHERE schemaname IN ({schemas})
            UNION ALL
            SELECT schemaname, matviewname FROM pg_matviews WHERE schemaname IN ({schemas})
            """
        )
    ).all()
    for schema, name in rows:
        # CASCADE, because views build on views.
        conn.execute(text(f'DROP VIEW IF EXISTS "{schema}"."{name}" CASCADE'))
    return len(rows)


def _static_view_sql(views_dir: Path) -> list[tuple[str, str]]:
    """View files in filename order; the numeric prefix is the dependency order."""
    files = sorted(p for p in views_dir.glob("*.sql") if p.name != GRANTS_FILE)
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def generated_view_sql(conn: Connection) -> list[tuple[str, str]]:
    """Emit one typed view per season from ``core.rule_pack_component``.

    Consumers get ``v_breakdown_2025.auto_points`` rather than a JSON extraction expression.
    """
    seasons = conn.execute(text("SELECT DISTINCT season FROM core.rule_pack_component ORDER BY season")).scalars().all()

    out: list[tuple[str, str]] = []
    phase_seasons: list[int] = []
    for season in seasons:
        components = conn.execute(
            text(
                """
                SELECT name, column_name, kind, recovered_from, state_scoring, phase
                FROM core.rule_pack_component
                WHERE season = :season ORDER BY column_name
                """
            ),
            {"season": season},
        ).all()

        projections: list[str] = []
        phased = [(column_name, phase) for _, column_name, _, _, _, phase in components if phase]
        for name, column_name, kind, recovered_from, state_scoring, _phase in components:
            cast = _CAST_BY_KIND[kind]
            if state_scoring:
                # The API publishes the robot states but not what they score.
                projections.append(f"    {state_scoring_sql(state_scoring, cast)} AS {_quote_ident(column_name)}")
                continue
            if recovered_from:
                # The API publishes this field but not its value. Sum the summands the pack names instead.
                # `core.match_breakdown` still holds what FIRST sent.
                summands = " + ".join(
                    f"COALESCE((mb.breakdown ->> '{_quote_json_key(s)}'){cast}, 0)" for s in recovered_from
                )
                projections.append(f"    ({summands}) AS {_quote_ident(column_name)}")
                continue
            accessor = "->" if kind == "array" else "->>"
            projections.append(f"    (mb.breakdown {accessor} '{name}'){cast} AS {_quote_ident(column_name)}")

        view = f"v_breakdown_{season}"
        body = ",\n".join(
            [
                "    mb.match_id",
                "    m.event_id",
                "    e.season",
                "    m.level",
                "    m.series",
                "    m.match_number",
                "    mb.alliance",
                *projections,
            ]
        )
        out.append(
            (
                f"generated:{view}",
                f"CREATE VIEW pub.{view} AS\nSELECT\n{body}\n"
                f"FROM core.match_breakdown mb\n"
                f"JOIN core.match m ON m.match_id = mb.match_id\n"
                f"JOIN core.event e ON e.event_id = m.event_id\n"
                f"WHERE e.season = {int(season)};",
            )
        )
        by_phase: dict[str, list[str]] = {}
        for column_name, phase in phased:
            by_phase.setdefault(phase, []).append(column_name)
        if by_phase:
            out.append(phase_sum_view_sql(int(season), by_phase))
            phase_seasons.append(int(season))
    out.append(unratable_view_sql(phase_seasons))
    return out


def phase_sum_view_sql(season: int, columns_by_phase: dict[str, list[str]]) -> tuple[str, str]:
    """One view per season: the match's auto points and teleop points, each summed from its finest-grain leaves."""

    def total(phase: str) -> str:
        columns = columns_by_phase.get(phase, [])
        return " + ".join(f"COALESCE(b.{_quote_ident(c)}, 0)" for c in columns) or "0"

    view = f"v_phase_points_{season}"
    return (
        f"generated:{view}",
        f"CREATE VIEW pub.{view} AS\n"
        f"SELECT b.match_id, b.event_id, b.season, b.level, b.series, b.match_number, b.alliance,\n"
        f"    ({total('auto')})::double precision AS auto_sum,\n"
        f"    ({total('teleop')})::double precision AS teleop_sum,\n"
        f"    ({total('auto')} + {total('teleop')})::double precision AS total_sum\n"
        f"FROM pub.v_breakdown_{season} b;",
    )


_NON_FOUL = (
    "CASE p.alliance WHEN 'RED' THEN m.score_red_final - coalesce(m.score_blue_foul, 0) "
    "ELSE m.score_blue_final - coalesce(m.score_red_foul, 0) END"
)


def unratable_view_sql(seasons: Sequence[int]) -> tuple[str, str]:
    """Matches whose breakdown does not add up to the official non-foul score."""

    columns = "m.match_id, m.event_id, m.level, m.series, m.match_number"
    branches = [
        f"SELECT {columns}, {int(season)} AS season, max(abs(p.total_sum - ({_NON_FOUL}))) AS worst_gap\n"
        f"FROM core.match m\n"
        f"JOIN pub.v_phase_points_{int(season)} p ON p.match_id = m.match_id\n"
        f"WHERE m.score_red_final IS NOT NULL AND m.score_blue_final IS NOT NULL\n"
        f"  AND EXISTS (SELECT 1 FROM core.match_team mt\n"
        f"              WHERE mt.match_id = m.match_id AND mt.alliance = p.alliance AND NOT mt.no_show)\n"
        f"  AND abs(p.total_sum - ({_NON_FOUL})) > {RECONCILE_TOLERANCE!r}\n"
        f"GROUP BY {columns}"
        for season in seasons
    ]
    if not branches:
        branches = [f"SELECT {columns}, 0 AS season, 0::double precision AS worst_gap FROM core.match m WHERE false"]
    return "generated:v_match_unratable", "CREATE VIEW pub.v_match_unratable AS\n" + "\nUNION ALL\n".join(
        branches
    ) + ";"


def _rule_packs_present(conn: Connection) -> bool:
    return bool(conn.execute(text("SELECT to_regclass('core.rule_pack_component') IS NOT NULL")).scalar())


def rebuild(conn: Connection, views_dir: Path | None = None) -> list[str]:
    """Drop every view and re-run the directory."""
    drop_all_views(conn)
    if not _rule_packs_present(conn):
        return []

    directory = views_dir or VIEWS_DIR
    applied: list[str] = []
    for label, sql in [*_static_view_sql(directory), *generated_view_sql(conn)]:
        conn.execute(text(sql))
        applied.append(label)

    grants = directory / GRANTS_FILE
    if grants.exists():
        conn.execute(text(grants.read_text(encoding="utf-8")))
        applied.append(GRANTS_FILE)
    return applied
