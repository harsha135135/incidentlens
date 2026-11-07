"""Actions and evidence: the durable record of everything a job asked for and observed (spec §8, §11).

Every action follows: validate -> reserve budget and insert PENDING -> commit -> external I/O
-> persist the result, evidence and progress event -> commit. No transaction is open during I/O.
Each step locks the job row first (then the action row), so admission, results and takeovers are
serialised per job, and a result from an old generation can never become evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from incidentlens.actions import DIAGNOSTIC_TOOLS
from incidentlens.jobs import ApiError, append_event, capability_hash

MAX_MODEL_ACTIONS = 8
MAX_TOOL_ACTIONS = 12
TOKEN_BUDGET = 40_000
MAX_OUTPUT_TOKENS = 1_500
MAX_INPUT_TOKENS = 6_000
EVIDENCE_LIMIT_BYTES = 1024 * 1024
ACTIVE_ACTION = ("PENDING", "RUNNING")


@dataclass(frozen=True)
class ActionRequest:
    action_id: uuid.UUID
    kind: Literal["tool", "model"]
    tool: str | None = None
    arguments: dict | None = None


def input_hash(request: ActionRequest) -> str:
    canonical = json.dumps({"kind": request.kind, "tool": request.tool, "arguments": request.arguments or {}},
                           sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_job(conn: psycopg.Connection, job_id: uuid.UUID) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute("SELECT *, now() AS db_now FROM investigations WHERE id = %s FOR UPDATE",
                              (job_id,)).fetchone()


def check_capability(job: dict | None, token: str) -> dict:
    """The capability must match the job's current one: a revoked (cancelled, finished) or
    replaced (taken over) capability gets 403 and can do nothing."""
    if job is None or not job["capability_hash"] or not hmac.compare_digest(job["capability_hash"],
                                                                            capability_hash(token)):
        raise ApiError(403, "CAPABILITY_REJECTED", "This capability is not valid for the job's current generation.")
    if job["deadline"] is not None and job["db_now"] >= job["deadline"]:
        # Capabilities expire at the job deadline (spec §8): nothing more is admitted or finalized
        # by the runner; the supervisor writes the deadline fallback.
        raise ApiError(403, "DEADLINE_EXCEEDED", "The job deadline has passed; this capability has expired.")
    return job


def action_state(row: dict) -> dict:
    return {"action_id": str(row["id"]), "kind": row["kind"], "tool": row["tool"], "status": row["status"],
            "response": row["response"], "error": row["error"], "generation": row["generation"]}


def submit_action(conn: psycopg.Connection, job_id: uuid.UUID, token: str, request: ActionRequest) -> dict:
    """Admits exactly one action, durably, before any I/O. A repeated ID with the same input
    returns the saved state; a changed input is 409. A new action must use the server-issued
    next_action_id, while the job is RUNNING, within its lease and deadline, with nothing else in
    flight and budget left. Returns the action's state."""
    if request.kind == "tool":
        from incidentlens.model_gateway import OFFERED_TOOLS  # one list for the model and the gateway
        if request.tool not in DIAGNOSTIC_TOOLS:
            raise ApiError(422, "UNKNOWN_TOOL", "That tool does not exist.")
        if request.tool not in OFFERED_TOOLS:
            raise ApiError(422, "TOOL_NOT_OFFERED", "That tool is not available to this job.")
        try:
            arguments = DIAGNOSTIC_TOOLS[request.tool][1].model_validate(request.arguments or {}).model_dump()
        except ValueError as error:
            raise ApiError(422, "INVALID_ARGUMENTS", f"Invalid tool arguments: {error.__class__.__name__}.") from error
        request = ActionRequest(request.action_id, "tool", request.tool, arguments)
    elif request.tool is not None or request.arguments:
        raise ApiError(422, "INVALID_ACTION", "A model action takes only action_id and kind.")
    digest = input_hash(request)

    with conn.transaction():
        job = check_capability(lock_job(conn, job_id), token)
        with conn.cursor(row_factory=dict_row) as cursor:
            existing = cursor.execute("SELECT * FROM actions WHERE id = %s", (request.action_id,)).fetchone()
        if existing:
            if existing["investigation_id"] != job_id or existing["input_hash"] != digest:
                raise ApiError(409, "ACTION_ID_REUSED", "This action ID was used with a different action.")
            return action_state(existing)

        refuse_unless_admissible(conn, job, request)
        conn.execute(
            "INSERT INTO actions (id, investigation_id, generation, kind, tool, arguments, input_hash, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')",
            (request.action_id, job_id, job["generation"], request.kind, request.tool, Jsonb(request.arguments or {}),
             digest))
        counter = "tool_actions" if request.kind == "tool" else "model_actions"
        conn.execute(f"UPDATE investigations SET {counter} = {counter} + 1 WHERE id = %s", (job_id,))
        append_event(conn, job_id, "action_admitted",
                     {"action_id": str(request.action_id), "kind": request.kind, "tool": request.tool})
        with conn.cursor(row_factory=dict_row) as cursor:
            return action_state(cursor.execute("SELECT * FROM actions WHERE id = %s", (request.action_id,)).fetchone())


