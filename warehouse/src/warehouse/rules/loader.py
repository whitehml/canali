"""Load rule-pack files into ``core.rule_pack`` and ``core.rule_pack_component``."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, delete
from sqlalchemy.dialects.postgresql import insert

from warehouse.rules.model import RulePack
from warehouse.schema import core

PACKS_DIR = Path(__file__).resolve().parents[3] / "rule_packs"

SEASONS: dict[int, tuple[str, str]] = {
    2022: ("2022-23", "POWERPLAY"),
    2023: ("2023-24", "CENTERSTAGE"),
    2024: ("2024-25", "INTO THE DEEP"),
    2025: ("2025-26", "DECODE"),
    2026: ("2026-27", "BIOBUZZ"),
}


def discover(packs_dir: Path | None = None) -> list[RulePack]:
    """Every pack in the directory, in season order."""
    directory = packs_dir or PACKS_DIR
    return sorted((RulePack.from_file(p) for p in directory.glob("*.toml")), key=lambda p: p.season)


def ensure_seasons(conn: Connection, seasons: Iterable[int]) -> None:
    """Write the `core.season` rows the packs reference."""
    rows = [
        {
            "season": year,
            "name": SEASONS.get(year, (str(year), ""))[0],
            "game": SEASONS.get(year, ("", ""))[1],
        }
        for year in sorted(set(seasons))
    ]
    if not rows:
        return
    stmt = insert(core.season).values(rows)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["season"],
            set_={"name": stmt.excluded.name, "game": stmt.excluded.game},
        )
    )


def load(conn: Connection, packs: Iterable[RulePack]) -> dict[int, int]:
    """Replace each pack's rows wholesale. Returns component counts by season."""
    packs = list(packs)
    ensure_seasons(conn, (p.season for p in packs))
    counts: dict[int, int] = {}
    now = datetime.now(UTC)

    for pack in packs:
        conn.execute(delete(core.rule_pack_component).where(core.rule_pack_component.c.season == pack.season))

        stmt = insert(core.rule_pack).values(
            season=pack.season,
            game=pack.game,
            version=pack.version,
            source_file=pack.source_file,
            rp_win=pack.ranking.rp_win,
            rp_tie=pack.ranking.rp_tie,
            rp_loss=pack.ranking.rp_loss,
            has_bonus_rp=pack.ranking.has_bonus_rp,
            alliance_size=pack.structure.alliance_size,
            ranking_formula=pack.ranking.formula,
            tiebreakers=pack.ranking.tiebreakers or None,
            playoff_structure=pack.structure.playoff_structure,
            alliance_brackets=[b.model_dump() for b in pack.structure.alliance_brackets] or None,
            playoff_implemented=pack.structure.playoff_implemented,
            advancement_implemented=pack.structure.advancement_implemented,
            loaded_at_utc=now,
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["season"],
                set_={c.name: stmt.excluded[c.name] for c in core.rule_pack.columns if c.name != "season"},
            )
        )

        if pack.components:
            conn.execute(
                insert(core.rule_pack_component).values(
                    [
                        {
                            "season": pack.season,
                            "name": c.name,
                            "level": c.level,
                            "kind": c.kind,
                            "is_subtotal": c.is_subtotal,
                            "is_derived": c.is_derived,
                            "partition_group": c.partition_group,
                            "recovered_from": c.recovered_from or None,
                            "column_name": c.column_name,
                        }
                        for c in pack.components
                    ]
                )
            )
        counts[pack.season] = len(pack.components)

    return counts


def load_from_disk(conn: Connection, packs_dir: Path | None = None) -> dict[int, int]:
    return load(conn, discover(packs_dir))
