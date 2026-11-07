"""Investigation lifecycle: idempotent creation with admission control, reading, cancellation,
and the claims and transitions the supervisor makes (spec §7, §11, §12).

Every lease, deadline and expiry comparison uses the database clock (now()), so the API,
gateway and supervisor processes always agree on time."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from incidentlens.config import SERVICE_ID

ACTIVE_LIMIT = 1
QUEUED_LIMIT = 10
QUEUE_WAIT = "10 minutes"
JOB_DEADLINE = "180 seconds"
LEASE = "30 seconds"
MAX_RECOVERIES = 2
SUPERVISOR_STALE_SECONDS = 30
CREATE_ENDPOINT = "POST /api/v1/investigations"
TERMINAL = ("COMPLETED", "CANCELLED", "FAILED")


class ApiError(Exception):
    """An expected failure with the spec's stable error contract (§7)."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False,
                 retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.retry_after = retryable, retry_after


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_id: str = Field(min_length=1, max_length=100)
    symptom: str = Field(min_length=1, max_length=2000)
    window: TimeWindow
    profile: Literal["standard"] = "standard"


def request_hash(request: CreateRequest) -> str:
    """Canonical body hash: same meaning, same hash (times normalised to UTC)."""
    body = {"service_id": request.service_id, "symptom": request.symptom, "profile": request.profile,
            "window": {"start": request.window.start.timestamp(), "end": request.window.end.timestamp()}}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def append_event(conn: psycopg.Connection, job_id: uuid.UUID, event_type: str, payload: dict) -> int:
    """Appends a progress event. The caller holds the job row lock, so sequences never collide.
    Append before a terminal transition in the same transaction: a terminal row cannot change."""
    sequence = conn.execute("UPDATE investigations SET event_sequence = event_sequence + 1 WHERE id = %s "
                            "RETURNING event_sequence", (job_id,)).fetchone()[0]
    conn.execute("INSERT INTO progress_events (investigation_id, sequence, type, payload) VALUES (%s, %s, %s, %s)",
                 (job_id, sequence, event_type, Jsonb(payload)))
    return sequence


def create_investigation(conn: psycopg.Connection, owner: str, request: CreateRequest,
                         idempotency_key: str) -> tuple[uuid.UUID, bool]:
    """Creates a QUEUED investigation, or replays an earlier request with the same key.

    One transaction under the singleton control-row lock: the key lookup, the admission check and
    both inserts, so concurrent requests cannot exceed the queue cap or create duplicates. A replay
    is answered before any admission check, so it succeeds even when the queue is now full."""
    if not 16 <= len(idempotency_key) <= 128 or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 16-128 printable ASCII characters.")
    body_hash = request_hash(request)
    with conn.transaction():
        heartbeat_age = conn.execute(
            "SELECT extract(epoch FROM now() - supervisor_heartbeat_at) FROM control FOR UPDATE").fetchone()[0]
        existing = conn.execute(
            "SELECT request_hash, investigation_id FROM submission_keys "
            "WHERE owner = %s AND endpoint = %s AND idempotency_key = %s",
            (owner, CREATE_ENDPOINT, idempotency_key)).fetchone()
        if existing:
            if existing[0] != body_hash:
                raise ApiError(409, "IDEMPOTENCY_MISMATCH", "This Idempotency-Key was used with a different request.")
            return existing[1], True

        validate_scope(conn, request)
        if heartbeat_age is None or heartbeat_age > SUPERVISOR_STALE_SECONDS:
            raise ApiError(503, "SUPERVISOR_UNAVAILABLE", "The investigation supervisor is not running.",
                           retryable=True, retry_after=30)
        queued = conn.execute("SELECT count(*) FROM investigations WHERE status = 'QUEUED'").fetchone()[0]
        if queued >= QUEUED_LIMIT:
            raise ApiError(429, "QUEUE_FULL", f"{QUEUED_LIMIT} investigations are already waiting.",
                           retryable=True, retry_after=30)
        job_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO investigations (id, owner, service_id, symptom, window_start, window_end, profile, status, "
            f"queue_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'QUEUED', now() + interval '{QUEUE_WAIT}')",
            (job_id, owner, request.service_id, request.symptom, request.window.start, request.window.end,
             request.profile))
        conn.execute("INSERT INTO submission_keys (owner, endpoint, idempotency_key, request_hash, investigation_id) "
                     "VALUES (%s, %s, %s, %s, %s)", (owner, CREATE_ENDPOINT, idempotency_key, body_hash, job_id))
        append_event(conn, job_id, "queued", {})
    return job_id, False


