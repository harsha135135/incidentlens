"""The executor against real PostgreSQL, the live telemetry sources and the test-only fake
provider: tool results become evidence, model replies become typed decisions, and provider
failures keep their reservations (spec §12)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from incidentlens import evidence, jobs
from incidentlens.evidence import ActionRequest
from incidentlens.executor import Executor
from incidentlens.jobs import CreateRequest
from incidentlens.model_gateway import ModelSettings
from tests.support.fake_provider import FakeProvider


@pytest.fixture
def provider():
    fake = FakeProvider().start()
    yield fake
    fake.stop()


@pytest.fixture
def executor(settings, test_database, provider):
    return Executor(dataclasses.replace(settings, app_conninfo=test_database["app"]),
                    ModelSettings(base_url=provider.url, api_key="test", timeout_seconds=2))


def running_job(owner, app_store) -> jobs.Claim:
    owner.execute("UPDATE control SET supervisor_instance = 'test', supervisor_heartbeat_at = now()")
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    body = CreateRequest(service_id="ledgercore-local", symptom="notifications are late",
                         window={"start": end - timedelta(minutes=5), "end": end})
    jobs.create_investigation(app_store, "local", body, "key-" + uuid.uuid4().hex)
    return jobs.claim_next(app_store)


def submit(owner, app_store, claim, request_kind="model", tool=None, arguments=None) -> uuid.UUID:
    action_id = owner.execute("SELECT next_action_id FROM investigations WHERE id = %s", (claim.job_id,)).fetchone()[0]
    evidence.submit_action(app_store, claim.job_id, claim.capability,
                           ActionRequest(action_id, request_kind, tool, arguments))
    return action_id


def action_row(owner, action_id):
    return owner.execute("SELECT status, response, error, reserved_tokens, input_tokens FROM actions WHERE id = %s",
                         (action_id,)).fetchone()


def test_a_tool_action_runs_against_the_live_sources_and_becomes_evidence(owner, app_store, executor):
    claim = running_job(owner, app_store)
    action_id = submit(owner, app_store, claim, "tool", "get_outbox_stats", {})
    assert executor.run_one(app_store)
    status, response, *_ = action_row(owner, action_id)
    assert status == "SUCCEEDED" and response["handle"] == "E1"
    stored = owner.execute("SELECT envelope->>'tool', envelope->>'coverage' FROM evidence").fetchone()
    assert stored[0] == "get_outbox_stats" and stored[1] in ("COMPLETE", "PARTIAL", "UNAVAILABLE")


def test_the_product_offers_only_terminal_actions_at_the_decision(owner, app_store, executor, provider):
    """D48: after the fixed bundle the model may only report or stop; a tool call is malformed."""
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "tool", "name": "get_delivery_timings", "input": {}}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    _, response, *_ = action_row(owner, action_id)
    assert response["type"] == "malformed" and "unknown or unavailable" in response["problem"]


def test_a_model_reply_becomes_a_typed_decision_and_usage_is_reconciled(owner, app_store, executor, provider,
                                                                       monkeypatch):
    from incidentlens import model_gateway
    monkeypatch.setattr(model_gateway, "INVESTIGATION_MODE", "agent")  # the agent's call_tool decision
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "tool", "name": "get_delivery_timings", "input": {}}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    status, response, _, reserved, input_tokens = action_row(owner, action_id)
    assert status == "SUCCEEDED" and response["type"] == "call_tool" and response["tool"] == "get_delivery_timings"
    assert input_tokens == 1_200 and reserved > 1_500
    assert owner.execute("SELECT tokens_reserved, next_phase FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone() == (1_280, "tool_observation")
    paths = [entry["path"] for entry in provider.log]
    assert paths == ["/v1/messages/count_tokens", "/v1/messages"]  # counted before it was sent


def test_a_rate_limited_call_is_an_error_that_keeps_its_reservation(owner, app_store, executor, provider):
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "status", "code": 429}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    status, _, error, reserved, _ = action_row(owner, action_id)
    assert (status, error) == ("ERROR", "rate_limited (429)")
    assert owner.execute("SELECT tokens_reserved FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone()[0] == reserved
    assert len(provider.messages_requests()) == 1  # no automatic retry


def test_a_timed_out_call_is_unknown_and_keeps_its_reservation(owner, app_store, executor, provider):
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "hang", "seconds": 5}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    status, _, error, reserved, _ = action_row(owner, action_id)
    assert status == "UNKNOWN" and "outcome unknown" in error
    assert owner.execute("SELECT tokens_reserved FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone()[0] == reserved


def test_two_actions_in_one_reply_are_malformed(owner, app_store, executor, provider):
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "malformed"}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    _, response, *_ = action_row(owner, action_id)
    assert response["type"] == "malformed" and "exactly one" in response["problem"]
    assert owner.execute("SELECT next_phase FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone()[0] == "model_decision"


def test_a_reply_cut_off_at_the_output_limit_is_malformed(owner, app_store, executor, provider):
    claim = running_job(owner, app_store)
    provider.set_script([{"reply": "cut_off"}])
    action_id = submit(owner, app_store, claim)
    executor.run_one(app_store)
    _, response, *_ = action_row(owner, action_id)
    assert response["type"] == "malformed" and "1,500-token" in response["problem"]


def test_a_cancelled_job_gets_no_model_call(owner, app_store, executor, provider):
    claim = running_job(owner, app_store)
    action_id = submit(owner, app_store, claim)
    jobs.request_cancel(app_store, "local", claim.job_id)
    executor.run_one(app_store)
    status, _, error, *_ = action_row(owner, action_id)
    assert status == "ERROR" and "moved on" in error
    assert provider.messages_requests() == []
