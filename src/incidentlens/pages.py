"""The three screens (spec §15): Start, Progress and Report, plus sign-in. Server-rendered with
Jinja2 autoescaping, so every value from a job, a log or a model is escaped. The pages load one
script file (no inline scripts), which polls the JSON API and writes values with textContent."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from incidentlens import auth, jobs
from incidentlens.evidence_render import render_observation
from incidentlens.jobs import ApiError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
OUTCOME_LABELS = {
    "SUPPORTED": "Diagnosis supported in the observed window",
    "TENTATIVE": "Tentative diagnosis in the observed window",
    "NO_FAULT_OBSERVED": "No fault observed in the window",
    "INCONCLUSIVE": "No diagnosis: the evidence was insufficient",
}


def report_view(conn, job_id: uuid.UUID, report: dict, source: str) -> dict:
    """Everything the Report screen and the HTML export show, with evidence rendered to text."""
    rows = conn.execute("SELECT e.id, e.ordinal, a.tool, a.arguments, e.envelope FROM evidence e "
                        "JOIN actions a ON a.id = e.action_id WHERE e.investigation_id = %s ORDER BY e.ordinal",
                        (job_id,)).fetchall()
    evidence = [{"id": str(evidence_id), "handle": f"E{ordinal}", "tool": tool, "coverage": envelope.get("coverage"),
                 "truncated": envelope.get("truncated"), "collected_at": envelope.get("collected_at"),
                 "observed": envelope.get("observed_window") or {}, "source": (envelope.get("source") or {}).get("kind"),
                 "text": render_observation(f"E{ordinal}", tool, arguments, envelope)}
                for evidence_id, ordinal, tool, arguments, envelope in rows]
    missing = [item for item in evidence if item["coverage"] == "UNAVAILABLE"]
    partial = [item for item in evidence if item["coverage"] == "PARTIAL"]
    if source == "fallback":
        banner = ("stopped", "Stopped without a diagnosis")
    elif report["outcome"] == "NO_FAULT_OBSERVED":
        banner = ("no-fault", OUTCOME_LABELS["NO_FAULT_OBSERVED"])
    else:
        banner = (report["outcome"].lower(), OUTCOME_LABELS[report["outcome"]])
    return {"report": report, "source": source, "evidence": evidence, "missing": missing, "partial": partial,
            "banner_class": banner[0], "banner_text": banner[1]}


def create_pages(pool: ConnectionPool) -> APIRouter:
    router = APIRouter()

    def session_or_none(request: Request):
        try:
            with pool.connection() as conn:
                return auth.session_for(conn, request.cookies.get(auth.SESSION_COOKIE))
        except ApiError:
            return None

    @router.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)  # no icon; avoids a 404 in the browser console

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"title": "Sign in"})

    @router.get("/", response_class=HTMLResponse)
    def start_page(request: Request):
        session = session_or_none(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        with pool.connection() as conn:
            recent = conn.execute("SELECT id, symptom, status, created_at FROM investigations WHERE owner = %s "
                                  "ORDER BY created_at DESC LIMIT 10", (session.identity,)).fetchall()
        return TEMPLATES.TemplateResponse(request, "start.html", {
            "title": "Start an investigation",
            "recent": [{"id": str(r[0]), "symptom": r[1], "status": r[2], "created_at": r[3].isoformat()} for r in recent]})

    @router.get("/investigations/{job_id}", response_class=HTMLResponse)
    def investigation_page(job_id: uuid.UUID, request: Request):
        session = session_or_none(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        with pool.connection() as conn:
            job = jobs.get_investigation(conn, session.identity, job_id)
            if job is None:
                return TEMPLATES.TemplateResponse(request, "not_found.html", {"title": "Not found"}, status_code=404)
            row = conn.execute("SELECT report, source FROM reports WHERE investigation_id = %s", (job_id,)).fetchone()
            view = report_view(conn, job_id, row[0], row[1]) if row else None
        context = {"title": "Investigation", "job": {key: (value.isoformat() if hasattr(value, "isoformat") else
                                                            str(value) if isinstance(value, uuid.UUID) else value)
                                                      for key, value in job.items()}}
        if view:
            return TEMPLATES.TemplateResponse(request, "report.html", context | view | {"title": "Report"})
        return TEMPLATES.TemplateResponse(request, "progress.html", context | {"title": "In progress"})

    return router