def validate_scope(conn: psycopg.Connection, request: CreateRequest) -> None:
    """Spec §6: a registered service, and a window of 1-30 minutes that ends no later than now and
    starts within the past 24 hours, all against the database clock."""
    if request.service_id != SERVICE_ID:
        raise ApiError(422, "UNKNOWN_SERVICE", f"Service {request.service_id!r} is not registered.")
    start, end = request.window.start, request.window.end
    if start.tzinfo is None or end.tzinfo is None:
        raise ApiError(422, "INVALID_WINDOW", "Window times must include a UTC offset.")
    seconds, ends_in_future, starts_too_early = conn.execute(
        "SELECT extract(epoch FROM %s::timestamptz - %s::timestamptz), %s::timestamptz > now(), "
        "%s::timestamptz < now() - interval '24 hours'", (end, start, end, start)).fetchone()
    if not 60 <= seconds <= 1800:
        raise ApiError(422, "INVALID_WINDOW", "The window must be between 1 and 30 minutes long.")
    if ends_in_future:
        raise ApiError(422, "INVALID_WINDOW", "The window cannot end in the future.")
    if starts_too_early:
        raise ApiError(422, "WINDOW_OUT_OF_RETENTION", "The window must start within the past 24 hours.")


def get_investigation(conn: psycopg.Connection, owner: str, job_id: uuid.UUID) -> dict | None:
    """The job as its owner may see it, or None (absent and someone else's look the same)."""
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            "SELECT id, service_id, symptom, window_start, window_end, profile, status, created_at, started_at, "
            "deadline, finished_at, final_reason, recoveries, model_actions, tool_actions, tokens_reserved, "
            "evidence_count, evidence_bytes, event_sequence, "
            "EXISTS (SELECT 1 FROM reports r WHERE r.investigation_id = i.id) AS report_available "
            "FROM investigations i WHERE id = %s AND owner = %s", (job_id, owner)).fetchone()


