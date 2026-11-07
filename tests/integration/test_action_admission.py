"""Action admission, result commit and fencing against real PostgreSQL (spec §8, §12, §17)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from incidentlens import evidence, jobs
from incidentlens.evidence import ActionRequest
from incidentlens.jobs import ApiError, CreateRequest
from incidentlens.tools.envelope import Window, build_envelope


def running_job(owner, app_store) -> jobs.Claim:
    owner.execute("UPDATE control SET supervisor_instance = 'test', supervisor_heartbeat_at = now()")
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    body = CreateRequest(service_id="ledgercore-local", symptom="notifications are late",
                         window={"start": end - timedelta(minutes=15), "end": end})
    jobs.create_investigation(app_store, "local", body, "key-" + uuid.uuid4().hex)
    return jobs.claim_next(app_store)


def next_id(owner, job_id) -> uuid.UUID:
    return owner.execute("SELECT next_action_id FROM investigations WHERE id = %s", (job_id,)).fetchone()[0]


def tool(action_id, name="get_outbox_stats", arguments=None) -> ActionRequest:
    return ActionRequest(action_id, "tool", name, arguments or {})


def envelope(name="get_outbox_stats") -> dict:
    end = datetime.now(timezone.utc)
    return build_envelope(name, {}, Window(end - timedelta(minutes=5), end), "COMPLETE", "test", {"n": 1}, [], [])


def run_tool(owner, app_store, claim, name="get_outbox_stats", arguments=None) -> uuid.UUID:
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id, name, arguments))
    assert evidence.start_tool(app_store, action_id) is not None
    assert evidence.commit_tool_result(app_store, action_id, envelope(name)) == "SUCCEEDED"
    return action_id


def take_over(owner, app_store, job_id) -> jobs.Claim:
    """What the supervisor does after a lease expires: back to QUEUED, then a fresh claim."""
    owner.execute("UPDATE investigations SET status = 'QUEUED', recoveries = recoveries + 1, capability_hash = NULL "
                  "WHERE id = %s", (job_id,))
    return jobs.claim_next(app_store)


def test_only_the_current_capability_is_accepted(owner, app_store):
    claim = running_job(owner, app_store)
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, "not-the-capability", tool(next_id(owner, claim.job_id)))
    assert error.value.status == 403


def test_the_runner_must_use_the_server_issued_action_id(owner, app_store):
    claim = running_job(owner, app_store)
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(uuid.uuid4()))
    assert error.value.code == "UNEXPECTED_ACTION_ID"


def test_a_repeated_action_id_replays_and_a_changed_input_conflicts(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    first = evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id))
    again = evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id))
    assert first == again and first["status"] == "PENDING"
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id, "get_request_metrics",
                                                                               {"route_group": "transfers"}))
    assert error.value.code == "ACTION_ID_REUSED"
    assert owner.execute("SELECT tool_actions FROM investigations WHERE id = %s", (claim.job_id,)).fetchone()[0] == 1


def test_a_result_becomes_numbered_evidence_and_advances_the_next_action(owner, app_store):
    """In the product (D48) the phase moves to the model decision only after the whole fixed bundle."""
    from incidentlens.bundle import FIXED_BUNDLE
    claim = running_job(owner, app_store)
    before = next_id(owner, claim.job_id)
    for index, (name, arguments) in enumerate(FIXED_BUNDLE, 1):
        run_tool(owner, app_store, claim, name, arguments)
        phase = owner.execute("SELECT next_phase FROM investigations WHERE id = %s", (claim.job_id,)).fetchone()[0]
        assert phase == ("model_decision" if index == len(FIXED_BUNDLE) else "initial_collection"), (index, phase)
    assert next_id(owner, claim.job_id) != before
    context = evidence.job_context(app_store, claim.job_id, claim.capability)
    assert [item["handle"] for item in context["evidence"]] == [f"E{i}" for i in range(1, len(FIXED_BUNDLE) + 1)]
    assert [(item["tool"], item["arguments"]) for item in context["initial_bundle"]] == FIXED_BUNDLE
    assert context["in_flight"] is None
    assert claim.capability not in str(context)


def test_only_one_action_can_be_in_flight(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id))
    owner.execute("UPDATE investigations SET next_action_id = gen_random_uuid() WHERE id = %s", (claim.job_id,))
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(next_id(owner, claim.job_id)))
    assert error.value.code == "ACTION_IN_FLIGHT"


def test_an_old_generation_cannot_append_evidence_after_a_takeover(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id))
    evidence.start_tool(app_store, action_id)
    new_claim = take_over(owner, app_store, claim.job_id)
    assert new_claim.generation == 2

    assert evidence.commit_tool_result(app_store, action_id, envelope()) == "ERROR"
    assert owner.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    with pytest.raises(ApiError) as error:  # the old runner's capability is dead
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(next_id(owner, claim.job_id)))
    assert error.value.status == 403
    deadline_before, = owner.execute("SELECT deadline FROM investigations WHERE id = %s", (claim.job_id,)).fetchone()
    assert deadline_before is not None  # recovery kept the original deadline


def test_cancelling_while_a_tool_runs_discards_its_result_and_admits_nothing_more(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id))
    evidence.start_tool(app_store, action_id)
    jobs.request_cancel(app_store, "local", claim.job_id)

    assert evidence.commit_tool_result(app_store, action_id, envelope()) == "ERROR"
    assert owner.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability,
                               ActionRequest(next_id(owner, claim.job_id), "model"))
    assert error.value.status == 403


def test_gateway_restart_marks_unfinished_actions_without_redispatching(owner, app_store):
    claim = running_job(owner, app_store)
    model_action = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(model_action, "model"))
    assert evidence.reserve_model_call(app_store, model_action, estimated_input_tokens=2_300) is not None

    assert evidence.recover_unfinished_actions(app_store) == {"UNKNOWN": 1, "INTERRUPTED": 0, "ERROR": 0}
    status, reserved = owner.execute("SELECT status, reserved_tokens FROM actions WHERE id = %s",
                                     (model_action,)).fetchone()
    assert (status, reserved) == ("UNKNOWN", 3_800)
    assert owner.execute("SELECT tokens_reserved FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone()[0] == 3_800  # the unknown charge keeps its reservation

    read = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(read))
    evidence.start_tool(app_store, read)
    assert evidence.recover_unfinished_actions(app_store)["INTERRUPTED"] == 1
    never_sent = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(never_sent, "model"))
    assert evidence.recover_unfinished_actions(app_store)["ERROR"] == 1
    assert owner.execute("SELECT model_actions, tokens_reserved FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone() == (2, 3_800)


def test_model_reservations_are_counted_and_bounded(owner, app_store):
    claim = running_job(owner, app_store)
    too_big = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(too_big, "model"))
    assert evidence.reserve_model_call(app_store, too_big, estimated_input_tokens=6_001) is None
    assert owner.execute("SELECT status, error FROM actions WHERE id = %s", (too_big,)).fetchone()[0] == "ERROR"

    owner.execute("UPDATE investigations SET tokens_reserved = 37_000 WHERE id = %s", (claim.job_id,))
    over = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(over, "model"))
    assert evidence.reserve_model_call(app_store, over, estimated_input_tokens=2_000) is None  # 37,000 + 3,500
    assert owner.execute("SELECT tokens_reserved FROM investigations WHERE id = %s",
                         (claim.job_id,)).fetchone()[0] == 37_000


def test_completed_model_calls_reconcile_to_reported_usage(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, ActionRequest(action_id, "model"))
    evidence.reserve_model_call(app_store, action_id, estimated_input_tokens=2_000)  # reserves 3,500
    evidence.commit_model_result(app_store, action_id, {"type": "call_tool", "tool": "get_delivery_timings",
                                                        "arguments": {}}, 2_000, 120, "msg_test")
    tokens, phase, decision = owner.execute(
        "SELECT tokens_reserved, next_phase, last_decision FROM investigations WHERE id = %s",
        (claim.job_id,)).fetchone()
    assert tokens == 2_120 and phase == "tool_observation"
    assert decision["tool"] == "get_delivery_timings" and decision["executed"] is False


def test_tool_budget_and_deadline_are_enforced(owner, app_store):
    claim = running_job(owner, app_store)
    owner.execute("UPDATE investigations SET tool_actions = 12 WHERE id = %s", (claim.job_id,))
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(next_id(owner, claim.job_id)))
    assert error.value.code == "TOOL_BUDGET_EXHAUSTED"
    owner.execute("UPDATE investigations SET tool_actions = 0, deadline = now() - interval '1 second' WHERE id = %s",
                  (claim.job_id,))
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(next_id(owner, claim.job_id)))
    assert error.value.code == "DEADLINE_EXCEEDED"


def test_unknown_tools_and_bad_arguments_are_rejected_before_anything_is_stored(owner, app_store):
    claim = running_job(owner, app_store)
    action_id = next_id(owner, claim.job_id)
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability, tool(action_id, "run_shell", {"cmd": "id"}))
    assert error.value.code == "UNKNOWN_TOOL"
    with pytest.raises(ApiError) as error:
        evidence.submit_action(app_store, claim.job_id, claim.capability,
                               tool(action_id, "search_delivery_logs", {"outcome": "any", "limit": 1000}))
    assert error.value.code == "INVALID_ARGUMENTS"
    assert owner.execute("SELECT count(*) FROM actions").fetchone()[0] == 0


def test_a_takeover_closes_the_old_generations_unfinished_action_and_issues_a_new_id(owner, app_store):
    """Regression (found by the database-loss process test): an action left RUNNING by a dead
    generation must not block the new runner, and its ID must not be replayed."""
    claim = running_job(owner, app_store)
    stuck = next_id(owner, claim.job_id)
    evidence.submit_action(app_store, claim.job_id, claim.capability, tool(stuck))
    evidence.start_tool(app_store, stuck)  # RUNNING when the generation died

    assert jobs.requeue_for_recovery(app_store, claim.job_id, "the runner exited") == "QUEUED"
    new_claim = jobs.claim_next(app_store)
    assert owner.execute("SELECT status FROM actions WHERE id = %s", (stuck,)).fetchone()[0] == "INTERRUPTED"
    context = evidence.job_context(app_store, claim.job_id, new_claim.capability)
    assert context["in_flight"] is None
    assert context["next_action_id"] != str(stuck)
    evidence.submit_action(app_store, claim.job_id, new_claim.capability, tool(uuid.UUID(context["next_action_id"])))
    assert evidence.commit_tool_result(app_store, stuck, envelope()) == "INTERRUPTED"  # a late result changes nothing
