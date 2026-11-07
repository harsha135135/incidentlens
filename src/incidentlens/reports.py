"""Reports: deterministic validation and transactional finalization (spec §14), and the
deterministic inconclusive fallback built only from saved facts.

The runner never supplies report text. It names the model action whose recorded decision is the
report (decisions D30), so a compromised runner cannot forge a report. The gateway validates that
decision, maps evidence handles to the job's own evidence IDs, merges the limitations the code
requires (which the model cannot remove), adds server-side usage, and stores the report in the same
transaction that completes the job."""

from __future__ import annotations

import json
import re
import uuid

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from incidentlens.actions import ReportPayload
from incidentlens.evidence import ACTIVE_ACTION, check_capability, lock_job
from incidentlens.jobs import ApiError, append_event
from incidentlens.model_gateway import PROMPT_VERSION

SCHEMA_VERSION = "1.0"
MAX_REPORT_BYTES = 32 * 1024
HANDLE = re.compile(r"^E([1-9][0-9]{0,3})$")


def evidence_index(conn: psycopg.Connection, job_id: uuid.UUID) -> dict[str, dict]:
    rows = conn.execute("SELECT e.ordinal, e.id, a.tool, e.envelope->>'coverage', "
                        "(e.envelope->>'truncated')::boolean, e.envelope->'limitations' "
                        "FROM evidence e JOIN actions a ON a.id = e.action_id WHERE e.investigation_id = %s "
                        "ORDER BY e.ordinal", (job_id,)).fetchall()
    return {f"E{ordinal}": {"evidence_id": str(evidence_id), "tool": tool, "coverage": coverage,
                            "truncated": truncated, "limitations": limitations or []}
            for ordinal, evidence_id, tool, coverage, truncated, limitations in rows}


def required_limitations(conn: psycopg.Connection, job_id: uuid.UUID, index: dict[str, dict]) -> list[str]:
    """Limitations the code adds to every report: incomplete or truncated observations, and reads
    that failed or were interrupted. The model cannot remove them (spec §14)."""
    notes = []
    for handle, item in index.items():
        if item["coverage"] != "COMPLETE" or item["truncated"]:
            first = (item["limitations"] or ["coverage incomplete"])[0]
            state = item["coverage"] + (", truncated" if item["truncated"] else "")
            notes.append(f"{handle} ({item['tool']}) is {state}: {first}")
    failed = conn.execute("SELECT tool, status FROM actions WHERE investigation_id = %s AND kind = 'tool' "
                          "AND status IN ('ERROR', 'INTERRUPTED') ORDER BY created_at", (job_id,)).fetchall()
    for tool, status in failed:
        notes.append(f"A {tool} read ended {status}; that source is unknown for this window.")
    return notes


def usage(conn: psycopg.Connection, job_id: uuid.UUID) -> dict:
    """Usage comes from the action records, never from the model (spec §14)."""
    model, tools, reserved = conn.execute("SELECT model_actions, tool_actions, tokens_reserved FROM investigations "
                                          "WHERE id = %s", (job_id,)).fetchone()
    used_in, used_out, unknown = conn.execute(
        "SELECT coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), "
        "count(*) FILTER (WHERE status = 'UNKNOWN') FROM actions WHERE investigation_id = %s AND kind = 'model'",
        (job_id,)).fetchone()
    return {"model_actions": model, "tool_actions": tools, "tokens_counted_against_budget": reserved,
            "reported_input_tokens": used_in, "reported_output_tokens": used_out, "model_calls_with_unknown_usage": unknown}


