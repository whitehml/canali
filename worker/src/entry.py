"""D1 adapter for local computation and authenticated hosted benchmarks."""

import json
import re
from urllib.parse import urlparse

from access import allowed
from assemble import handle as handle_assembly
from compute import compute, validate
from dispatch import dispatch, dispatch_job, retry_job, run_job
from engine_version import ENGINE_VERSION
from first_event import handle as handle_import
from forecast_store import export as export_forecast
from forecast_store import handle as handle_forecast
from forecast_store import upload_result
from season import handle as handle_season
from season import source_current
from workers import Response, WorkerEntrypoint, WorkflowEntrypoint

MAX_BODY = 512_000


class WorkflowBinding:
    def __init__(self, binding):
        self.binding = binding

    async def ensure(self, identifier, payload):
        try:
            await self.binding.create({"id": identifier, "params": payload})
        except Exception:
            # Duplicate IDs and lost create responses are safe only if the instance exists.
            await self.binding.get(identifier)

    async def status(self, identifier):
        instance = await self.binding.get(identifier)
        return (await instance.status()).status


class ForecastWorkflow(WorkflowEntrypoint):
    async def run(self, event, step):
        return await run_job(self.env.DB, event["payload"], step)


class Default(WorkerEntrypoint):
    async def scheduled(self, _controller, _env, _ctx):
        await dispatch(self.env.DB, WorkflowBinding(self.env.FORECAST_WORKFLOW))

    async def fetch(self, request):
        if not allowed(self.env, request):
            return Response.json({"error": "access denied"}, status=403)
        path = urlparse(request.url).path
        if path == "/health" and request.method == "GET":
            import numpy
            import scipy

            return Response.json(
                {
                    "status": "ok",
                    "numpy": numpy.__version__,
                    "scipy": scipy.__version__,
                    "engine_version": ENGINE_VERSION,
                }
            )
        season_route = re.fullmatch(
            r"/seasons/([0-9]{4})(?:/(step)|/(events|priors|forecasts)/([A-Za-z0-9_-]{1,50}))?", path
        )
        if season_route is not None:
            year, step, action, code = season_route.groups()
            try:
                if int(year) < 2015:
                    raise ValueError("season must be at least 2015")
                if action == "forecasts":
                    report, status = await handle_assembly(self.env.DB, request, int(year), code)
                else:
                    report, status = await handle_season(self.env.DB, request, int(year), step or action, code)
                return Response.json(report, status=status)
            except (ValueError, KeyError, TypeError) as exc:
                return Response.json({"error": str(exc)}, status=400)
        import_route = re.fullmatch(r"/imports/([A-Za-z0-9_-]{1,64})", path)
        if import_route is not None:
            try:
                report, status = await handle_import(self.env.DB, request, import_route[1])
                return Response.json(report, status=status)
            except (ValueError, KeyError, TypeError) as exc:
                return Response.json({"error": str(exc)}, status=400)
        forecast_route = re.fullmatch(
            r"/forecasts/([A-Za-z0-9_-]{1,64})(/step|/export|/result|/retry|/dispatch)?", path
        )
        if forecast_route is not None:
            code, step = forecast_route.groups()
            try:
                report, status = (
                    await dispatch_job(self.env.DB, WorkflowBinding(self.env.FORECAST_WORKFLOW), code, request.method)
                    if step == "/dispatch"
                    else await retry_job(self.env.DB, WorkflowBinding(self.env.FORECAST_WORKFLOW), code, request.method)
                    if step == "/retry"
                    else await upload_result(self.env.DB, request, code)
                    if step == "/result"
                    else await export_forecast(self.env.DB, request, code)
                    if step == "/export"
                    else await handle_forecast(self.env.DB, request, code, bool(step))
                )
                return Response.json(report, status=status)
            except (ValueError, KeyError, TypeError, NotImplementedError) as exc:
                return Response.json({"error": str(exc)}, status=400)
        route = re.fullmatch(r"/events/([A-Za-z0-9_-]{1,64})(/run)?", path)
        if route is None:
            return Response.json({"error": "route not found"}, status=404)
        code, run = route.groups()
        db = self.env.DB
        try:
            if request.method == "PUT" and not run:
                body = await request.text()
                if len(body.encode()) > MAX_BODY:
                    return Response.json({"error": "input exceeds 512000 bytes"}, status=413)
                document = json.loads(body)
                validate(document)
                row = (
                    await db.prepare(
                        "INSERT INTO event_input(event_code, revision, document) VALUES (?, 1, ?) "
                        "ON CONFLICT(event_code) DO UPDATE SET revision = revision + 1, document = excluded.document, "
                        "source_document = NULL "
                        "RETURNING revision"
                    )
                    .bind(code, json.dumps(document, allow_nan=False))
                    .first()
                )
                return Response.json({"event": code, "revision": row.revision})
            if request.method == "POST" and run:
                row = (
                    await db.prepare(
                        "SELECT revision, document FROM event_input i WHERE event_code = ? AND " + source_current("i")
                    )
                    .bind(code)
                    .first()
                )
                if row is None:
                    return Response.json({"error": "event not found"}, status=404)
                revision = row.revision
                result = json.dumps(compute(json.loads(row.document)), allow_nan=False)
                saved = (
                    await db.prepare(
                        "INSERT INTO event_result(event_code, input_revision, document) "
                        "SELECT event_code, revision, ? FROM event_input i WHERE event_code = ? AND revision = ? AND "
                        + source_current("i")
                        + " "
                        "ON CONFLICT(event_code) DO UPDATE SET input_revision = excluded.input_revision, "
                        "document = excluded.document, computed_at = CURRENT_TIMESTAMP RETURNING input_revision"
                    )
                    .bind(result, code, revision)
                    .first()
                )
                if saved is None:
                    return Response.json({"error": "input changed; rerun computation"}, status=409)
                return Response.json({"event": code, "revision": revision, "result": json.loads(result)})
            if request.method == "GET" and not run:
                row = (
                    await db.prepare(
                        "SELECT r.document, r.input_revision FROM event_result r JOIN event_input i "
                        "ON i.event_code = r.event_code AND i.revision = r.input_revision WHERE r.event_code = ? AND "
                        + source_current("i")
                    )
                    .bind(code)
                    .first()
                )
                if row is None:
                    return Response.json({"error": "no result for current inputs"}, status=404)
                return Response.json(
                    {"event": code, "revision": row.input_revision, "result": json.loads(row.document)}
                )
            return Response.json({"error": "method not allowed"}, status=405)
        except (ValueError, KeyError, TypeError) as exc:
            return Response.json({"error": str(exc)}, status=400)