def refuse_unless_admissible(conn: psycopg.Connection, job: dict, request: ActionRequest) -> None:
    if job["status"] != "RUNNING":
        raise ApiError(409, "JOB_NOT_RUNNING", f"The job is {job['status']}; no new actions are admitted.")
    if job["deadline"] is not None and job["db_now"] >= job["deadline"]:
        raise ApiError(409, "DEADLINE_EXCEEDED", "The job deadline has passed.")
    if job["lease_until"] is None or job["db_now"] >= job["lease_until"]:
        raise ApiError(409, "LEASE_EXPIRED", "The job lease has expired.")
    if request.action_id != job["next_action_id"]:
        raise ApiError(409, "UNEXPECTED_ACTION_ID", "Use the next_action_id issued in the job context.")
    in_flight = conn.execute("SELECT count(*) FROM actions WHERE investigation_id = %s AND status = ANY(%s)",
                             (job["id"], list(ACTIVE_ACTION))).fetchone()[0]
    if in_flight:
        raise ApiError(409, "ACTION_IN_FLIGHT", "Another action is still in flight.")
    if request.kind == "tool" and job["tool_actions"] >= MAX_TOOL_ACTIONS:
        raise ApiError(409, "TOOL_BUDGET_EXHAUSTED", f"All {MAX_TOOL_ACTIONS} tool actions are used.")
    if request.kind == "model" and job["model_actions"] >= MAX_MODEL_ACTIONS:
        raise ApiError(409, "MODEL_BUDGET_EXHAUSTED", f"All {MAX_MODEL_ACTIONS} model actions are used.")
    if request.kind == "model" and job["tokens_reserved"] + MAX_OUTPUT_TOKENS > TOKEN_BUDGET:
        raise ApiError(409, "TOKEN_BUDGET_EXHAUSTED", "Too few tokens remain for another model call.")


def get_action(conn: psycopg.Connection, job_id: uuid.UUID, token: str, action_id: uuid.UUID) -> dict:
    """Polling never spends budget. Polling still requires the current capability."""
    with conn.transaction():
        check_capability(lock_job(conn, job_id), token)
        with conn.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute("SELECT * FROM actions WHERE id = %s AND investigation_id = %s",
                                 (action_id, job_id)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "No such action for this job.")
    return action_state(row)


# ---- Executor side ----------------------------------------------------------------------------


def start_tool(conn: psycopg.Connection, action_id: uuid.UUID) -> dict | None:
    """Marks a PENDING tool action RUNNING if its job is still RUNNING in the same generation.
    Returns the action, or None after recording why it will not run."""
    with conn.transaction():
        action = lock_action_with_job(conn, action_id)
        if action is None or action["status"] != "PENDING":
            return None
        if not still_current(action):
            finish_without_result(conn, action, "ERROR", "the job moved on before this action ran")
            return None
        conn.execute("UPDATE actions SET status = 'RUNNING', started_at = now() WHERE id = %s", (action_id,))
        return action


