"""Durable per-event ownership and recoverable Workflow dispatch."""

import hashlib
import logging

from engine_version import ENGINE_VERSION
from forecast_store import step as forecast_step
from season import source_current

# Leave room under the Workflow step limit; long forecasts continue in a new instance.
BATCHES_PER_INSTANCE = 256
TERMINAL = ("complete", "exhausted", "superseded", "errored", "yielded")
CLAIM = (
    "INSERT INTO forecast_job(event_code, input_revision, engine_version, generation, instance_id, status) "
    "SELECT event_code, revision, ?, ?, ?, 'pending' FROM forecast_input i "
    "WHERE event_code = ? AND revision = ? AND " + source_current("i") + " "
    "ON CONFLICT(event_code) DO UPDATE SET input_revision = excluded.input_revision, "
    "engine_version = excluded.engine_version, generation = excluded.generation, "
    "instance_id = excluded.instance_id, status = 'pending', updated_at = CURRENT_TIMESTAMP "
    "WHERE forecast_job.status IN ('complete', 'exhausted', 'superseded', 'yielded') "
    "AND (forecast_job.input_revision != excluded.input_revision "
    "OR forecast_job.engine_version != excluded.engine_version "
    "OR (forecast_job.status = 'yielded' AND excluded.generation = forecast_job.generation + 1)) "
    "RETURNING *"
)


def instance_id(code, revision, engine, generation=0):
    key = f"{code}:{revision}:{engine}:{generation}"
    return "forecast-" + hashlib.sha256(key.encode()).hexdigest()


async def mark(db, job, status):
    await (
        db.prepare(
            "UPDATE forecast_job SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE event_code = ? AND instance_id = ?"
        )
        .bind(status, job.event_code, job.instance_id)
        .run()
    )


async def reconcile(db, workflows, job):
    """Only release ownership after Cloudflare confirms the instance has stopped."""
    if job.status not in ("pending", "running"):
        return
    if job.status == "pending":
        # Creation is idempotent by ID. A crash before/after create needs no lock expiry.
        await workflows.ensure(
            job.instance_id,
            {
                "code": job.event_code,
                "revision": job.input_revision,
                "engine": job.engine_version,
                "instance_id": job.instance_id,
            },
        )
    status = await workflows.status(job.instance_id)
    if status == "complete":
        row = (
            await db.prepare(
                "SELECT i.revision, s.input_revision, s.engine_version, s.status FROM forecast_input i "
                "LEFT JOIN forecast_state s ON s.event_code = i.event_code WHERE i.event_code = ? AND "
                + source_current("i")
            )
            .bind(job.event_code)
            .first()
        )
        if row is None or row.revision != job.input_revision or job.engine_version != ENGINE_VERSION:
            outcome = "superseded"
        elif row.input_revision == job.input_revision and row.engine_version == job.engine_version:
            outcome = "yielded" if row.status == "running" else row.status
        else:
            outcome = "yielded"
        await mark(db, job, outcome)
    elif status in ("errored", "terminated"):
        await mark(db, job, "errored")
    else:
        await mark(db, job, "running")


async def dispatch(db, workflows, code=None):
    """Coalesce inputs, atomically claim each event, and reconcile interrupted dispatch."""
    jobs = await db.prepare("SELECT * FROM forecast_job WHERE (? IS NULL OR event_code = ?)").bind(code, code).all()
    errors = await reconcile_jobs(db, workflows, jobs.results)
    inputs = (
        await db.prepare(
            "SELECT i.event_code, i.revision, j.input_revision, j.engine_version, j.generation, j.status "
            "FROM forecast_input i LEFT JOIN forecast_job j ON j.event_code = i.event_code "
            "WHERE (? IS NULL OR i.event_code = ?) AND "
            + source_current("i")
            + " AND NOT EXISTS (SELECT 1 FROM forecast_state s "
            "WHERE s.event_code = i.event_code AND s.input_revision = i.revision AND s.engine_version = ? "
            "AND s.status IN ('complete', 'exhausted'))"
        )
        .bind(code, code, ENGINE_VERSION)
        .all()
    )
    for row in inputs.results:
        same = row.input_revision == row.revision and row.engine_version == ENGINE_VERSION
        if row.status == "errored" or (
            row.status is not None and (row.status not in TERMINAL or (same and row.status != "yielded"))
        ):
            continue
        generation = row.generation + 1 if same else 0
        identifier = instance_id(row.event_code, row.revision, ENGINE_VERSION, generation)
        claimed = (
            await db.prepare(CLAIM).bind(ENGINE_VERSION, generation, identifier, row.event_code, row.revision).first()
        )
        if claimed is not None:
            errors.extend(await reconcile_jobs(db, workflows, [claimed]))
    if errors:
        raise RuntimeError("Workflow dispatch failed for: " + ", ".join(errors))


async def reconcile_jobs(db, workflows, jobs):
    errors = []
    for job in jobs:
        try:
            await reconcile(db, workflows, job)
        except Exception:
            logging.getLogger(__name__).exception("Workflow dispatch failed for %s", job.event_code)
            errors.append(job.event_code)
    return errors


async def advance_job(db, payload):
    """A delayed or restarted instance cannot compute a different revision or steal ownership."""
    job = await db.prepare("SELECT instance_id FROM forecast_job WHERE event_code = ?").bind(payload["code"]).first()
    if job is None or job.instance_id != payload["instance_id"]:
        return "superseded"
    report, status = await forecast_step(
        db, payload["code"], expected_revision=payload["revision"], expected_engine=payload["engine"]
    )
    # A competing step or a source change during a write requires rereading the checkpoint.
    if status == 409 and report.get("error") == "input or checkpoint changed; retry the current revision":
        raise RuntimeError(report["error"])
    if status in (404, 409):
        return "superseded"
    if status >= 400:
        raise RuntimeError(str(report))
    return report["status"]


async def retry_job(db, workflows, code, method="POST"):
    if method != "POST":
        return {"error": "method not allowed"}, 405
    job = await db.prepare("SELECT * FROM forecast_job WHERE event_code = ?").bind(code).first()
    if job is None or job.status != "errored":
        return {"error": "no failed job to retry"}, 409
    # Only an explicit request may release a failed event, even after inputs or code change.
    released = (
        await db.prepare(
            "UPDATE forecast_job SET status = 'yielded', updated_at = CURRENT_TIMESTAMP "
            "WHERE event_code = ? AND instance_id = ? AND status = 'errored' RETURNING instance_id"
        )
        .bind(code, job.instance_id)
        .first()
    )
    if released is None:
        return {"error": "job changed; read its current status"}, 409
    # A fresh instance uses the current deployment and resumes the durable D1 checkpoint.
    return await dispatch_job(db, workflows, code)


async def dispatch_job(db, workflows, code, method="POST"):
    if method != "POST":
        return {"error": "method not allowed"}, 405
    await dispatch(db, workflows, code)
    job = await db.prepare("SELECT * FROM forecast_job WHERE event_code = ?").bind(code).first()
    if job is None:
        return {"error": "no job for current inputs"}, 404
    return {"event": code, "instance_id": job.instance_id, "status": job.status}, 200


async def run_job(db, payload, steps):
    for index in range(BATCHES_PER_INSTANCE):

        @steps.do(f"batch-{index}", config={"retries": {"limit": 0, "delay": "10 seconds"}})
        async def batch():
            return await advance_job(db, payload)

        status = await batch()
        if status != "running":
            return status
    return "yielded"
