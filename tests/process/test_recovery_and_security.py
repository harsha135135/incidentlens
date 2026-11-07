"""Spec §17 reliability and security tests with real processes and real signals, against real
PostgreSQL. The provider and Prometheus are the loopback test fake (deterministic failure tests);
the runner is the labelled unsandboxed development mode (D14). Real-provider and real-sandbox
end-to-end runs are Stage 3 items."""

from __future__ import annotations

import os
import signal
import time

import httpx

from tests.process.conftest import (
    BROWSER_HEADERS,
    PUBLIC,
    Process,
    create_job,
    job_row,
    report_step,
    runner_pid,
    wait_until,
)


def status(owner, job_id) -> str:
    return job_row(owner, job_id)[0]


def test_a_full_investigation_completes_through_the_real_processes(owner, client, supervisor, fake):
    """The product (D48): the fixed bundle as tool actions, then one model decision that reports."""
    from incidentlens.bundle import FIXED_BUNDLE
    fake.set_script([report_step("E1", "E3")])
    job_id = create_job(client)
    wait_until(lambda: status(owner, job_id) == "COMPLETED", "the job to complete", 60)
    report = client.get(f"/api/v1/investigations/{job_id}/report").json()
    assert report["source"] == "model"
    assert report["report"]["findings"][0]["evidence_handles"] == ["E1", "E3"]
    tools = [row[0] for row in owner.execute("SELECT tool FROM actions WHERE investigation_id = %s AND kind = 'tool' "
                                             "ORDER BY created_at", (job_id,)).fetchall()]
    assert tools == [tool for tool, _ in FIXED_BUNDLE]
    assert len(fake.messages_requests()) == 1  # one model decision
    events = client.get(f"/api/v1/investigations/{job_id}/events").json()["events"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "completed"


def test_a_runner_killed_after_an_evidence_commit_resumes_without_repeating_it(owner, client, supervisor, fake):
    fake.query_delay = 2.0  # the first read takes long enough to freeze the runner while it waits
    fake.set_script([report_step("E1")])
    job_id = create_job(client)
    wait_until(lambda: owner.execute("SELECT 1 FROM actions WHERE investigation_id = %s AND kind = 'tool'",
                                     (job_id,)).fetchone(), "the first read to be admitted")
    pid = runner_pid()
    os.kill(pid, signal.SIGSTOP)  # the runner cannot receive the result
    wait_until(lambda: owner.execute("SELECT evidence_count FROM investigations WHERE id = %s",
                                     (job_id,)).fetchone()[0] >= 1, "the evidence commit")
    os.kill(pid, signal.SIGKILL)  # dies after the commit, before receiving the response
    fake.query_delay = 0.0

    wait_until(lambda: status(owner, job_id) == "COMPLETED", "the recovered job to complete", 60)
    state, generation, recoveries, *_ = job_row(owner, job_id)
    assert (generation, recoveries) == (2, 1)
    reads = owner.execute("SELECT tool, count(*) FROM actions WHERE investigation_id = %s AND kind = 'tool' "
                          "AND status = 'SUCCEEDED' GROUP BY tool", (job_id,)).fetchall()
    from incidentlens.bundle import FIXED_BUNDLE
    assert dict(reads) == {tool: 1 for tool, _ in FIXED_BUNDLE}  # the saved read was reused, never repeated
    assert owner.execute("SELECT deadline - started_at FROM investigations WHERE id = %s",
                         (job_id,)).fetchone()[0].total_seconds() == 180  # recovery kept the deadline


def test_a_gateway_killed_during_a_model_call_keeps_the_reservation_and_retries_stay_bounded(
        owner, client, supervisor, gateway, fake):
    fake.set_script([{"reply": "hang", "seconds": 20}, report_step("E1")])
    job_id = create_job(client)
    running = wait_until(lambda: owner.execute(
        "SELECT id, reserved_tokens FROM actions WHERE investigation_id = %s AND kind = 'model' AND status = 'RUNNING'",
        (job_id,)).fetchone(), "the model call to be dispatched", 30)
    gateway.kill(signal.SIGKILL)
    gateway.start()  # startup recovery: the dispatched call becomes UNKNOWN

    wait_until(lambda: status(owner, job_id) == "COMPLETED", "the job to complete after the restart", 60)
    unknown_status, reserved = owner.execute("SELECT status, reserved_tokens FROM actions WHERE id = %s",
                                             (running[0],)).fetchone()
    assert unknown_status == "UNKNOWN" and reserved == running[1]
    _, _, _, model_actions, _, tokens_reserved, _ = job_row(owner, job_id)
    reconciled = owner.execute("SELECT coalesce(sum(input_tokens + output_tokens), 0) FROM actions "
                               "WHERE investigation_id = %s AND status = 'SUCCEEDED' AND kind = 'model'",
                               (job_id,)).fetchone()[0]
    assert tokens_reserved == reserved + reconciled  # the unknown charge is still counted
    assert model_actions == 2  # one unknown call, one retry: bounded


def test_cancelling_while_a_read_is_in_flight_starts_no_model_call_and_publishes_no_report(
        owner, client, supervisor, fake):
    fake.query_delay = 5.0
    job_id = create_job(client)
    wait_until(lambda: owner.execute("SELECT 1 FROM actions WHERE investigation_id = %s AND status = 'RUNNING'",
                                     (job_id,)).fetchone(), "a read to be in flight")
    assert client.post(f"/api/v1/investigations/{job_id}/cancel").status_code == 202
    wait_until(lambda: status(owner, job_id) == "CANCELLED", "the cancellation to finish", 30)
    time.sleep(6)  # let the slow read return; its result must be discarded
    assert fake.messages_requests() == []
    assert owner.execute("SELECT count(*) FROM reports WHERE investigation_id = %s", (job_id,)).fetchone()[0] == 0
    assert owner.execute("SELECT count(*) FROM evidence WHERE investigation_id = %s", (job_id,)).fetchone()[0] == 0
    assert client.get(f"/api/v1/investigations/{job_id}/report").status_code == 409


def test_losing_the_incidentlens_database_starts_no_unrecorded_work(owner, client, supervisor, fake, proxy, env,
                                                                     tmp_path):
    fake.query_delay = 1.0
    fake.set_script([report_step("E1")])
    job_id = create_job(client)
    wait_until(lambda: owner.execute("SELECT 1 FROM actions WHERE investigation_id = %s", (job_id,)).fetchone(),
               "the first action")
    proxy.cut()
    time.sleep(2)  # anything already in flight finishes or fails to commit
    seen_sources, seen_models = len(fake.source_requests()), len(fake.messages_requests())
    time.sleep(8)
    assert len(fake.source_requests()) == seen_sources, "a source was queried while the database was down"
    assert len(fake.messages_requests()) == seen_models, "the model was called while the database was down"

    proxy.restore()
    if supervisor.process.poll() is not None:  # it exits when its database connection is lost (spec §12)
        Process("supervisor", env, tmp_path).start()
    wait_until(lambda: status(owner, job_id) == "COMPLETED", "recovery after the database returned", 90, 1.0)


def test_a_second_supervisor_exits_instead_of_running_jobs(supervisor, env, tmp_path):
    time.sleep(1)
    second = Process("supervisor", env, tmp_path).start()
    assert second.process.wait(timeout=15) == 1


def test_the_deadline_ends_the_job_with_the_fallback_report(owner, client, supervisor, fake):
    fake.set_script([{"reply": "hang", "seconds": 8}])
    job_id = create_job(client)
    wait_until(lambda: owner.execute("SELECT 1 FROM actions WHERE investigation_id = %s AND kind = 'model' "
                                     "AND status = 'RUNNING'", (job_id,)).fetchone(), "the model call", 30)
    owner.execute("UPDATE investigations SET deadline = now() + interval '1 second' WHERE id = %s", (job_id,))
    wait_until(lambda: status(owner, job_id) == "COMPLETED", "the deadline fallback", 20)
    report = client.get(f"/api/v1/investigations/{job_id}/report").json()
    assert report["source"] == "fallback" and "deadline" in report["report"]["summary"]
    time.sleep(8)  # the late model reply must not change the finished job
    assert status(owner, job_id) == "COMPLETED"
    assert owner.execute("SELECT count(*) FROM reports WHERE investigation_id = %s", (job_id,)).fetchone()[0] == 1


def test_sessions_csrf_origin_host_and_ownership_are_enforced(owner, client, supervisor):
    job_id = create_job(client)
    anonymous = httpx.Client(base_url=PUBLIC, headers=BROWSER_HEADERS, timeout=10)
    assert anonymous.get(f"/api/v1/investigations/{job_id}").status_code == 401
    assert anonymous.get("/health/live").status_code == 200

    no_csrf = dict(client.headers)
    no_csrf.pop("x-csrf-token")
    assert httpx.post(f"{PUBLIC}/api/v1/investigations/{job_id}/cancel", headers=no_csrf,
                      cookies=client.cookies).status_code == 403
    wrong_origin = dict(client.headers) | {"origin": "http://evil.example"}  # keys are lowercase
    assert httpx.post(f"{PUBLIC}/api/v1/investigations/{job_id}/cancel", headers=wrong_origin,
                      cookies=client.cookies).status_code == 403
    rebound_host = dict(client.headers) | {"host": "attacker.example:8088"}
    assert httpx.get(f"{PUBLIC}/api/v1/investigations/{job_id}", headers=rebound_host,
                     cookies=client.cookies).status_code == 403

    # Another developer's job, with evidence and a report: every endpoint answers exactly as for a
    # missing job. A real evidence ID requested under the wrong job is also 404.
    other_job, other_evidence = insert_foreign_job(owner)
    missing = "00000000-0000-4000-8000-000000000000"
    expected = client.get(f"/api/v1/investigations/{missing}").json()["error"]["code"]
    probes = [("GET", ""), ("GET", "/events"), ("GET", "/evidence"), ("GET", f"/evidence/{other_evidence}"),
              ("GET", "/report"), ("POST", "/cancel")]
    for method, suffix in probes:
        response = client.request(method, f"/api/v1/investigations/{other_job}{suffix}")
        assert (response.status_code, response.json()["error"]["code"]) == (404, expected), (method, suffix)
    assert client.get(f"/api/v1/investigations/{job_id}/evidence/{other_evidence}").status_code == 404
    assert owner.execute("SELECT status FROM investigations WHERE id = %s", (other_job,)).fetchone()[0] == "COMPLETED"

    oversized = client.post("/api/v1/investigations", headers={"Idempotency-Key": "x" * 20},
                            content=b"{" + b" " * 17_000 + b"}")
    assert oversized.status_code == 413
    unknown_field = client.post("/api/v1/investigations", headers={"Idempotency-Key": "y" * 20},
                                json={"service_id": "ledgercore-local", "symptom": "x", "model": "other",
                                      "window": {"start": "2026-11-06T00:00:00Z", "end": "2026-11-06T00:10:00Z"}})
    assert unknown_field.status_code == 422


def insert_foreign_job(owner) -> tuple[str, str]:
    """A completed job owned by someone else, inserted directly, with one evidence row and a report."""
    import uuid as uuid_module
    job, action, evidence_id = (str(uuid_module.uuid4()) for _ in range(3))
    owner.execute("INSERT INTO investigations (id, owner, service_id, symptom, window_start, window_end, profile, "
                  "status, queue_expires_at) VALUES (%s, 'another-developer', 'ledgercore-local', 'theirs', "
                  "now() - interval '10 min', now() - interval '5 min', 'standard', 'QUEUED', now())", (job,))
    owner.execute("UPDATE investigations SET status = 'RUNNING', generation = 1 WHERE id = %s", (job,))
    owner.execute("INSERT INTO actions (id, investigation_id, generation, kind, tool, arguments, input_hash, status) "
                  "VALUES (%s, %s, 1, 'tool', 'get_outbox_stats', '{}', 'h', 'SUCCEEDED')", (action, job))
    owner.execute("INSERT INTO evidence (id, action_id, investigation_id, ordinal, envelope, sha256, byte_count) "
                  "VALUES (%s, %s, %s, 1, '{\"secret\": \"theirs\"}', 'h', 20)", (evidence_id, action, job))
    owner.execute("INSERT INTO reports (investigation_id, schema_version, outcome, report, source) "
                  "VALUES (%s, '1.0', 'INCONCLUSIVE', '{}', 'fallback')", (job,))
    owner.execute("UPDATE investigations SET status = 'COMPLETED' WHERE id = %s", (job,))
    return job, evidence_id


def test_logins_are_limited_to_five_per_minute(owner, gateway):
    http = httpx.Client(base_url=PUBLIC, headers=BROWSER_HEADERS, timeout=10)
    codes = [http.post("/api/v1/session", json={"secret": "wrong"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    assert http.post("/api/v1/session", content=b'{"secret":"x"}',
                     headers={"Content-Type": "text/plain"}).status_code == 415
