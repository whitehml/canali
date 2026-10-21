"""D1 checkpoint publication guarded by input revision, engine version and step."""

import hashlib
import json
import math

from engine_version import ENGINE_VERSION
from forecast import advance
from pydantic import Field, PositiveInt
from season import source_current
from snapshot import EventSnapshot, InputModel

MAX_BODY = 512_000

INITIALIZE = (
    "INSERT INTO forecast_state(event_code, input_revision, engine_version) "
    "SELECT event_code, revision, ? FROM forecast_input i WHERE event_code = ? AND revision = ? AND "
    + source_current("i")
    + " "
    "ON CONFLICT(event_code) DO UPDATE SET input_revision = excluded.input_revision, "
    "engine_version = excluded.engine_version, step = 0, checkpoint = '{}', status = 'running', result = NULL, "
    "computed_at = NULL "
    "WHERE forecast_state.input_revision != excluded.input_revision "
    "OR forecast_state.engine_version != excluded.engine_version"
)
READ_STATE = (
    "SELECT s.* FROM forecast_state s JOIN forecast_input i "
    "ON i.event_code = s.event_code AND i.revision = s.input_revision "
    "WHERE s.event_code = ? AND s.engine_version = ? AND " + source_current("i")
)
EXPORT = (
    "SELECT i.document AS snapshot, s.input_revision, s.engine_version, s.status, s.result, s.computed_at "
    "FROM forecast_state s JOIN forecast_input i "
    "ON i.event_code = s.event_code AND i.revision = s.input_revision "
    "WHERE s.event_code = ? AND s.engine_version = ? AND " + source_current("i")
)
SAVE_STATE = (
    "UPDATE forecast_state SET checkpoint = ?, status = ?, result = ?, step = step + 1, "
    "computed_at = CURRENT_TIMESTAMP "
    "WHERE event_code = ? AND input_revision = ? AND engine_version = ? AND step = ? "
    "AND EXISTS (SELECT 1 FROM forecast_input i WHERE event_code = ? AND revision = ? AND "
    + source_current("i")
    + ") RETURNING step"
)

SAVE_RESULT = (
    "INSERT INTO forecast_state(event_code, input_revision, engine_version, status, result, computed_at) "
    "SELECT event_code, revision, ?, 'complete', ?, CURRENT_TIMESTAMP FROM forecast_input i "
    "WHERE event_code = ? AND revision = ? AND document = ? AND " + source_current("i") + " "
    "ON CONFLICT(event_code) DO UPDATE SET input_revision = excluded.input_revision, "
    "engine_version = excluded.engine_version, checkpoint = '{}', step = forecast_state.step + 1, "
    "status = 'complete', result = excluded.result, computed_at = excluded.computed_at RETURNING input_revision"
)


class TeamResult(InputModel):
    p_attend: float = Field(ge=0, le=1)
    mean_rank: float = Field(ge=1)
    rank_probabilities: list[float] = Field(min_length=1, max_length=128)
    p_captain: float = Field(ge=0, le=1)
    p_selected: float = Field(ge=0, le=1)
    p_win: float = Field(ge=0, le=1)
    p_advance: float | None = Field(ge=0, le=1)
    p_advance_first_championship: float | None = Field(ge=0, le=1)


class LocalResult(InputModel):
    revision: PositiveInt
    engine_version: str
    snapshot_sha256: str
    iterations: PositiveInt
    standard_error: float = Field(ge=0, le=0.5)
    teams: dict[PositiveInt, TeamResult]


def snapshot_hash(snapshot: EventSnapshot) -> str:
    return hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()


async def upload_result(db, request, code: str) -> tuple[dict, int]:
    """Accept native computations only for the exact current snapshot and engine."""
    if request.method != "PUT":
        return {"error": "method not allowed"}, 405
    body = await request.text()
    if len(body.encode()) > MAX_BODY:
        return {"error": "input exceeds 512000 bytes"}, 413
    result = LocalResult.model_validate_json(body)
    row = await db.prepare("SELECT revision, document FROM forecast_input WHERE event_code = ?").bind(code).first()
    if row is None:
        return {"error": "event not found"}, 404
    snapshot = EventSnapshot.model_validate_json(row.document)
    if (
        result.revision != row.revision
        or result.engine_version != ENGINE_VERSION
        or result.snapshot_sha256 != snapshot_hash(snapshot)
    ):
        return {"error": "input or engine changed; recompute the current snapshot"}, 409
    if not snapshot.min_iterations <= result.iterations <= snapshot.max_iterations:
        raise ValueError("iterations outside snapshot limits")
    if result.standard_error > snapshot.tolerance:
        raise ValueError("forecast has not converged")
    if set(result.teams) != set(snapshot.field.predicted):
        raise ValueError("result teams must match snapshot teams")
    for team in result.teams.values():
        ranks = team.rank_probabilities
        if (
            team.p_attend != 1
            or len(ranks) > len(result.teams)
            or any(not 0 <= p <= 1 for p in ranks)
            or not math.isclose(sum(ranks), 1, abs_tol=1e-9)
            or not math.isclose(sum(i * p for i, p in enumerate(ranks, 1)), team.mean_rank, abs_tol=1e-9)
            or (team.p_advance is None) != (snapshot.slots is None)
            or (team.p_advance_first_championship is None)
            != (snapshot.slots is None or snapshot.slots.first_championship is None)
        ):
            raise ValueError("inconsistent team probabilities or ranks")
    summary = result.model_dump(mode="json", include={"iterations", "standard_error", "teams"})
    report = {
        "status": "complete",
        "iterations": result.iterations,
        "standard_error": result.standard_error,
        "result": summary,
    }
    saved = (
        await db.prepare(SAVE_RESULT)
        .bind(ENGINE_VERSION, json.dumps(report, allow_nan=False), code, result.revision, row.document)
        .first()
    )
    if saved is None:
        return {"error": "input or season changed; rebuild the forecast"}, 409
    return {"event": code, "revision": result.revision, **report}, 200