def reserve_model_call(conn: psycopg.Connection, action_id: uuid.UUID, estimated_input_tokens: int) -> dict | None:
    """Commits the model call's reservation (counted input + the maximum output) and marks it
    RUNNING before dispatch. Refuses, without dispatching, when the input exceeds 6,000 tokens or
    the job's 40,000-token total would be exceeded (decisions D29)."""
    with conn.transaction():
        action = lock_action_with_job(conn, action_id)
        if action is None or action["status"] != "PENDING":
            return None
        if not still_current(action):
            finish_without_result(conn, action, "ERROR", "the job moved on before this action ran")
            return None
        reservation = estimated_input_tokens + MAX_OUTPUT_TOKENS
        if estimated_input_tokens > MAX_INPUT_TOKENS:
            finish_without_result(conn, action, "ERROR",
                                  f"input of {estimated_input_tokens} tokens exceeds {MAX_INPUT_TOKENS}; not sent")
            return None
        if action["job_tokens_reserved"] + reservation > TOKEN_BUDGET:
            finish_without_result(conn, action, "ERROR", "the 40,000-token budget would be exceeded; not sent")
            return None
        conn.execute("UPDATE actions SET status = 'RUNNING', started_at = now(), reserved_tokens = %s WHERE id = %s",
                     (reservation, action_id))
        conn.execute("UPDATE investigations SET tokens_reserved = tokens_reserved + %s WHERE id = %s",
                     (reservation, action["investigation_id"]))
        return action | {"reserved_tokens": reservation}


def lock_action_with_job(conn: psycopg.Connection, action_id: uuid.UUID) -> dict | None:
    """Locks the job row, then the action row (always in that order)."""
    job_id = conn.execute("SELECT investigation_id FROM actions WHERE id = %s", (action_id,)).fetchone()
    if job_id is None:
        return None
    job = lock_job(conn, job_id[0])
    with conn.cursor(row_factory=dict_row) as cursor:
        action = cursor.execute("SELECT * FROM actions WHERE id = %s FOR UPDATE", (action_id,)).fetchone()
    return action | {"job_status": job["status"], "job_generation": job["generation"], "job_deadline": job["deadline"],
                     "job_tokens_reserved": job["tokens_reserved"], "db_now": job["db_now"],
                     "job_evidence_bytes": job["evidence_bytes"], "job_evidence_count": job["evidence_count"],
                     "job_next_phase": job["next_phase"], "job_last_decision": job["last_decision"],
                     "job_repairs_used": job["repairs_used"]}


def still_current(action: dict) -> bool:
    return (action["job_status"] == "RUNNING" and action["job_generation"] == action["generation"]
            and (action["job_deadline"] is None or action["db_now"] < action["job_deadline"]))


def finish_without_result(conn: psycopg.Connection, action: dict, status: str, reason: str) -> None:
    """Ends an action with no evidence. When the job is still current, the next action ID advances
    and the event is recorded; for a stale or finished job nothing about the job changes."""
    conn.execute("UPDATE actions SET status = %s, error = %s, finished_at = now() WHERE id = %s",
                 (status, reason, action["id"]))
    if action["job_status"] == "RUNNING" and action["job_generation"] == action["generation"]:
        append_event(conn, action["investigation_id"], "action_finished",
                     {"action_id": str(action["id"]), "kind": action["kind"], "tool": action["tool"],
                      "status": status, "reason": reason})
        conn.execute("UPDATE investigations SET next_action_id = gen_random_uuid() WHERE id = %s",
                     (action["investigation_id"],))


def commit_tool_result(conn: psycopg.Connection, action_id: uuid.UUID, envelope: dict) -> str:
    """Saves the tool's envelope as evidence, completes the action and appends the progress event,
    in one transaction. A result for an old generation, a cancelled or a finished job is discarded:
    the action ends with an error and no evidence is created. Returns the action's final status."""
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    size = len(canonical.encode())
    with conn.transaction():
        action = lock_action_with_job(conn, action_id)
        if action is None or action["status"] not in ACTIVE_ACTION:
            return action["status"] if action else "MISSING"
        if not still_current(action):
            finish_without_result(conn, action, "ERROR", "result discarded: the job moved on while the tool ran")
            return "ERROR"
        if action["job_evidence_bytes"] + size > EVIDENCE_LIMIT_BYTES:
            finish_without_result(conn, action, "ERROR", "result discarded: the job's 1 MiB evidence limit is reached")
            return "ERROR"
        ordinal = action["job_evidence_count"] + 1
        evidence_id = uuid.UUID(envelope["evidence_id"])
        conn.execute("INSERT INTO evidence (id, action_id, investigation_id, ordinal, envelope, sha256, byte_count) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                     (evidence_id, action_id, action["investigation_id"], ordinal, Jsonb(envelope),
                      hashlib.sha256(canonical.encode()).hexdigest(), size))
        handle = f"E{ordinal}"
        conn.execute("UPDATE actions SET status = 'SUCCEEDED', finished_at = now(), response = %s WHERE id = %s",
                     (Jsonb({"evidence_id": str(evidence_id), "handle": handle, "coverage": envelope["coverage"]}),
                      action_id))
        append_event(conn, action["investigation_id"], "action_finished",
                     {"action_id": str(action_id), "kind": "tool", "tool": action["tool"], "status": "SUCCEEDED",
                      "handle": handle, "coverage": envelope["coverage"], "truncated": envelope["truncated"]})
        decision = action["job_last_decision"] or {}
        if decision.get("type") == "call_tool" and decision.get("tool") == action["tool"]:
            decision = decision | {"executed": True}
        conn.execute("UPDATE investigations SET evidence_count = %s, evidence_bytes = evidence_bytes + %s, "
                     "next_action_id = gen_random_uuid(), next_phase = %s, last_decision = %s WHERE id = %s",
                     (ordinal, size, next_phase_after_tool(conn, action["investigation_id"]),
                      Jsonb(decision) if decision else None, action["investigation_id"]))
        return "SUCCEEDED"


