"""The internal gateway API (spec §8), on the private listener (127.0.0.1:8089). Every request carries
the job's capability as a bearer token; there are no browser sessions here. Handlers are plain
functions over the tested core in evidence.py and reports.py."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from incidentlens import evidence, reports
from incidentlens.evidence import ActionRequest
from incidentlens.jobs import ApiError


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: uuid.UUID
    kind: Literal["tool", "model"]
    tool: str | None = None
    arguments: dict | None = None


class ReportBody(BaseModel):
    """Either the model action whose decision is the report, or a request for the fallback."""
    model_config = ConfigDict(extra="forbid")
    action_id: uuid.UUID | None = None
    give_up: str | None = None


def error_response(status: int, code: str, message: str, retryable: bool = False,
                   retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(status_code=status, headers=headers, content={"error": {
        "code": code, "message": message, "retryable": retryable, "request_id": str(uuid.uuid4())}})


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 200:
        raise ApiError(403, "CAPABILITY_REJECTED", "A job capability is required.")
    return header[7:]


def create_internal_app(pool: ConnectionPool, wake_executor) -> FastAPI:
    app = FastAPI(title="IncidentLens internal gateway", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(error.status, error.code, error.message, error.retryable, error.retry_after)

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(PoolTimeout)
    def database_down(request: Request, error: Exception) -> JSONResponse:
        # Fail closed: without the database nothing is recorded, so nothing is started (spec §12).
        return error_response(503, "DATABASE_UNAVAILABLE", "IncidentLens's database is unavailable.", True, 5)

    @app.exception_handler(ValidationError)
    def invalid(request: Request, error: ValidationError) -> JSONResponse:
        return error_response(422, "INVALID_REQUEST", f"{error.error_count()} validation error(s).")

    @app.get("/internal/v1/jobs/{job_id}/context")
    def context(job_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            return evidence.job_context(conn, job_id, bearer(request))

    @app.post("/internal/v1/jobs/{job_id}/actions", status_code=202)
    async def submit(job_id: uuid.UUID, request: Request):
        body = ActionBody.model_validate_json(await request.body())
        token = bearer(request)
        state = await run_in_threadpool(submit_sync, job_id, token, body)
        wake_executor()
        replayed = state["status"] != "PENDING"
        return JSONResponse(status_code=200 if replayed else 202, content=state | {
            "poll_url": f"/internal/v1/jobs/{job_id}/actions/{body.action_id}"})

    def submit_sync(job_id: uuid.UUID, token: str, body: ActionBody) -> dict:
        with pool.connection() as conn:
            return evidence.submit_action(conn, job_id, token,
                                          ActionRequest(body.action_id, body.kind, body.tool, body.arguments))

    @app.get("/internal/v1/jobs/{job_id}/actions/{action_id}")
    def poll(job_id: uuid.UUID, action_id: uuid.UUID, request: Request):
        with pool.connection() as conn:
            return evidence.get_action(conn, job_id, bearer(request), action_id)

    @app.post("/internal/v1/jobs/{job_id}/report")
    async def report(job_id: uuid.UUID, request: Request):
        body = ReportBody.model_validate_json(await request.body())
        token = bearer(request)
        if (body.action_id is None) == (body.give_up is None):
            raise ApiError(422, "INVALID_REQUEST", "Send exactly one of action_id or give_up.")
        return await run_in_threadpool(report_sync, job_id, token, body)

    def report_sync(job_id: uuid.UUID, token: str, body: ReportBody) -> dict:
        with pool.connection() as conn:
            if body.action_id is not None:
                return reports.submit_report(conn, job_id, token, body.action_id)
            return reports.finalize_fallback(conn, job_id, f"the runner stopped: {body.give_up[:200]}", token)

    return app