def stored_report(row) -> dict:
    report = json.loads(row.result) if row.result else {"status": "running", "iterations": 0, "result": None}
    return {"event": row.event_code, "revision": row.input_revision, "step": row.step, **report}


async def export(db, request, code: str) -> tuple[dict, int]:
    """Read the matching input and forecast atomically; never export obsolete computations."""
    if request.method != "GET":
        return {"error": "method not allowed"}, 405
    row = await db.prepare(EXPORT).bind(code, ENGINE_VERSION).first()
    if row is None:
        return {"error": "no current forecast"}, 404
    report = json.loads(row.result) if row.result else {}
    return {
        "event": code,
        "revision": row.input_revision,
        "engine_version": row.engine_version,
        "status": row.status,
        "snapshot": json.loads(row.snapshot) if row.status == "complete" else None,
        "forecast": report.get("result") if row.status == "complete" else None,
        "snapshot_sha256": hashlib.sha256(row.snapshot.encode()).hexdigest() if row.status == "complete" else None,
        "computed_at": row.computed_at,
    }, 200


async def handle(db, request, code: str, run: bool) -> tuple[dict, int]:
    if request.method == "PUT" and not run:
        return await upload(db, request, code)
    if request.method == "GET" and not run:
        row = await db.prepare(READ_STATE).bind(code, ENGINE_VERSION).first()
        if row is None:
            return {"error": "no forecast for current inputs and engine"}, 404
        return stored_report(row), 200
    if request.method != "POST" or not run:
        return {"error": "method not allowed"}, 405
    return await step(db, code)


async def upload(db, request, code: str) -> tuple[dict, int]:
    body = await request.text()
    if len(body.encode()) > MAX_BODY:
        return {"error": "input exceeds 512000 bytes"}, 413
    snapshot = EventSnapshot.model_validate_json(body)
    return await save_input(db, code, snapshot)


async def save_input(db, code: str, snapshot: EventSnapshot) -> tuple[dict, int]:
    """Publish a snapshot only while its linked season revision remains current."""
    document = snapshot.model_dump_json()
    row = (
        await db.prepare(
            "INSERT INTO forecast_input(event_code, revision, document) "
            "SELECT ?, 1, i.document FROM (SELECT ? AS document) i WHERE " + source_current("i") + " "
            "ON CONFLICT(event_code) DO UPDATE SET revision = revision + "
            "CASE WHEN forecast_input.document != excluded.document THEN 1 ELSE 0 END, "
            "document = excluded.document RETURNING revision"
        )
        .bind(code, document)
        .first()
    )
    if row is None:
        return {"error": "season changed; rebuild snapshot with current ratings"}, 409
    return {"event": code, "revision": row.revision}, 200


async def step(db, code: str, *, expected_revision=None, expected_engine=None) -> tuple[dict, int]:
    source = (
        await db.prepare(
            "SELECT revision, document FROM forecast_input i WHERE event_code = ? AND " + source_current("i")
        )
        .bind(code)
        .first()
    )
    if source is None:
        return {"error": "event not found"}, 404
    revision = source.revision
    if (expected_revision is not None and expected_revision != revision) or (
        expected_engine is not None and expected_engine != ENGINE_VERSION
    ):
        return {"error": "input or engine changed"}, 409
    await db.prepare(INITIALIZE).bind(ENGINE_VERSION, code, revision).run()
    state = await db.prepare(READ_STATE).bind(code, ENGINE_VERSION).first()
    if state is None or state.input_revision != revision:
        return {"error": "input changed; retry the current revision"}, 409
    if state.status != "running":
        return stored_report(state), 200
    snapshot = EventSnapshot.model_validate_json(source.document)
    checkpoint, report = advance(snapshot, json.loads(state.checkpoint))
    saved = (
        await db.prepare(SAVE_STATE)
        .bind(
            json.dumps(checkpoint, allow_nan=False),
            report["status"],
            json.dumps(report, allow_nan=False),
            code,
            revision,
            ENGINE_VERSION,
            state.step,
            code,
            revision,
        )
        .first()
    )
    if saved is None:
        return {"error": "input or checkpoint changed; retry the current revision"}, 409
    status = 202 if report["status"] == "running" else 200
    return {"event": code, "revision": revision, "step": saved.step, **report}, status
