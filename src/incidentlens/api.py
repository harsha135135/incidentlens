"""The public API (spec §7), on the loopback listener 127.0.0.1:8088. Everything except liveness
needs the developer's session; mutations also need the CSRF token and the canonical Origin.
JSON is snake_case; unknown fields are rejected; errors use one stable contract."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from incidentlens import auth, jobs
from incidentlens.actions import DIAGNOSTIC_TOOLS
from incidentlens.config import SERVICE_ID, Settings
from incidentlens.database import schema_current
from incidentlens.internal_api import error_response
from incidentlens.jobs import ApiError, CreateRequest
from incidentlens.pages import TEMPLATES, create_pages, report_view

MAX_BODY_BYTES = 16 * 1024
STATIC_DIR = __import__("pathlib").Path(__file__).parent / "static"
SECURITY_HEADERS = {
    # One script file, no inline scripts, nothing from other origins (spec §15).
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
PAGE_DEFAULT, PAGE_MAX = 20, 100
STATUSES = ("QUEUED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "CANCELLED", "FAILED")


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), str(row_id)]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created_at, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(created_at), uuid.UUID(row_id)
    except (ValueError, TypeError) as error:
        raise ApiError(400, "BAD_CURSOR", "The cursor is not valid.") from error


def page_limit(value: str | None) -> int:
    try:
        limit = int(value) if value else PAGE_DEFAULT
    except ValueError as error:
        raise ApiError(422, "INVALID_LIMIT", "limit must be a number.") from error
    if not 1 <= limit <= PAGE_MAX:
        raise ApiError(422, "INVALID_LIMIT", f"limit must be 1 to {PAGE_MAX}.")
    return limit


def jsonable(row: dict) -> dict:
    return {key: (value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, uuid.UUID)
                  else value) for key, value in row.items()}


def create_public_app(pool: ConnectionPool, settings: Settings, secret_hash: str) -> FastAPI:
    app = FastAPI(title="IncidentLens", docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")

    @app.exception_handler(ApiError)
    def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(error.status, error.code, error.message, error.retryable, error.retry_after)

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(PoolTimeout)
    def database_down(request: Request, error: Exception) -> JSONResponse:
        # Fail closed: without the database nothing is recorded, so nothing is started (spec §12).
        return error_response(503, "DATABASE_UNAVAILABLE", "IncidentLens's database is unavailable.", True, 5)

    @app.exception_handler(RequestValidationError)
    def request_invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(422, "INVALID_REQUEST", f"{len(error.errors())} validation error(s).")

    @app.exception_handler(ValidationError)
    def body_invalid(request: Request, error: ValidationError) -> JSONResponse:
        if any(item["type"] == "json_invalid" for item in error.errors()):
            return error_response(400, "MALFORMED_JSON", "The request body is not valid JSON.")
        return error_response(422, "INVALID_REQUEST", f"{error.error_count()} validation error(s).")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Host check on every request; body size cap; session, CSRF and Origin for /api/."""
        try:
            auth.check_host(request.headers.get("host"))
            if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
                raise ApiError(413, "REQUEST_TOO_LARGE", "The request body exceeds 16 KiB.")
            path, method = request.url.path, request.method
            if path.startswith("/api/") and not (path == "/api/v1/session" and method == "POST"):
                try:
                    session = await run_in_threadpool(lookup_session, request.cookies.get(auth.SESSION_COOKIE))
                except (psycopg.OperationalError, PoolTimeout):
                    raise ApiError(503, "DATABASE_UNAVAILABLE", "IncidentLens's database is unavailable.", True, 5)
                request.state.session = session
                if method not in ("GET", "HEAD"):
                    auth.check_origin(request.headers.get("origin"))
                    auth.check_csrf(session, request.headers.get("x-csrf-token"))
        except ApiError as error:
            response = error_response(error.status, error.code, error.message, error.retryable, error.retry_after)
            response.headers.update(SECURITY_HEADERS)
            return response
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def lookup_session(token: str | None) -> auth.Session:
        with pool.connection() as conn:
            return auth.session_for(conn, token)

    # ---- Session ----

    @app.post("/api/v1/session", status_code=204)
    async def create_session(request: Request):
        auth.check_origin(request.headers.get("origin"))
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise ApiError(415, "JSON_REQUIRED", "Send the secret as JSON.")
        body = LoginBody.model_validate_json(await request.body())
        token = await run_in_threadpool(login_sync, body.secret)
        if not token:
            raise ApiError(401, "AUTHENTICATION_FAILED", "That access secret is not correct.")
        response = Response(status_code=204)
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True,
                            samesite="strict", path="/")
        return response

    def login_sync(secret: str) -> str:
        with pool.connection() as conn:
            return auth.login(conn, secret, secret_hash)

    @app.get("/api/v1/session")
    def current_session(request: Request):
        session = request.state.session
        return {"identity": session.identity, "expires_at": session.expires_at.isoformat(),
                "csrf_token": session.csrf_token}

    @app.delete("/api/v1/session", status_code=204)
    def delete_session(request: Request):
        with pool.connection() as conn:
            auth.logout(conn, request.cookies.get(auth.SESSION_COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    # ---- Services and health ----

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        try:
            with pool.connection(timeout=2) as conn:
                current = schema_current(conn)
        except (psycopg.Error, PoolTimeout):
            return JSONResponse(status_code=503, content={"status": "not ready"})
        if not current:
            return JSONResponse(status_code=503, content={"status": "not ready"})
        return {"status": "ready"}

    @app.get("/api/v1/services")
    def services():
        return {"services": [{"service_id": SERVICE_ID, "description": "LedgerCore (local instance)",
                              "capabilities": capability_states(pool, settings)}]}

    # ---- Investigations ----

    @app.post("/api/v1/investigations", status_code=202)
    async def create(request: Request):
        body = CreateRequest.model_validate_json(await request.body())
        key = request.headers.get("idempotency-key") or ""
        job_id, replayed, status = await run_in_threadpool(create_sync, request.state.session.identity, body, key)
        status_url = f"/api/v1/investigations/{job_id}"
        return JSONResponse(status_code=202, headers={"Location": status_url},
                            content={"id": str(job_id), "status": status, "replayed": replayed, "status_url": status_url})

    def create_sync(owner: str, body: CreateRequest, key: str):
        if not key:
            raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header.")
        with pool.connection() as conn:
            job_id, replayed = jobs.create_investigation(conn, owner, body, key)
            status = conn.execute("SELECT status FROM investigations WHERE id = %s", (job_id,)).fetchone()[0]
            return job_id, replayed, status

    @app.get("/api/v1/investigations")
    def history(request: Request, status: str | None = None, cursor: str | None = None, limit: str | None = None):
        owner, size = request.state.session.identity, page_limit(limit)
        if status is not None and status not in STATUSES:
            raise ApiError(422, "INVALID_STATUS", "Unknown status filter.")
        conditions, params = ["owner = %s"], [owner]
        if status:
            conditions.append("status = %s")
            params.append(status)
        if cursor:
            created_at, row_id = decode_cursor(cursor)
            conditions.append("(created_at, id) < (%s, %s)")
            params += [created_at, row_id]
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(f"SELECT id, service_id, symptom, status, created_at, finished_at FROM investigations "
                               f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC, id DESC LIMIT %s",
                               params + [size + 1]).fetchall()
        more = len(rows) > size
        rows = rows[:size]
        return {"items": [jsonable(row) for row in rows],
                "next_cursor": encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more else None}

    @app.get("/api/v1/investigations/{job_id}")
    def detail(job_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            row = jobs.get_investigation(conn, request.state.session.identity, job_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No such investigation.")
        return jsonable(row)

    @app.post("/api/v1/investigations/{job_id}/cancel")
    def cancel(job_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            status, terminal = jobs.request_cancel(conn, request.state.session.identity, job_id)
        return JSONResponse(status_code=200 if terminal else 202, content={"id": str(job_id), "status": status})

    def owned(conn, owner: str, job_id: uuid.UUID) -> None:
        if conn.execute("SELECT 1 FROM investigations WHERE id = %s AND owner = %s", (job_id, owner)).fetchone() is None:
            raise ApiError(404, "NOT_FOUND", "No such investigation.")

    @app.get("/api/v1/investigations/{job_id}/events")
    def events(job_id: uuid.UUID, request: Request, after_seq: int = 0):
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            owned(conn, request.state.session.identity, job_id)
            rows = cur.execute("SELECT sequence, type, payload, created_at FROM progress_events "
                               "WHERE investigation_id = %s AND sequence > %s ORDER BY sequence LIMIT 100",
                               (job_id, after_seq)).fetchall()
        return {"events": [jsonable(row) for row in rows]}

    @app.get("/api/v1/investigations/{job_id}/evidence")
    def evidence_list(job_id: uuid.UUID, request: Request, cursor: str | None = None, limit: str | None = None):
        size = page_limit(limit)
        after = int(cursor) if cursor and cursor.isdigit() else 0
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            owned(conn, request.state.session.identity, job_id)
            rows = cur.execute(
                "SELECT e.id AS evidence_id, 'E' || e.ordinal AS handle, e.ordinal, a.tool, a.arguments, "
                "e.envelope->>'coverage' AS coverage, (e.envelope->>'truncated')::boolean AS truncated, "
                "e.envelope->>'collected_at' AS collected_at, e.byte_count, e.sha256 FROM evidence e "
                "JOIN actions a ON a.id = e.action_id WHERE e.investigation_id = %s AND e.ordinal > %s "
                "ORDER BY e.ordinal LIMIT %s", (job_id, after, size + 1)).fetchall()
        more = len(rows) > size
        rows = rows[:size]
        return {"items": [jsonable(row) for row in rows], "next_cursor": str(rows[-1]["ordinal"]) if more else None}

    @app.get("/api/v1/investigations/{job_id}/evidence/{evidence_id}")
    def evidence_item(job_id: uuid.UUID, evidence_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            owned(conn, request.state.session.identity, job_id)
            row = conn.execute("SELECT envelope, ordinal FROM evidence WHERE id = %s AND investigation_id = %s",
                               (evidence_id, job_id)).fetchone()
        if row is None:  # another job's evidence looks exactly like a missing one
            raise ApiError(404, "NOT_FOUND", "No such evidence for this investigation.")
        return {"handle": f"E{row[1]}", "observation": row[0]}

    @app.get("/api/v1/investigations/{job_id}/export")
    def export(job_id: uuid.UUID, request: Request, format: str = "json"):
        """A bounded download of the report with its cited evidence. The HTML form is escaped,
        has no scripts and loads nothing from the network (spec §15)."""
        if format not in ("json", "html"):
            raise ApiError(422, "INVALID_FORMAT", "format must be json or html.")
        owner = request.state.session.identity
        with pool.connection() as conn:
            job = jobs.get_investigation(conn, owner, job_id)
            if job is None:
                raise ApiError(404, "NOT_FOUND", "No such investigation.")
            row = conn.execute("SELECT report, source FROM reports WHERE investigation_id = %s", (job_id,)).fetchone()
            if row is None:
                raise ApiError(409, "REPORT_NOT_READY", "The report is not available yet.", retryable=True)
            view = report_view(conn, job_id, row[0], row[1])
        name = f"incidentlens-{str(job_id)[:8]}"
        if format == "json":
            body = {"investigation": jsonable(job), "report": row[0], "report_source": row[1],
                    "evidence": [{key: item[key] for key in ("handle", "id", "tool", "coverage", "truncated", "text")}
                                 for item in view["evidence"]]}
            return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})
        html = TEMPLATES.get_template("export.html").render(
            job=jsonable(job), exported_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            .isoformat()[:19], **view)
        return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="{name}.html"',
                                           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @app.get("/api/v1/investigations/{job_id}/report")
    def report(job_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            owned(conn, request.state.session.identity, job_id)
            row = conn.execute("SELECT report, source, created_at FROM reports WHERE investigation_id = %s",
                               (job_id,)).fetchone()
        if row is None:
            raise ApiError(409, "REPORT_NOT_READY", "The report is not available yet.", retryable=True)
        return {"report": row[0], "source": row[1], "created_at": row[2].isoformat()}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(create_pages(pool))
    return app


def capability_states(pool: ConnectionPool, settings: Settings) -> dict:
    """available, degraded or unavailable per capability, separately from API readiness (spec §7)."""
    with pool.connection() as conn:
        supervisor_age, collector_age, blocker_age, config_known = conn.execute(
            "SELECT extract(epoch FROM now() - supervisor_heartbeat_at), "
            "(SELECT extract(epoch FROM now() - max(observed_at)) FROM telemetry_samples "
            " WHERE kind = 'collector_heartbeat'), "
            "(SELECT extract(epoch FROM now() - max(observed_at)) FROM telemetry_samples "
            " WHERE kind = 'db_blockers' AND coverage = 'COMPLETE'), "
            "EXISTS (SELECT 1 FROM log_records WHERE record_type = 'ledgercore.config') FROM control").fetchone()
    try:
        prometheus_up = httpx.get(f"{settings.prometheus_url}/-/ready", timeout=1).status_code == 200
    except httpx.HTTPError:
        prometheus_up = False

    def state(ok: bool) -> str:
        return "available" if ok else "unavailable"

    logs_ok = collector_age is not None and collector_age < 15
    states = {
        "execution": "available" if supervisor_age is not None and supervisor_age <= jobs.SUPERVISOR_STALE_SECONDS
        else "degraded",
        "get_request_metrics": state(prometheus_up), "get_outbox_stats": state(prometheus_up),
        "get_pool_metrics": state(prometheus_up),
        "get_database_blockers": state(blocker_age is not None and blocker_age < 15),
        "get_runtime_config": state(config_known),
        "search_delivery_logs": state(logs_ok), "get_delivery_timings": state(logs_ok),
    }
    assert set(states) - {"execution"} == set(DIAGNOSTIC_TOOLS)
    return states
