"""Job creation, admission and cancellation against real PostgreSQL, with genuinely parallel
connections for the concurrency cases (spec §17)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from incidentlens import jobs
from incidentlens.jobs import ApiError, CreateRequest


def request(symptom: str = "Transfers succeed but notifications are delayed", minutes: int = 15) -> CreateRequest:
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    return CreateRequest(service_id="ledgercore-local", symptom=symptom,
                         window={"start": end - timedelta(minutes=minutes), "end": end})


def supervisor_alive(owner) -> None:
    owner.execute("UPDATE control SET supervisor_instance = 'test', supervisor_heartbeat_at = now()")


def parallel(test_database, count: int, work) -> list:
    """Runs work(conn, index) on `count` separate connections released together."""
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(index: int) -> None:
        with psycopg.connect(test_database["app"], autocommit=True) as conn:
            barrier.wait()
            try:
                results[index] = work(conn, index)
            except ApiError as error:
                results[index] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_repeated_creates_yield_one_job(owner, app_store, test_database):
    supervisor_alive(owner)
    body = request()
    results = parallel(test_database, 12, lambda conn, i: jobs.create_investigation(conn, "local", body, "key-" + "a" * 20))
    assert all(isinstance(result, tuple) for result in results), results
    assert len({job_id for job_id, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False] + [True] * 11
    assert owner.execute("SELECT count(*) FROM investigations").fetchone()[0] == 1


def test_a_changed_body_with_the_same_key_is_a_conflict(owner, app_store):
    supervisor_alive(owner)
    jobs.create_investigation(app_store, "local", request("first symptom"), "key-" + "b" * 20)
    with pytest.raises(ApiError) as error:
        jobs.create_investigation(app_store, "local", request("different symptom"), "key-" + "b" * 20)
    assert (error.value.status, error.value.code) == (409, "IDEMPOTENCY_MISMATCH")


def test_queue_admission_never_exceeds_its_cap_under_concurrency(owner, app_store, test_database):
    supervisor_alive(owner)
    results = parallel(test_database, 25,
                       lambda conn, i: jobs.create_investigation(conn, "local", request(f"symptom {i}"), f"key-{i:020d}"))
    created = [r for r in results if isinstance(r, tuple)]
    rejected = [r for r in results if isinstance(r, ApiError)]
    assert len(created) == jobs.QUEUED_LIMIT
    assert {(e.status, e.code) for e in rejected} == {(429, "QUEUE_FULL")}
    assert owner.execute("SELECT count(*) FROM investigations WHERE status = 'QUEUED'").fetchone()[0] == 10


def test_a_replay_succeeds_even_when_the_queue_is_full_or_the_job_is_terminal(owner, app_store):
    supervisor_alive(owner)
    kept = request("kept")  # the same body object: request() takes its window from the current time
    first, _ = jobs.create_investigation(app_store, "local", kept, "key-" + "c" * 20)
    for i in range(jobs.QUEUED_LIMIT - 1):
        jobs.create_investigation(app_store, "local", request(f"filler {i}"), f"fill-{i:020d}")
    jobs.request_cancel(app_store, "local", first)
    assert jobs.create_investigation(app_store, "local", kept, "key-" + "c" * 20) == (first, True)


def test_keys_are_scoped_to_the_owner(owner, app_store):
    supervisor_alive(owner)
    mine, _ = jobs.create_investigation(app_store, "alice", request(), "key-" + "d" * 20)
    theirs, replayed = jobs.create_investigation(app_store, "bob", request(), "key-" + "d" * 20)
    assert mine != theirs and replayed is False


def test_windows_are_validated_against_the_database_clock(owner, app_store):
    supervisor_alive(owner)
    now = datetime.now(timezone.utc)
    cases = {
        "too short": ({"start": now - timedelta(minutes=2), "end": now - timedelta(seconds=90)}, "INVALID_WINDOW"),
        "too long": ({"start": now - timedelta(minutes=40), "end": now - timedelta(minutes=5)}, "INVALID_WINDOW"),
        "future": ({"start": now - timedelta(minutes=5), "end": now + timedelta(minutes=5)}, "INVALID_WINDOW"),
        "old": ({"start": now - timedelta(hours=25), "end": now - timedelta(hours=24, minutes=50)},
                "WINDOW_OUT_OF_RETENTION"),
    }
    for index, (window, code) in enumerate(cases.values()):
        body = CreateRequest(service_id="ledgercore-local", symptom="x", window=window)
        with pytest.raises(ApiError) as error:
            jobs.create_investigation(app_store, "local", body, f"window-{index:014d}")
        assert (error.value.status, error.value.code) == (422, code)


def test_creation_is_refused_while_the_supervisor_is_down(owner, app_store):
    owner.execute("UPDATE control SET supervisor_heartbeat_at = now() - interval '31 seconds'")
    with pytest.raises(ApiError) as error:
        jobs.create_investigation(app_store, "local", request(), "key-" + "e" * 20)
    assert (error.value.status, error.value.code, error.value.retryable) == (503, "SUPERVISOR_UNAVAILABLE", True)


def test_invalid_idempotency_keys_are_rejected(owner, app_store):
    supervisor_alive(owner)
    for key in ("short", "x" * 129, "has\nnewline-0000000"):
        with pytest.raises(ApiError) as error:
            jobs.create_investigation(app_store, "local", request(), key)
        assert error.value.code == "INVALID_IDEMPOTENCY_KEY"


def test_one_active_job_at_a_time_and_claims_set_the_deadline_once(owner, app_store):
    supervisor_alive(owner)
    first, _ = jobs.create_investigation(app_store, "local", request("one"), "key-" + "f" * 20)
    jobs.create_investigation(app_store, "local", request("two"), "key-" + "g" * 20)
    claim = jobs.claim_next(app_store)
    assert claim.job_id == first and claim.generation == 1
    assert jobs.claim_next(app_store) is None  # one active job
    stored_hash, deadline_seconds = owner.execute(
        "SELECT capability_hash, extract(epoch FROM deadline - started_at) FROM investigations WHERE id = %s",
        (first,)).fetchone()
    assert stored_hash == jobs.capability_hash(claim.capability) and claim.capability not in stored_hash
    assert deadline_seconds == 180


def test_cancelling_a_queued_job_is_immediate_and_a_terminal_job_is_untouched(owner, app_store):
    supervisor_alive(owner)
    job, _ = jobs.create_investigation(app_store, "local", request(), "key-" + "h" * 20)
    assert jobs.request_cancel(app_store, "local", job) == ("CANCELLED", False)
    assert jobs.request_cancel(app_store, "local", job) == ("CANCELLED", True)
    with pytest.raises(ApiError) as error:
        jobs.request_cancel(app_store, "someone-else", job)
    assert error.value.status == 404


def test_cancelling_a_running_job_revokes_its_capability(owner, app_store):
    supervisor_alive(owner)
    job, _ = jobs.create_investigation(app_store, "local", request(), "key-" + "i" * 20)
    jobs.claim_next(app_store)
    assert jobs.request_cancel(app_store, "local", job) == ("CANCEL_REQUESTED", False)
    assert owner.execute("SELECT capability_hash FROM investigations WHERE id = %s", (job,)).fetchone()[0] is None


def test_queued_jobs_expire_after_ten_minutes(owner, app_store):
    supervisor_alive(owner)
    job, _ = jobs.create_investigation(app_store, "local", request(), "key-" + "j" * 20)
    owner.execute("UPDATE investigations SET queue_expires_at = now() - interval '1 second' WHERE id = %s", (job,))
    assert jobs.expire_queued(app_store) == [job]
    assert owner.execute("SELECT status, final_reason FROM investigations WHERE id = %s", (job,)).fetchone() == \
        ("FAILED", "queue wait expired after 10 minutes")


def test_a_job_is_recovered_at_most_twice_then_fails(owner, app_store):
    supervisor_alive(owner)
    job, _ = jobs.create_investigation(app_store, "local", request(), "key-" + "k" * 20)
    outcomes = []
    for _ in range(3):
        jobs.claim_next(app_store)
        outcomes.append(jobs.requeue_for_recovery(app_store, job, "the runner exited"))
    assert outcomes == ["QUEUED", "QUEUED", "FAILED"]
    status, recoveries, reason = owner.execute("SELECT status, recoveries, final_reason FROM investigations "
                                               "WHERE id = %s", (job,)).fetchone()
    assert (status, recoveries) == ("FAILED", 2) and "no recovery left" in reason