def request_cancel(conn: psycopg.Connection, owner: str, job_id: uuid.UUID) -> tuple[str, bool]:
    """Returns (status, already_terminal). A queued job is cancelled at once; a running one moves to
    CANCEL_REQUESTED, its capability is revoked so no new action is admitted, and the supervisor
    stops the runner and finishes the cancellation. A terminal job is left exactly as it is."""
    with conn.transaction():
        row = conn.execute("SELECT status FROM investigations WHERE id = %s AND owner = %s FOR UPDATE",
                           (job_id, owner)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No such investigation.")
        status = row[0]
        if status in TERMINAL or status == "CANCEL_REQUESTED":
            return status, status in TERMINAL
        if status == "QUEUED":
            # The event comes first: once the job is terminal, its row can no longer change.
            append_event(conn, job_id, "cancelled", {"while": "queued"})
            conn.execute("UPDATE investigations SET status = 'CANCELLED', cancel_requested_at = now(), "
                         "finished_at = now(), final_reason = 'cancelled before it started' WHERE id = %s", (job_id,))
            return "CANCELLED", False
        conn.execute("UPDATE investigations SET status = 'CANCEL_REQUESTED', cancel_requested_at = now(), "
                     "capability_hash = NULL WHERE id = %s", (job_id,))
        append_event(conn, job_id, "cancel_requested", {})
        return "CANCEL_REQUESTED", False


# ---- Supervisor transitions -------------------------------------------------------------------


@dataclass
class Claim:
    job_id: uuid.UUID
    generation: int
    capability: str  # given to the runner once; only its hash is stored
    sandbox_name: str


def capability_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def expire_queued(conn: psycopg.Connection) -> list[uuid.UUID]:
    """Queued jobs that waited longer than 10 minutes fail with a stated reason (spec §12)."""
    expired = []
    with conn.transaction():
        rows = conn.execute("SELECT id FROM investigations WHERE status = 'QUEUED' AND queue_expires_at <= now() "
                            "FOR UPDATE SKIP LOCKED").fetchall()
        for (job_id,) in rows:
            append_event(conn, job_id, "failed", {"reason": "queue_expired"})
            conn.execute("UPDATE investigations SET status = 'FAILED', finished_at = now(), "
                         "final_reason = 'queue wait expired after 10 minutes' WHERE id = %s", (job_id,))
            expired.append(job_id)
    return expired


def claim_next(conn: psycopg.Connection) -> Claim | None:
    """Claims the oldest queued job when no job is active: a new generation, a 30 s lease, a new
    capability and a sandbox name. The deadline is set at the first claim only."""
    with conn.transaction():
        conn.execute("SELECT 1 FROM control FOR UPDATE")  # serialises with creation and other claims
        active = conn.execute("SELECT count(*) FROM investigations "
                              "WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')").fetchone()[0]
        if active >= ACTIVE_LIMIT:
            return None
        row = conn.execute("SELECT id FROM investigations WHERE status = 'QUEUED' AND queue_expires_at > now() "
                           "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
        if row is None:
            return None
        token = secrets.token_urlsafe(32)
        job_id = row[0]
        generation, = conn.execute(
            "UPDATE investigations SET status = 'RUNNING', generation = generation + 1, "
            f"lease_until = now() + interval '{LEASE}', capability_hash = %s, "
            f"started_at = coalesce(started_at, now()), deadline = coalesce(deadline, now() + interval '{JOB_DEADLINE}'), "
            "next_action_id = coalesce(next_action_id, gen_random_uuid()), "
            "next_phase = coalesce(next_phase, 'initial_collection') WHERE id = %s RETURNING generation",
            (capability_hash(token), job_id)).fetchone()
        sandbox = f"il-{str(job_id)[:8]}-g{generation}"
        conn.execute("UPDATE investigations SET sandbox_name = %s WHERE id = %s", (sandbox, job_id))
        append_event(conn, job_id, "running", {"generation": generation})
    return Claim(job_id, generation, token, sandbox)


def refresh_lease(conn: psycopg.Connection, job_id: uuid.UUID, generation: int) -> bool:
    """Extends the lease while the runner of this generation is alive. False when the job has
    moved on (another generation, cancelled, finished)."""
    with conn.transaction():
        row = conn.execute(f"UPDATE investigations SET lease_until = now() + interval '{LEASE}' "
                           "WHERE id = %s AND generation = %s AND status = 'RUNNING' RETURNING id",
                           (job_id, generation)).fetchone()
    return row is not None


def heartbeat(conn: psycopg.Connection, instance: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE control SET supervisor_instance = %s, supervisor_heartbeat_at = now()", (instance,))


def requeue_for_recovery(conn: psycopg.Connection, job_id: uuid.UUID, reason: str) -> str:
    """The runner died, or the lease expired. Revokes the capability (the old generation can do
    nothing more), then requeues the job for a bounded recovery, keeping its deadline and budget
    counters, or fails it after two recoveries (spec §12). Returns the new status."""
    with conn.transaction():
        row = conn.execute("SELECT status, recoveries FROM investigations WHERE id = %s FOR UPDATE",
                           (job_id,)).fetchone()
        if row is None or row[0] != "RUNNING":
            return row[0] if row else "MISSING"
        if row[1] >= MAX_RECOVERIES:
            append_event(conn, job_id, "failed", {"reason": reason, "recoveries": row[1]})
            conn.execute("UPDATE investigations SET status = 'FAILED', finished_at = now(), capability_hash = NULL, "
                         "final_reason = %s WHERE id = %s",
                         (f"{reason}; no recovery left after {MAX_RECOVERIES}", job_id))
            return "FAILED"
        abandon_unfinished_actions(conn, job_id)
        append_event(conn, job_id, "recovering", {"reason": reason, "recovery": row[1] + 1})
        conn.execute("UPDATE investigations SET status = 'QUEUED', recoveries = recoveries + 1, capability_hash = NULL, "
                     "lease_until = NULL, next_action_id = gen_random_uuid() WHERE id = %s", (job_id,))
        return "QUEUED"


def abandon_unfinished_actions(conn: psycopg.Connection, job_id: uuid.UUID) -> int:
    """At a takeover, the old generation's unfinished actions can never produce evidence (fencing).
    Closing them here keeps the new runner from waiting forever on one: reads become INTERRUPTED,
    dispatched model calls UNKNOWN (keeping their reservation), undispatched ones end with an
    error. The caller also issues a new next_action_id, so the abandoned ID is never replayed."""
    return conn.execute(
        "UPDATE actions SET status = CASE WHEN kind = 'tool' THEN 'INTERRUPTED' "
        "WHEN status = 'RUNNING' THEN 'UNKNOWN' ELSE 'ERROR' END, finished_at = now(), "
        "error = 'abandoned when the job was recovered by a new generation' "
        "WHERE investigation_id = %s AND status IN ('PENDING', 'RUNNING')", (job_id,)).rowcount


def finish_cancellation(conn: psycopg.Connection, job_id: uuid.UUID) -> bool:
    """CANCEL_REQUESTED -> CANCELLED once the runner is stopped."""
    with conn.transaction():
        row = conn.execute("SELECT status FROM investigations WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row[0] != "CANCEL_REQUESTED":
            return False
        append_event(conn, job_id, "cancelled", {"while": "running"})
        conn.execute("UPDATE investigations SET status = 'CANCELLED', finished_at = now(), capability_hash = NULL, "
                     "final_reason = 'cancelled by the developer' WHERE id = %s", (job_id,))
        return True


def fail_job(conn: psycopg.Connection, job_id: uuid.UUID, reason: str) -> bool:
    """An infrastructure or configuration failure: FAILED, with the reason and any evidence kept."""
    with conn.transaction():
        row = conn.execute("SELECT status FROM investigations WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row[0] not in ("QUEUED", "RUNNING"):
            return False
        append_event(conn, job_id, "failed", {"reason": reason})
        conn.execute("UPDATE investigations SET status = 'FAILED', finished_at = now(), capability_hash = NULL, "
                     "final_reason = %s WHERE id = %s", (reason, job_id))
        return True
