"""Typed FIRST breakdowns for a rule pack's EPA partition."""

import hashlib
import json
import math

from compute import integer, number

from warehouse.rules.model import RulePack
from warehouse.rules.partition import RECONCILE_TOLERANCE, ResolvedPartition, resolve_partition


def partition_for(season: int, pack: RulePack | None) -> ResolvedPartition:
    if pack is None:
        return ResolvedPartition.total_only(season)
    if pack.season != season:
        raise ValueError("rule pack must match the replay season")
    partition = resolve_partition(season, [component.model_dump() for component in pack.components])
    selected = [component for component in pack.components if component.name in partition.names]
    if not selected or any(not c.is_fittable or c.level != "alliance" for c in selected):
        raise ValueError("component replay needs an alliance-level fittable partition")
    if len(selected) > 64:
        raise ValueError("component partition must not exceed 64 series")
    return partition


def version(pack: RulePack | None) -> str:
    if pack is None:
        return ""
    return hashlib.sha256(
        json.dumps(pack.model_dump(mode="json"), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def values(pack: RulePack, breakdown: dict, partition: ResolvedPartition | None = None) -> dict[str, float]:
    partition = partition or partition_for(pack.season, pack)
    by_name = {component.name: component for component in pack.components}
    return {
        column: _value(by_name, name, breakdown, set())
        for name, column in zip(partition.names, partition.columns, strict=True)
    }


def component_value(pack: RulePack, name: str, breakdown: dict) -> float:
    """Read a published scalar, including rule-pack recoveries for broken upstream totals."""
    return _value({c.name: c for c in pack.components}, name, breakdown, set())


def _value(by_name, name, breakdown, seen):
    if name in seen or name not in by_name:
        raise ValueError(f"unknown or cyclic scoring component {name}")
    component = by_name[name]
    if component.recovered_from:
        value = sum(_value(by_name, part, breakdown, seen | {name}) for part in component.recovered_from)
    elif component.state_scoring is not None:
        scoring = component.state_scoring
        if any(source not in breakdown for source in scoring.states_from):
            raise ValueError(f"missing robot state for {name}")
        value = sum(scoring.points.get(breakdown[source], 0.0) for source in scoring.states_from)
    elif name not in breakdown:
        raise ValueError(f"missing scoring component {name}")
    elif component.kind == "boolean":
        if not isinstance(breakdown[name], bool):
            raise ValueError(f"{name} must be boolean")
        value = float(breakdown[name])
    elif component.kind == "numeric":
        value = breakdown[name]
    else:
        raise ValueError(f"{name} is not a scalar scoring component")
    return number(value, name, minimum=-math.inf)


def attach(matches: list, scores: dict, pack: RulePack) -> None:
    """Require complete, reconciled data before replacing a saved event."""
    sides = {}
    partition = partition_for(pack.season, pack)
    known = {match.ordinal for match in matches}
    for row in scores["matchScores"]:
        if row["matchLevel"] not in ("QUALIFICATION", "QUAL", "QUALS") or row.get("matchSeries", 0) != 0:
            raise ValueError("component scores must be qualification-only with series zero")
        ordinal = integer(row["matchNumber"], "score match number", minimum=1)
        if ordinal not in known:
            raise ValueError("score breakdown does not match the qualification schedule")
        for alliance in row["alliances"]:
            if not isinstance(alliance, dict) or not isinstance(alliance.get("alliance"), str):
                raise ValueError("score alliances must be named objects")
            side = alliance["alliance"].lower()
            if side not in ("red", "blue") or (ordinal, side) in sides:
                raise ValueError("score alliances must be red and blue without duplicates")
            sides[ordinal, side] = alliance
    for match in matches:
        if match.red_score is None:
            continue
        for side in ("red", "blue"):
            raw = sides.get((match.ordinal, side))
            if raw is None:
                raise ValueError(f"match {match.ordinal} is missing its {side} breakdown")
            projected = values(pack, raw, partition)
            if abs(sum(projected.values()) - getattr(match, f"{side}_score")) > RECONCILE_TOLERANCE:
                raise ValueError(f"match {match.ordinal} {side} components do not reconcile with its non-foul score")
            setattr(match, f"{side}_components", projected)
