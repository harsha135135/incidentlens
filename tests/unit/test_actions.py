"""The action parser accepts exactly one valid action and rejects everything else."""

from incidentlens.actions import (
    DIAGNOSTIC_TOOLS,
    CallTool,
    MalformedAction,
    StopInconclusive,
    SubmitReport,
    parse_action,
    provider_tools,
)

VALID_REPORT = {
    "outcome": "TENTATIVE",
    "summary": "Recorded publication throughput was below event arrival throughput",
    "findings": [{
        "claim": "The window shows 100 arrivals/s and 7 recorded publications/s",
        "support": "observed",
        "evidence_ids": ["0612caa2-4aa6-4d0a-8ec3-9200d21d7250"],
        "reason": "Durable counts divided by the same 900-second interval",
    }],
    "alternatives": [],
    "limitations": ["No delivery timing breakdown was collected"],
    "next_checks": ["Collect correlated consumer request timing"],
}


def call(name, args):
    return [{"name": name, "args": args, "id": "toolu_1"}]


def test_one_valid_tool_call_becomes_call_tool():
    action = parse_action(call("search_delivery_logs", {"outcome": "success", "limit": 50}))
    assert action == CallTool("search_delivery_logs", {"outcome": "success", "limit": 50})


def test_report_and_stop_are_recognised():
    assert isinstance(parse_action(call("submit_report", VALID_REPORT)), SubmitReport)
    stop = parse_action(call("stop_inconclusive", {"reason": "no timings", "missing_evidence": ["timings"]}))
    assert stop == StopInconclusive("no timings", ["timings"])


def test_no_action_is_malformed():
    assert isinstance(parse_action([]), MalformedAction)


def test_two_actions_are_malformed():
    calls = call("get_outbox_stats", {}) + call("get_pool_metrics", {})
    action = parse_action(calls)
    assert isinstance(action, MalformedAction) and "exactly one" in action.problem


def test_unknown_or_unavailable_tool_is_malformed():
    assert isinstance(parse_action(call("run_shell", {"command": "ls"})), MalformedAction)
    # A real tool that is not among this job's capabilities is also refused.
    assert isinstance(parse_action(call("get_pool_metrics", {}), allowed_tools=["get_outbox_stats"]), MalformedAction)


def test_out_of_range_and_extra_arguments_are_malformed():
    assert isinstance(parse_action(call("search_delivery_logs", {"outcome": "any", "limit": 500})), MalformedAction)
    assert isinstance(parse_action(call("get_outbox_stats", {"sql": "select 1"})), MalformedAction)
    assert isinstance(parse_action(call("get_database_blockers", {"mode": "both", "limit": 5})), MalformedAction)


def test_report_limits_are_enforced_after_the_provider():
    too_many = dict(VALID_REPORT, findings=VALID_REPORT["findings"] * 9)
    assert isinstance(parse_action(call("submit_report", too_many)), MalformedAction)
    with_usage = dict(VALID_REPORT, usage={"tokens": 1})  # usage is server-supplied only
    assert isinstance(parse_action(call("submit_report", with_usage)), MalformedAction)


def test_provider_schemas_are_strict_and_cover_every_tool():
    tools = {tool["name"]: tool for tool in provider_tools()}
    assert set(tools) == set(DIAGNOSTIC_TOOLS) | {"submit_report", "stop_inconclusive"}
    for tool in tools.values():
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])


def test_every_offered_tool_is_strict_in_the_request_langchain_sends():
    """bind_tools(strict=True) is ignored for tools already in Anthropic format, so the flag must be
    on each definition. Checked on the bound request, without calling the provider."""
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model="claude-sonnet-5-5", api_key="not-used", max_retries=0)
    bound = llm.bind_tools(provider_tools(), strict=True, tool_choice="auto", parallel_tool_calls=False)
    tools = bound.kwargs["tools"]
    assert len(tools) == len(provider_tools())
    assert all(tool.get("strict") is True for tool in tools)