def bundle_key(tool: str, arguments: dict) -> str:
    return json.dumps([tool, arguments or {}], sort_keys=True)


def next_phase_after_tool(conn: psycopg.Connection, job_id: uuid.UUID) -> str:
    """After a tool result the next step is a model decision, unless the initial bundle is not
    complete yet: the whole fixed bundle in the product (D48), the first two tools for the agent."""
    from incidentlens.model_gateway import initial_bundle
    collected = {bundle_key(tool, arguments) for tool, arguments in conn.execute(
        "SELECT a.tool, a.arguments FROM actions a JOIN evidence e ON e.action_id = a.id WHERE a.investigation_id = %s",
        (job_id,)).fetchall()}
    wanted = {bundle_key(tool, arguments) for tool, arguments in initial_bundle()}
    return "model_decision" if wanted <= collected else "initial_collection"


def commit_model_result(conn: psycopg.Connection, action_id: uuid.UUID, decision: dict, input_tokens: int | None,
                        output_tokens: int | None, provider_request_id: str | None, status: str = "SUCCEEDED",
                        error: str | None = None) -> str:
    """Records the model's decision (or its error) and reconciles the token reservation with the
    usage the provider reported. Unknown usage keeps the full reservation."""
    with conn.transaction():
        action = lock_action_with_job(conn, action_id)
        if action is None or action["status"] not in ACTIVE_ACTION:
            return action["status"] if action else "MISSING"
        used = None if input_tokens is None or output_tokens is None else input_tokens + output_tokens
        conn.execute("UPDATE actions SET status = %s, finished_at = now(), response = %s, error = %s, "
                     "input_tokens = %s, output_tokens = %s, provider_request_id = %s WHERE id = %s",
                     (status, Jsonb(decision), error, input_tokens, output_tokens, provider_request_id, action_id))
        if used is not None:
            # Reconcile on the job row only while it may still change; a finished job keeps its record.
            if action["job_status"] in ("RUNNING", "CANCEL_REQUESTED", "QUEUED"):
                conn.execute("UPDATE investigations SET tokens_reserved = tokens_reserved - %s + %s WHERE id = %s",
                             (action["reserved_tokens"], used, action["investigation_id"]))
        if not still_current(action):
            return status  # the decision is kept for the record, but cannot steer a job that moved on
        phase = {"call_tool": "tool_observation", "submit_report": "finalization",
                 "stop_inconclusive": "finalization"}.get(decision.get("type"), "model_decision")
        append_event(conn, action["investigation_id"], "action_finished",
                     {"action_id": str(action_id), "kind": "model", "status": status,
                      "decision": decision.get("type"), "tool": decision.get("tool")})
        if decision.get("type") == "malformed":
            # One repair per job (spec §8). The second malformed reply ends the job with the
            # deterministic fallback, in this same transaction.
            if action["job_repairs_used"] >= 1:
                from incidentlens.reports import finalize_fallback_locked  # imported here: reports imports this module
                finalize_fallback_locked(conn, {"id": action["investigation_id"], "status": "RUNNING"},
                                         "the model's output was malformed again after its one repair")
                return status
            conn.execute("UPDATE investigations SET repairs_used = repairs_used + 1 WHERE id = %s",
                         (action["investigation_id"],))
        conn.execute("UPDATE investigations SET next_action_id = gen_random_uuid(), next_phase = %s, "
                     "last_decision = %s WHERE id = %s",
                     (phase, Jsonb(decision | {"action_id": str(action_id), "executed": False}),
                      action["investigation_id"]))
        return status


