"""Baselines (spec §16). Both share the agent's service access, time window, limits and evidence
renderer, and use one fixed collection bundle defined here, once:

  A  deterministic diagnostic rules over the bundle (thresholds fixed below)
  B  one model call over the bundle, offered only submit_report and stop_inconclusive
  C  the adaptive agent (the IncidentLens job itself), which starts from the same first two tools

Also here: the pilot's provisional primary-cause classifier for free-text reports. It is a reading
aid only; Stage 5 uses the blinded manual review with a written rubric (spec §16)."""

from __future__ import annotations

import time

import psycopg
from langchain_core.messages import HumanMessage, SystemMessage

from incidentlens.actions import MalformedAction, StopInconclusive, SubmitReport, parse_action, provider_tools
from incidentlens.config import Settings
from incidentlens.evidence_render import evidence_section
from incidentlens.model_gateway import EVIDENCE_PROMPT_CHARS, SYSTEM_PROMPT, THINKING, chat_model, model_settings
from incidentlens.tools.envelope import Window
from incidentlens.tools.registry import live_context, run_tool

# The Stage 3 pilot used the first four tools; from the extended pilot on (D44) and in the product
# (D48), the bundle is every implemented tool, defined once in incidentlens/bundle.py.
from incidentlens.bundle import FIXED_BUNDLE  # noqa: E402

PILOT_BUNDLE = FIXED_BUNDLE[:4]

# Baseline A thresholds (fixed before the pilot).
MIN_FAILURES = 5
THROUGHPUT_SHORTFALL = 0.85  # publications below 85 % of arrivals
PENDING_GROWTH = 20
SLOW_COMPONENT_MS = 200
HEALTHY_PENDING_MAX = 40  # two delivery batches


def collect_bundle(window: Window, store: psycopg.Connection, settings: Settings,
                   tools: list[tuple] | None = None) -> list[tuple]:
    context = live_context(window, store, settings)
    return [(f"E{i + 1}", tool, arguments, run_tool(tool, arguments, context))
            for i, (tool, arguments) in enumerate(tools or FIXED_BUNDLE)]


def by_tool(bundle: list[tuple]) -> dict:
    return {tool: (handle, envelope) for handle, tool, _, envelope in bundle}


def baseline_a(bundle: list[tuple]) -> dict:
    """Fixed rules. Returns outcome, primary_cause, the rule that fired and the handles it used."""
    tools = by_tool(bundle)
    logs_handle, logs = tools["search_delivery_logs"]
    outbox_handle, outbox = tools["get_outbox_stats"]
    timing_handle, timings = tools["get_delivery_timings"]
    counts = logs["data"].get("counts_by_outcome") or {}
    failures = sum(count for name, count in counts.items() if name != "success")

    def result(outcome, cause, rule, handles):
        return {"outcome": outcome, "primary_cause": cause, "rule": rule, "evidence_ids": handles}

    if logs["coverage"] == "UNAVAILABLE" or outbox["coverage"] == "UNAVAILABLE":
        return result("INCONCLUSIVE", "insufficient_evidence", "delivery logs or outbox statistics unavailable",
                      [logs_handle, outbox_handle])
    for category, cause in (("network_error", "consumer_unavailable"), ("auth_error", "consumer_credential_rejected"),
                            ("server_error", "consumer_server_error")):
        if failures >= MIN_FAILURES and counts.get(category, 0) >= 0.5 * failures:
            return result("SUPPORTED", cause, f"{counts[category]} of {failures} failed deliveries were {category}",
                          [logs_handle])
    # Frozen before the extended pilot (D45): blocking first, then pool exhaustion without blocking.
    blockers = tools.get("get_database_blockers")
    pool = tools.get("get_pool_metrics")
    pool_timeouts = pool[1]["data"].get("acquisition_timeouts", 0) if pool else 0
    pool_pending = ((pool[1]["data"].get("gauges") or {}).get("pending") or {}).get("samples_above_zero") or 0 if pool else 0
    if blockers:
        block = blockers[1]["data"]
        if (block.get("blocker_roles") or {}).get("other", 0) >= 1 and (block.get("samples") or {}).get("with_blocking", 0) >= 2:
            note = f"; {pool_timeouts} pool acquisition timeouts" if pool_timeouts else ""
            return result("SUPPORTED", "lock_contention",
                          f"a non-ledger_app session blocked ledger_app sessions in {block['samples']['with_blocking']} samples{note}",
                          [blockers[0]] + ([pool[0]] if pool_timeouts else []))
    if pool and (pool_timeouts >= 1 or pool_pending >= 2):
        return result("SUPPORTED", "pool_saturation", f"{pool_timeouts} acquisition timeouts, pending in {pool_pending} scrapes",
                      [pool[0]])
    durable = outbox["data"].get("durable_counts") or {}
    pending = (outbox["data"].get("gauges") or {}).get("pending") or {}
    grew = pending.get("samples") and pending["last"]["value"] - pending["first"]["value"] >= PENDING_GROWTH
    if durable and durable["publications_per_second"] < THROUGHPUT_SHORTFALL * durable["arrivals_per_second"] and grew:
        handler = (timings["data"].get("consumer_handler_ms") or {}).get("p50")
        auth = (timings["data"].get("consumer_auth_ms") or {}).get("p50")
        if handler is not None and handler >= SLOW_COMPONENT_MS:
            return result("SUPPORTED", "slow_consumer_handler", f"throughput shortfall; handler p50 {handler} ms",
                          [outbox_handle, timing_handle])
        if auth is not None and auth >= SLOW_COMPONENT_MS:
            return result("SUPPORTED", "slow_consumer_authentication", f"throughput shortfall; auth p50 {auth} ms",
                          [outbox_handle, timing_handle])
        return result("TENTATIVE", "publisher_throughput", "throughput shortfall without a slow consumer component",
                      [outbox_handle])
    transfers = tools.get("get_request_metrics")
    server_errors = (transfers[1]["data"].get("server_error_ratio") or 0) if transfers else 0
    if failures == 0 and pending.get("samples") and pending["max"] <= HEALTHY_PENDING_MAX and not server_errors \
            and not pool_timeouts:
        return result("NO_FAULT_OBSERVED", "none", "no failed deliveries and pending stayed low", [logs_handle, outbox_handle])
    return result("INCONCLUSIVE", "unclassified", "no rule matched", [])


