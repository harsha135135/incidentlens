"""Report validation, finalization and the deterministic fallback against real PostgreSQL
(spec §14, §12)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from incidentlens import evidence, jobs, reports
from incidentlens.evidence import ActionRequest
from incidentlens.jobs import ApiError, CreateRequest
from incidentlens.tools.envelope import Window, build_envelope


def running_job(owner, app_store) -> jobs.Claim:
    owner.execute("UPDATE control SET supervisor_instance = 'test', supervisor_heartbeat_at = now()")
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    body = CreateRequest(service_id="ledgercore-local", symptom="notifications are late",
                         window={"start": end - timedelta(minutes=5), "end": end})
    jobs.create_investigation(app_store, "local", body, "key-" + uuid.uuid4().hex)
    return jobs.claim_next(app_store)


def next_id(owner, job_id):
    return owner.execute("SELECT next_action_id FROM investigations WHERE id = %s", (job_id,)).fetchone()[0]


def observe(owner, app_store, claim, tool="get_outbox_stats", coverage="COMPLETE", limitations=None):
    action_id = next_id(owner, claim.job_id)
    arguments = {"route_group": "transfers"} if tool == "get_request_metrics" else {}
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(action_id, "tool", tool, arguments))
    evidence.start_tool(app_store, action_id)
    end = datetime.now(timezone.utc)
    envelope = build_envelope(tool, {}, Window(end - timedelta(minutes=5), end), coverage, "test", {"n": 1},
                              limitations or [], [])
    evidence.commit_tool_result(app_store, action_id, envelope)
    return envelope["evidence_id"]


def decide(owner, app_store, claim, decision: dict) -> uuid.UUID:
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(action_id, "model"))
    evidence.reserve_model_call(app_store, action_id, 2_000)
    evidence.commit_model_result(app_store, action_id, decision, 2_000, 200, "msg_test")
    return action_id


def report_decision(*citations: str) -> dict:
    return {"type": "submit_report", "report": {
        "outcome": "TENTATIVE", "summary": "Publications fell behind arrivals.",
        "findings": [{"claim": "Arrivals exceeded publications.", "support": "observed",
                      "evidence_ids": list(citations), "reason": "Durable counts over the same window."}],
        "alternatives": [], "limitations": ["Consumer timing was not collected."], "next_checks": ["Collect timings."]}}


def stored(owner, job_id):
    return owner.execute("SELECT status, (SELECT report FROM reports WHERE investigation_id = i.id), "
                         "(SELECT source FROM reports WHERE investigation_id = i.id) FROM investigations i WHERE id = %s",
                         (job_id,)).fetchone()


def test_a_report_is_stored_with_real_evidence_ids_server_usage_and_required_limitations(owner, app_store):
    claim = running_job(owner, app_store)
    first = observe(owner, app_store, claim)
    observe(owner, app_store, claim, "get_request_metrics", "PARTIAL", ["Scrapes are missing for 20 s."])
    action_id = decide(owner, app_store, claim, report_decision("E1"))

    assert reports.submit_report(app_store, claim.job_id, claim.capability, action_id)["status"] == "COMPLETED"
    status, report, source = stored(owner, claim.job_id)
    assert (status, source) == ("COMPLETED", "model")
    assert report["findings"][0]["evidence_ids"] == [first]
    assert report["findings"][0]["evidence_handles"] == ["E1"]
    assert report["usage"]["model_actions"] == 1 and report["usage"]["reported_output_tokens"] == 200
    assert any(note.startswith("E2 (get_request_metrics) is PARTIAL") for note in report["limitations"])
    assert owner.execute("SELECT capability_hash FROM investigations WHERE id = %s", (claim.job_id,)).fetchone()[0] is None


def test_unknown_citations_get_one_repair_then_the_fallback(owner, app_store):
    claim = running_job(owner, app_store)
    observe(owner, app_store, claim)
    first_try = decide(owner, app_store, claim, report_decision("E7"))
    result = reports.submit_report(app_store, claim.job_id, claim.capability, first_try)
    assert result == {"status": "REPAIR_REQUESTED", "unknown_evidence": ["E7"]}
    phase, decision = owner.execute("SELECT next_phase, last_decision FROM investigations WHERE id = %s",
                                    (claim.job_id,)).fetchone()
    assert phase == "model_decision" and "E7" in decision["problem"]

    second_try = decide(owner, app_store, claim, report_decision("E9"))
    assert reports.submit_report(app_store, claim.job_id, claim.capability, second_try)["source"] == "fallback"
    status, report, source = stored(owner, claim.job_id)
    assert (status, source, report["outcome"]) == ("COMPLETED", "fallback", "INCONCLUSIVE")
    assert report["findings"] == []


def test_a_second_malformed_reply_ends_in_the_fallback(owner, app_store):
    claim = running_job(owner, app_store)
    decide(owner, app_store, claim, {"type": "malformed", "problem": "two actions"})
    assert owner.execute("SELECT status, repairs_used FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone() == ("RUNNING", 1)
    decide(owner, app_store, claim, {"type": "malformed", "problem": "two actions again"})
    status, report, source = stored(owner, claim.job_id)
    assert (status, source) == ("COMPLETED", "fallback")
    assert "malformed again" in report["summary"]


def test_a_stop_decision_becomes_an_inconclusive_report_naming_the_missing_evidence(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = decide(owner, app_store, claim, {"type": "stop_inconclusive", "reason": "No consumer timings.",
                                                 "missing_evidence": ["consumer timing for the window"]})
    reports.submit_report(app_store, claim.job_id, claim.capability, action_id)
    _, report, source = stored(owner, claim.job_id)
    assert (report["outcome"], source) == ("INCONCLUSIVE", "model")
    assert report["next_checks"] == ["consumer timing for the window"]


def test_the_runner_cannot_substitute_a_tool_or_call_tool_decision_for_a_report(owner, app_store):
    claim = running_job(owner, app_store)
    observe(owner, app_store, claim)
    tool_action = owner.execute("SELECT id FROM actions WHERE kind = 'tool'").fetchone()[0]
    with pytest.raises(ApiError) as error:
        reports.submit_report(app_store, claim.job_id, claim.capability, tool_action)
    assert error.value.code == "NOT_A_DECISION"
    call = decide(owner, app_store, claim, {"type": "call_tool", "tool": "get_delivery_timings", "arguments": {}})
    with pytest.raises(ApiError) as error:
        reports.submit_report(app_store, claim.job_id, claim.capability, call)
    assert error.value.code == "NOT_A_REPORT"


def test_a_cancelled_job_cannot_be_completed_by_a_late_report_or_fallback(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = decide(owner, app_store, claim, report_decision())
    jobs.request_cancel(app_store, "local", claim.job_id)
    with pytest.raises(ApiError) as error:
        reports.submit_report(app_store, claim.job_id, claim.capability, action_id)
    assert error.value.status == 403
    assert reports.finalize_fallback(app_store, claim.job_id, "deadline")["status"] == "CANCEL_REQUESTED"
    assert owner.execute("SELECT count(*) FROM reports").fetchone()[0] == 0


def test_the_deadline_fallback_completes_once_and_never_rewrites(owner, app_store):
    claim = running_job(owner, app_store)
    observe(owner, app_store, claim)
    assert reports.finalize_fallback(app_store, claim.job_id, "the 180-second deadline passed")["source"] == "fallback"
    assert reports.finalize_fallback(app_store, claim.job_id, "again")["status"] == "COMPLETED"
    _, report, _ = stored(owner, claim.job_id)
    assert "180-second deadline" in report["summary"]
    assert "E1 get_outbox_stats (COMPLETE)" in report["limitations"][0]


def test_a_lost_source_never_becomes_a_healthy_result(owner, app_store):
    claim = running_job(owner, app_store)
    observe(owner, app_store, claim, "get_outbox_stats", "UNAVAILABLE", ["Prometheus could not be queried."])
    healthy = report_decision("E1")
    healthy["report"]["outcome"] = "NO_FAULT_OBSERVED"
    action_id = decide(owner, app_store, claim, healthy)
    reports.submit_report(app_store, claim.job_id, claim.capability, action_id)
    _, report, _ = stored(owner, claim.job_id)
    assert report["outcome"] == "INCONCLUSIVE"
    assert any("cannot be concluded" in note for note in report["limitations"])
    assert any(note.startswith("E1 (get_outbox_stats) is UNAVAILABLE") for note in report["limitations"])


def test_the_old_generations_capability_cannot_finalize_after_a_takeover(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = decide(owner, app_store, claim, report_decision())
    jobs.requeue_for_recovery(app_store, claim.job_id, "the runner exited")
    jobs.claim_next(app_store)
    with pytest.raises(ApiError) as error:
        reports.submit_report(app_store, claim.job_id, claim.capability, action_id)
    assert error.value.status == 403
    with pytest.raises(ApiError) as error:
        reports.finalize_fallback(app_store, claim.job_id, "give up", claim.capability)
    assert error.value.status == 403
    assert owner.execute("SELECT count(*) FROM reports").fetchone()[0] == 0


def test_a_capability_expires_at_the_deadline(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = decide(owner, app_store, claim, report_decision())
    owner.execute("UPDATE investigations SET deadline = now() - interval '1 second' WHERE id = %s", (claim.job_id,))
    with pytest.raises(ApiError) as error:
        reports.submit_report(app_store, claim.job_id, claim.capability, action_id)
    assert (error.value.status, error.value.code) == (403, "DEADLINE_EXCEEDED")
    with pytest.raises(ApiError):
        evidence.job_context(app_store, claim.job_id, claim.capability)