def recover_unfinished_actions(conn: psycopg.Connection) -> dict[str, int]:
    """Gateway startup (spec §8): nothing is redispatched. A model call that was dispatched
    (RUNNING) becomes UNKNOWN and keeps its whole reservation, because the provider may have
    charged for it. A model call never dispatched (PENDING) ends with an error and no charge.
    Unfinished reads become INTERRUPTED; a new read may be requested with a new action ID."""
    counts = {"UNKNOWN": 0, "INTERRUPTED": 0, "ERROR": 0}
    with conn.transaction():
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM actions WHERE status IN ('PENDING', 'RUNNING') ORDER BY created_at").fetchall()]
    for action_id in ids:
        with conn.transaction():
            action = lock_action_with_job(conn, action_id)
            if action is None or action["status"] not in ACTIVE_ACTION:
                continue
            if action["kind"] == "tool":
                status, reason = "INTERRUPTED", "the gateway stopped while this read was in progress"
            elif action["status"] == "RUNNING":
                status, reason = "UNKNOWN", "the gateway stopped during this model call; it may have been charged"
            else:
                status, reason = "ERROR", "the gateway stopped before this model call was sent; not charged"
            finish_without_result(conn, action, status, reason)
            counts[status] += 1
    return counts


def initial_bundle_list() -> list[tuple[str, dict]]:
    from incidentlens.model_gateway import initial_bundle
    return initial_bundle()


def offered_tools() -> list[str]:
    from incidentlens.model_gateway import OFFERED_TOOLS
    return list(OFFERED_TOOLS)


def job_context(conn: psycopg.Connection, job_id: uuid.UUID, token: str) -> dict:
    """What the runner may know (spec §8): scope, limits and usage, the next action ID and phase,
    the last decision, saved observations by handle, and the action in flight. No secrets."""
    with conn.transaction():
        job = check_capability(lock_job(conn, job_id), token)
        evidence = conn.execute(
            "SELECT e.ordinal, a.tool, a.arguments, e.envelope->>'coverage', e.envelope->>'collected_at', "
            "(e.envelope->>'truncated')::boolean FROM evidence e JOIN actions a ON a.id = e.action_id "
            "WHERE e.investigation_id = %s ORDER BY e.ordinal", (job_id,)).fetchall()
        with conn.cursor(row_factory=dict_row) as cursor:
            in_flight = cursor.execute("SELECT * FROM actions WHERE investigation_id = %s AND status = ANY(%s)",
                                       (job_id, list(ACTIVE_ACTION))).fetchone()
            history = cursor.execute("SELECT id, kind, tool, status FROM actions WHERE investigation_id = %s "
                                     "ORDER BY created_at", (job_id,)).fetchall()
    return {
        "job_id": str(job_id),
        "status": job["status"],
        "generation": job["generation"],
        "service_id": job["service_id"],
        "symptom": job["symptom"],
        "window": {"start": job["window_start"].isoformat(), "end": job["window_end"].isoformat()},
        "deadline": job["deadline"].isoformat() if job["deadline"] else None,
        "seconds_left": max(0.0, (job["deadline"] - job["db_now"]).total_seconds()) if job["deadline"] else None,
        "next_action_id": str(job["next_action_id"]) if job["next_action_id"] else None,
        "next_phase": job["next_phase"],
        "last_decision": job["last_decision"],
        "limits": {"model_actions": MAX_MODEL_ACTIONS, "tool_actions": MAX_TOOL_ACTIONS, "tokens": TOKEN_BUDGET},
        "used": {"model_actions": job["model_actions"], "tool_actions": job["tool_actions"],
                 "tokens_reserved": job["tokens_reserved"]},
        "capabilities": offered_tools(),
        "initial_bundle": [{"tool": tool, "arguments": arguments} for tool, arguments in initial_bundle_list()],
        "evidence": [{"handle": f"E{ordinal}", "tool": tool, "arguments": arguments, "coverage": coverage,
                      "collected_at": collected_at, "truncated": truncated}
                     for ordinal, tool, arguments, coverage, collected_at, truncated in evidence],
        "in_flight": action_state(in_flight) if in_flight else None,
        "actions": [{"action_id": str(row["id"]), "kind": row["kind"], "tool": row["tool"], "status": row["status"]}
                    for row in history],
    }
