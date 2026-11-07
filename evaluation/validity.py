"""Case-run validity (evaluation/protocol.md §4), decided from the frozen bundle alone, before any
system's answer is read. Returns (valid, reason)."""

from __future__ import annotations

from evaluation.cases import Template


def by_tool(bundle: list[tuple]) -> dict:
    return {tool: envelope for _, tool, _, envelope in bundle}


def failures(logs: dict) -> tuple[int, dict]:
    counts = logs["data"].get("counts_by_outcome") or {}
    return sum(v for k, v in counts.items() if k != "success"), counts


def check_validity(template: Template, bundle: list[tuple]) -> tuple[bool, str]:
    b = by_tool(bundle)
    logs, timings = b["search_delivery_logs"], b["get_delivery_timings"]
    blockers, pool = b["get_database_blockers"], b["get_pool_metrics"]
    transfers, outbox = b["get_request_metrics"], b["get_outbox_stats"]
    failed, counts = failures(logs)
    category = template.category

    def blocking() -> tuple[int, int]:
        data = blockers["data"]
        return (data.get("blocker_roles") or {}).get("other", 0), (data.get("samples") or {}).get("with_blocking", 0)

    if category in ("consumer_unavailable", "credential_rejection"):
        kind = "network_error" if category == "consumer_unavailable" else "auth_error"
        ok = logs["coverage"] == "COMPLETE" and counts.get(kind, 0) >= 5 and counts.get(kind, 0) >= 0.5 * failed
        return ok, f"logs {logs['coverage']}, {kind} {counts.get(kind, 0)} of {failed} failures"
    if category == "slow_consumer":
        handler_max = (timings["data"].get("consumer_handler_ms") or {}).get("max") or 0
        ok = (logs["coverage"] == "COMPLETE" and timings["coverage"] == "COMPLETE"
              and handler_max >= 0.8 * template.delay_ms and failed == 0)
        return ok, f"logs {logs['coverage']}, timings {timings['coverage']}, handler max {handler_max} ms, failures {failed}"
    if category == "lock_contention":
        other, samples = blocking()
        ok = blockers["coverage"] == "COMPLETE" and other >= 1 and samples >= 2
        return ok, f"blockers {blockers['coverage']}, other edges {other}, samples with blocking {samples}"
    if category == "lock_pool_exhaustion":
        other, samples = blocking()
        timeouts = pool["data"].get("acquisition_timeouts", 0)
        # Blocker samples come from the collector, which keeps sampling while LedgerCore restarts; only the
        # Prometheus sources (pool, requests) may show the restart's scrape gap, and those may be PARTIAL.
        ok = blockers["coverage"] == "COMPLETE" and other >= 1 and timeouts >= 1
        return ok, f"blockers {blockers['coverage']}, other edges {other}, pool timeouts {timeouts}"
    if category == "healthy":
        complete = all(envelope["coverage"] == "COMPLETE" for envelope in b.values())
        pending = ((outbox["data"].get("gauges") or {}).get("pending") or {}).get("max")
        other, _ = blocking()
        ok = (complete and pending is not None and pending <= 40 and failed == 0
              and (transfers["data"].get("server_error_ratio") or 0) == 0
              and pool["data"].get("acquisition_timeouts", 0) == 0 and other == 0)
        return ok, (f"all complete {complete}, pending max {pending}, failures {failed}, "
                    f"server errors {transfers['data'].get('server_error_ratio')}, pool timeouts "
                    f"{pool['data'].get('acquisition_timeouts')}, other edges {other}")
    # missing evidence: the deciding source is missing, and the symptom is visible in a source that remains
    deciding = logs if template.deciding_source == "logs" else blockers
    missing = deciding["coverage"] != "COMPLETE"
    if template.fault in ("unavailable", "slow", "reject"):
        peak = backlog(outbox)
        visible = peak is not None and peak >= STALL_BACKLOG
        symptom = f"durable backlog peak {peak}"
    else:
        ratio = transfers["data"].get("server_error_ratio") or 0
        visible = transfers["coverage"] != "UNAVAILABLE" and ratio > 0
        symptom = f"requests {transfers['coverage']}, server-error ratio {ratio}"
    return missing and visible, f"deciding source {template.deciding_source} {deciding['coverage']}; {symptom}"


STALL_BACKLOG = 40  # above the healthy pending maximum (protocol §4)


def backlog(outbox: dict) -> int | None:
    """Peak of committed arrivals minus recorded publications, accumulated over the durable buckets.
    The outbox is empty when a window starts (preflight), so this is the backlog built up inside it."""
    buckets = (outbox["data"].get("durable_counts") or {}).get("buckets")
    if not buckets:
        return None
    running = peak = 0
    for bucket in buckets:
        running += bucket["arrivals"] - bucket["publications"]
        peak = max(peak, running)
    return peak
