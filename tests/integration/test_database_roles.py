"""Spec §11: application roles cannot edit finalized observations; terminal jobs and actions are
immutable. Checked through the real logins, not the owner."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb


def insert_job(owner, status: str = "QUEUED") -> uuid.UUID:
    job = uuid.uuid4()
    owner.execute("INSERT INTO investigations (id, owner, service_id, symptom, window_start, window_end, profile, "
                  "status, queue_expires_at) VALUES (%s, 'local', 'ledgercore-local', 'x', now() - interval '10 min', "
                  "now() - interval '1 min', 'standard', 'QUEUED', now() + interval '10 min')", (job,))
    if status != "QUEUED":
        owner.execute("UPDATE investigations SET status = 'RUNNING' WHERE id = %s", (job,))
        if status != "RUNNING":
            owner.execute("UPDATE investigations SET status = %s WHERE id = %s", (status, job))
    return job


def insert_evidence(owner, job: uuid.UUID) -> uuid.UUID:
    action = uuid.uuid4()
    owner.execute("INSERT INTO actions (id, investigation_id, generation, kind, tool, arguments, input_hash, status) "
                  "VALUES (%s, %s, 1, 'tool', 'get_outbox_stats', '{}', 'h', 'RUNNING')", (action, job))
    owner.execute("UPDATE actions SET status = 'SUCCEEDED' WHERE id = %s", (action,))
    evidence = uuid.uuid4()
    owner.execute("INSERT INTO evidence (id, action_id, investigation_id, ordinal, envelope, sha256, byte_count) "
                  "VALUES (%s, %s, %s, 1, %s, 'h', 2)", (evidence, action, job, Jsonb({})))
    return action


def denied(conn, statement: str, params=()) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement, params)


def test_the_application_login_is_not_a_superuser(app_store):
    assert app_store.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone() \
        == (False, False)


def test_the_application_cannot_edit_or_delete_finalized_observations(owner, app_store):
    job = insert_job(owner, "RUNNING")
    insert_evidence(owner, job)
    owner.execute("INSERT INTO progress_events (investigation_id, sequence, type, payload) VALUES (%s, 1, 'x', '{}')",
                  (job,))
    owner.execute("INSERT INTO reports (investigation_id, schema_version, outcome, report, source) "
                  "VALUES (%s, '1.0', 'INCONCLUSIVE', '{}', 'fallback')", (job,))
    for table in ("evidence", "progress_events", "reports"):
        denied(app_store, f"UPDATE {table} SET created_at = now() WHERE investigation_id = %s", (job,))
        denied(app_store, f"DELETE FROM {table} WHERE investigation_id = %s", (job,))
    denied(app_store, "DELETE FROM investigations WHERE id = %s", (job,))
    denied(app_store, "DELETE FROM actions WHERE investigation_id = %s", (job,))


def test_terminal_investigations_cannot_change_even_for_the_owner(owner):
    job = insert_job(owner, "COMPLETED")
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
        owner.execute("UPDATE investigations SET symptom = 'rewritten' WHERE id = %s", (job,))


def test_illegal_transitions_are_rejected(owner):
    job = insert_job(owner)
    with pytest.raises(psycopg.errors.RaiseException, match="QUEUED -> COMPLETED"):
        owner.execute("UPDATE investigations SET status = 'COMPLETED' WHERE id = %s", (job,))
    owner.execute("UPDATE investigations SET status = 'RUNNING' WHERE id = %s", (job,))
    owner.execute("UPDATE investigations SET status = 'CANCEL_REQUESTED' WHERE id = %s", (job,))
    with pytest.raises(psycopg.errors.RaiseException, match="CANCEL_REQUESTED -> COMPLETED"):
        owner.execute("UPDATE investigations SET status = 'COMPLETED' WHERE id = %s", (job,))


def test_terminal_actions_are_immutable(owner, app_store):
    job = insert_job(owner, "RUNNING")
    action = insert_evidence(owner, job)
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
        app_store.execute("UPDATE actions SET response = '{}' WHERE id = %s", (action,))


def test_the_collector_cannot_see_jobs_evidence_or_sessions(collector_store):
    for table in ("investigations", "actions", "evidence", "reports", "sessions", "submission_keys", "control"):
        denied(collector_store, f"SELECT * FROM {table} LIMIT 1")