def baseline_b(bundle: list[tuple], symptom: str, window: Window) -> dict:
    """One model call over the fixed bundle, rendered exactly as the agent sees evidence."""
    prompt = (f"service: ledgercore-local\nsymptom (from the developer): {symptom}\n"
              f"window: {window.start.isoformat()} to {window.end.isoformat()}\n\nSaved observations:\n"
              + evidence_section(list(bundle), EVIDENCE_PROMPT_CHARS)
              + "\n\nNo further diagnostic calls are possible: submit your report now, or stop as inconclusive.")
    tools = provider_tools(available=[])
    llm = chat_model(model_settings())
    started = time.perf_counter()
    reply = llm.bind_tools(tools, strict=True, tool_choice="auto", parallel_tool_calls=False).invoke(
        [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)])
    action = parse_action(reply.tool_calls, allowed_tools=[])
    usage, meta = reply.usage_metadata or {}, reply.response_metadata or {}
    handles = {handle for handle, *_ in bundle}
    report = action.report.model_dump() if isinstance(action, SubmitReport) else None
    if isinstance(action, StopInconclusive):
        report = {"outcome": "INCONCLUSIVE", "summary": action.reason, "findings": [], "alternatives": [],
                  "limitations": [], "next_checks": action.missing_evidence}
    unknown = sorted({ref for f in (report or {}).get("findings", []) for ref in f["evidence_ids"] if ref not in handles})
    return {"report": report, "malformed": action.problem if isinstance(action, MalformedAction) else None,
            "unknown_citations": unknown, "stop_reason": meta.get("stop_reason"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "model_calls": 1, "tool_calls": len(bundle), "latency_seconds": round(time.perf_counter() - started, 2)}


# ---- Provisional primary-cause classifier for the pilot (a reading aid, not the evaluation) ----

CAUSE_KEYWORDS = {
    "consumer_credential_rejected": ("401", "unauthorized", "credential", "authentication fail", "auth_error",
                                     "auth failure", "rejected the publisher", "rejects the publisher"),
    "lock_contention": ("lock", "blocked", "blocker", "contention"),
    "pool_saturation": ("connection pool", "pool exhaust", "pool saturat", "acquisition timeout", "hikari"),
    "consumer_unavailable": ("unavailable", "refused", "connection_failed", "connection failed", "unreachable",
                             "not reachable", "down", "outage", "rejecting connections"),
    "slow_consumer_handler": ("handler", "slow consumer", "consumer processing", "consumer-side latency",
                              "consumer latency", "slow downstream", "processing time"),
}


def classify_report(report: dict | None) -> str:
    if not report:
        return "no_report"
    if report.get("outcome") == "NO_FAULT_OBSERVED":
        return "none"
    if report.get("outcome") == "INCONCLUSIVE" and not report.get("findings"):
        return "inconclusive"
    findings = report.get("findings") or []
    text = " ".join([report.get("summary", "")] + [f.get("claim", "") for f in findings[:2]]).lower()
    scores = {cause: sum(word in text for word in words) for cause, words in CAUSE_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"
