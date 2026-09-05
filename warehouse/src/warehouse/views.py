"""Rebuilds the database's saved queries.

A view is a stored query that behaves like a table.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Connection, text

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
    for season in seasons:
        components = conn.execute(
            text(
                """
                SELECT name, column_name, kind, recovered_from
                FROM core.rule_pack_component
                WHERE season = :season ORDER BY column_name
                """
            ),
            {"season": season},
        ).all()

        projections: list[str] = []
        for name, column_name, kind, recovered_from in components:
            cast = _CAST_BY_KIND[kind]
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
    return out


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