def resolve_citations(items: list[dict], index: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Replaces handles (E1) with evidence IDs; returns the items and any unknown references.
    A full evidence ID that belongs to this job is accepted as well."""
    by_id = {item["evidence_id"]: handle for handle, item in index.items()}
    unknown, resolved = [], []
    for item in items:
        ids, handles = [], []
        for reference in item.get("evidence_ids", []):
            handle = reference if reference in index else by_id.get(reference)
            if handle is None:
                unknown.append(reference)
                continue
            handles.append(handle)
            ids.append(index[handle]["evidence_id"])
        resolved.append(item | {"evidence_ids": ids, "evidence_handles": handles})
    return resolved, unknown


NUMBER = re.compile(r"(?<![A-Za-z0-9_.:-])(\d+(?:[.,]\d+)?)(?![\d:]|xx)(\s?%)?")  # "5xx" is a status class


def numbers_in(text: str) -> list[str]:
    """Numbers stated in text, ignoring evidence handles (E3) and clock times (11:29:04)."""
    return [number for number, _ in numbers_with_units(text)]


def numbers_with_units(text: str) -> list[tuple[str, bool]]:
    """(number, stated as a percentage) for each number in text, with the same exclusions."""
    text = re.sub(r"\bE\d+\b", " ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b", " ", text)
    return [(number.replace(",", ""), bool(percent)) for number, percent in NUMBER.findall(text)]


def number_supported(value: str, evidence_text: str, percent: bool = False) -> bool:
    """True when the number appears in the cited evidence, allowing for the precision it is stated
    with. A percentage also matches a ratio in the evidence (8.1% matches 0.0808)."""
    stated = float(value)
    decimals = len(value.split(".")[1]) if "." in value else 0
    for candidate in numbers_in(evidence_text):
        number = float(candidate)
        if number == stated or round(number, decimals) == stated:
            return True
        if percent and round(number * 100, decimals) == stated:
            return True
    return False


def check_numbers(findings: list[dict], texts: dict[str, str]) -> dict:
    """Spec §14 asks validation to ensure accurate tool-calculated arithmetic. Every number in a
    finding's claim is looked up in the cited observations, as rendered for the model; numbers not
    found there are flagged on the finding, visibly, not removed (decisions D50)."""
    checked = unverified = 0
    for finding in findings:
        cited = "\n".join(texts.get(handle, "") for handle in finding.get("evidence_handles", []))
        stated = numbers_with_units(finding.get("claim", ""))
        missing = [value for value, percent in stated if not number_supported(value, cited, percent)]
        checked += len(stated)
        unverified += len(missing)
        finding["unverified_numbers"] = missing
    return {"numbers_checked": checked, "numbers_not_found_in_cited_evidence": unverified}


def build_report(conn: psycopg.Connection, job_id: uuid.UUID, payload: ReportPayload) -> tuple[dict, list[str]]:
    index = evidence_index(conn, job_id)
    findings, unknown_findings = resolve_citations([f.model_dump() for f in payload.findings], index)
    alternatives, unknown_alternatives = resolve_citations([a.model_dump() for a in payload.alternatives], index)
    limitations = list(payload.limitations)
    for note in required_limitations(conn, job_id, index):
        if note not in limitations:
            limitations.append(note)
    outcome = payload.outcome
    unavailable = [handle for handle, item in index.items() if item["coverage"] == "UNAVAILABLE"]
    if outcome == "NO_FAULT_OBSERVED" and unavailable:
        # A lost source is never turned into a healthy result (spec §17).
        outcome = "INCONCLUSIVE"
        limitations.append(f"Outcome changed from NO_FAULT_OBSERVED to INCONCLUSIVE: {', '.join(unavailable)} "
                           "unavailable, so the absence of a fault cannot be concluded.")
    from incidentlens.evidence_render import render_observation
    rendered = {f"E{ordinal}": render_observation(f"E{ordinal}", tool, arguments, envelope)
                for ordinal, tool, arguments, envelope in conn.execute(
                    "SELECT e.ordinal, a.tool, a.arguments, e.envelope FROM evidence e JOIN actions a ON a.id = e.action_id "
                    "WHERE e.investigation_id = %s", (job_id,)).fetchall()}
    number_check = check_numbers(findings, rendered)
    report = {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "summary": payload.summary,
        "findings": findings,
        "alternatives": alternatives,
        "limitations": limitations,
        "next_checks": list(payload.next_checks),
        "usage": usage(conn, job_id),
        "checks": number_check,
        "evidence": {handle: item["evidence_id"] for handle, item in index.items()},
    }
    return report, unknown_findings + unknown_alternatives


def submit_report(conn: psycopg.Connection, job_id: uuid.UUID, token: str, action_id: uuid.UUID) -> dict:
    """Finalizes the job with the report recorded as model action `action_id`'s decision. Unknown
    evidence references get one repair (if a repair and model budget remain), then the fallback."""
    with conn.transaction():
        job = check_capability(lock_job(conn, job_id), token)
        if job["status"] != "RUNNING":
            raise ApiError(409, "JOB_NOT_RUNNING", f"The job is {job['status']}.")
        row = conn.execute("SELECT kind, status, response, generation FROM actions WHERE id = %s "
                           "AND investigation_id = %s", (action_id, job_id)).fetchone()
        if row is None or row[0] != "model" or row[1] != "SUCCEEDED":
            raise ApiError(422, "NOT_A_DECISION", "The report must name a completed model action.")
        in_flight = conn.execute("SELECT count(*) FROM actions WHERE investigation_id = %s AND status = ANY(%s)",
                                 (job_id, list(ACTIVE_ACTION))).fetchone()[0]
        if in_flight:
            raise ApiError(409, "ACTION_IN_FLIGHT", "Another action is still in flight.")
        decision = row[2] or {}
        if decision.get("type") == "stop_inconclusive":
            payload = ReportPayload(outcome="INCONCLUSIVE", summary=decision.get("reason") or "Stopped without a diagnosis.",
                                    findings=[], alternatives=[], limitations=[],
                                    next_checks=list(decision.get("missing_evidence") or [])[:10])
        elif decision.get("type") == "submit_report":
            try:
                payload = ReportPayload.model_validate(decision["report"])
            except ValidationError as error:
                raise ApiError(422, "INVALID_REPORT", f"{error.error_count()} schema error(s).") from error
        else:
            raise ApiError(422, "NOT_A_REPORT", "That model action did not decide to report or stop.")

        report, unknown = build_report(conn, job_id, payload)
        if unknown:
            if job["repairs_used"] < 1 and job["model_actions"] < 8:
                conn.execute("UPDATE investigations SET repairs_used = repairs_used + 1, next_phase = 'model_decision', "
                             "next_action_id = gen_random_uuid(), last_decision = %s WHERE id = %s",
                             (Jsonb({"type": "malformed", "problem": "the report cited unknown evidence: "
                                     + ", ".join(sorted(set(unknown))[:5]), "action_id": str(action_id)}), job_id))
                append_event(conn, job_id, "report_rejected", {"reason": "unknown evidence", "repair": True})
                return {"status": "REPAIR_REQUESTED", "unknown_evidence": sorted(set(unknown))}
            return finalize_fallback_locked(conn, job, "the report cited unknown evidence and no repair remained")
        return store_report(conn, job, report, "model")


def store_report(conn: psycopg.Connection, job: dict, report: dict, source: str) -> dict:
    """Stores the report and completes the job in the caller's transaction (job row locked)."""
    encoded = json.dumps(report)
    if len(encoded.encode()) > MAX_REPORT_BYTES:
        raise ApiError(413, "REPORT_TOO_LARGE", "The report exceeds 32 KiB.")
    conn.execute("INSERT INTO reports (investigation_id, schema_version, outcome, report, source, model_id, "
                 "prompt_version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                 (job["id"], SCHEMA_VERSION, report["outcome"], Jsonb(report), source,
                  "claude-sonnet-5-5" if source == "model" else None, PROMPT_VERSION if source == "model" else None))
    append_event(conn, job["id"], "completed", {"outcome": report["outcome"], "source": source})
    conn.execute("UPDATE investigations SET status = 'COMPLETED', finished_at = now(), capability_hash = NULL, "
                 "final_reason = %s WHERE id = %s",
                 (f"{source} report: {report['outcome']}", job["id"]))
    return {"status": "COMPLETED", "outcome": report["outcome"], "source": source}


def fallback_report(conn: psycopg.Connection, job_id: uuid.UUID, reason: str) -> dict:
    """A deterministic INCONCLUSIVE report from saved facts only: what was observed (by handle and
    coverage), what failed, and why no diagnosis was made. It claims no cause."""
    index = evidence_index(conn, job_id)
    collected = ", ".join(f"{handle} {item['tool']} ({item['coverage']})" for handle, item in index.items()) or "none"
    payload = ReportPayload(outcome="INCONCLUSIVE", summary=f"No diagnosis was made: {reason}."[:1000],
                            findings=[], alternatives=[],
                            limitations=[f"Observations saved before stopping: {collected}."[:2000]],
                            next_checks=["Rerun the investigation, or inspect the saved observations directly."])
    report, _ = build_report(conn, job_id, payload)
    return report


def finalize_fallback_locked(conn: psycopg.Connection, job: dict, reason: str) -> dict:
    if job["status"] != "RUNNING":
        return {"status": job["status"], "outcome": None, "source": None}
    return store_report(conn, job, fallback_report(conn, job["id"], reason), "fallback")


def finalize_fallback(conn: psycopg.Connection, job_id: uuid.UUID, reason: str, token: str | None = None) -> dict:
    """Completes a RUNNING job with the fallback report (deadline, budget exhausted, repeated
    malformed output). With a token, the caller must hold the current capability (the runner);
    without one, the caller is the trusted supervisor or gateway."""
    with conn.transaction():
        job = lock_job(conn, job_id)
        if token is not None:
            check_capability(job, token)
        if job is None:
            raise ApiError(404, "NOT_FOUND", "No such investigation.")
        return finalize_fallback_locked(conn, job, reason)
