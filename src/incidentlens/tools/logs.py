"""Tools over collected log records:
  search_delivery_logs   LedgerCore's delivery-attempt records, filtered by outcome
  get_delivery_timings   attempt durations, correlated with the fixture consumer's measured
                         authentication and handler times

Both answer only from records the collector stored. An empty COMPLETE result means no matching
record was written, not proof that nothing happened."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from incidentlens.tools.envelope import build_envelope, iso, parse_iso, stats, worst_coverage
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import target_config

# The one mapping from LedgerCore's delivery result to the tool's outcome categories.
#   SUCCESS                         -> success
#   HTTP 401 or 403                 -> auth_error
#   TIMEOUT, CONNECTION_FAILED      -> network_error  (rows keep the exact result, so a timeout
#                                      and a refused connection stay distinguishable)
#   HTTP 5xx                        -> server_error
#   anything else (other 4xx, ...)  -> other
CATEGORY_SQL = """CASE
    WHEN fields->>'result' = 'SUCCESS' THEN 'success'
    WHEN fields->>'httpStatus' IN ('401', '403') THEN 'auth_error'
    WHEN fields->>'result' IN ('TIMEOUT', 'CONNECTION_FAILED') THEN 'network_error'
    WHEN fields->>'httpStatus' ~ '^5[0-9][0-9]$' THEN 'server_error'
    ELSE 'other' END"""
ATTEMPTS_SQL = f"""SELECT observed_at, event_id, fields, {CATEGORY_SQL} AS category
    FROM log_records
    WHERE service = %s AND source = 'ledgercore' AND record_type = 'outbox.delivery_attempt'
      AND observed_at >= %s AND observed_at < %s"""
CATEGORIES = ["success", "auth_error", "network_error", "server_error", "other"]
CORRELATION_SLACK = timedelta(milliseconds=50)
MAX_TIMING_ROWS = 50_000


def log_coverage(context: ToolContext, source: str) -> tuple[str, list[str], dict]:
    """Whether the collector had read the source log past the window end, and whether anything
    broke the file sequence (truncation, missing file, rotation lost while stopped)."""
    window = context.window
    limitations = []
    heartbeats = context.store.execute(
        "SELECT observed_at, payload->'sources'->%s FROM telemetry_samples "
        "WHERE service = %s AND kind = 'collector_heartbeat' AND observed_at >= %s "
        "ORDER BY observed_at LIMIT 100", (source, context.service, window.end)).fetchall()
    caught_up = next((at for at, status in heartbeats
                      if status and status.get("file_id") is not None and status.get("lag_bytes") == 0), None)
    gap_rows = context.store.execute(
        "SELECT observed_at, payload->>'reason' FROM telemetry_samples "
        "WHERE service = %s AND kind = 'log_gap' AND payload->>'source' = %s AND observed_at >= %s "
        "ORDER BY observed_at", (context.service, source, window.start)).fetchall()
    details = {"caught_up_at": iso(caught_up),
               "discontinuities": [{"at": iso(at), "reason": reason} for at, reason in gap_rows]}
    coverage = "COMPLETE"
    if caught_up is None:
        coverage = "PARTIAL"
        limitations.append(f"The collector has not yet confirmed reading the {source} log past the window end; "
                           "recent records may be missing.")
    if gap_rows:
        coverage = "PARTIAL"
        reasons = ", ".join(sorted({reason for _, reason in gap_rows}))
        limitations.append(f"The {source} log had discontinuities since the window start ({reasons}); "
                           "some records may be missing.")
    ever_read = context.store.execute("SELECT 1 FROM log_files WHERE service = %s AND source = %s LIMIT 1",
                                      (context.service, source)).fetchone()
    if ever_read is None:
        coverage = "UNAVAILABLE"
        limitations.append(f"The collector has never read the {source} log.")
    return coverage, limitations, details


def delivery_record_check(context: ToolContext) -> tuple[bool, str | None, dict]:
    """Compares the delivery-attempt records stored for the window with the number of attempts
    LedgerCore's own poll-batch records say it made. Every selected event gets exactly one attempt
    record, so far fewer records than batch attempts means records were lost (for example dropped
    by the log encoder), and an empty result must not be read as "no deliveries"."""
    window = context.window
    attempts = context.store.execute(
        f"SELECT count(*) FROM ({ATTEMPTS_SQL}) attempts", (context.service, window.start, window.end)).fetchone()[0]
    selected, largest = context.store.execute(
        "SELECT coalesce(sum((fields->>'selected')::int), 0), coalesce(max((fields->>'selected')::int), 0) "
        "FROM log_records WHERE service = %s AND source = 'ledgercore' AND record_type = 'outbox.poll_batch' "
        "AND observed_at >= %s AND observed_at < %s", (context.service, window.start, window.end)).fetchone()
    details = {"attempt_records": attempts, "attempts_reported_by_poll_batches": selected}
    # A batch that starts just before the window end can log its attempts after it, so allow one batch.
    if selected > 0 and attempts < selected - largest:
        return False, (f"Delivery records look incomplete: poll batches report {selected} attempts in the window "
                       f"but only {attempts} attempt records were stored."), details
    return True, None, details


def search_delivery_logs(context: ToolContext, arguments) -> dict:
    window = context.window
    target = target_config(context.store, context.service, window)
    coverage, limitations, ingestion = log_coverage(context, "ledgercore")
    if target is None:
        coverage = worst_coverage(coverage, "PARTIAL")
        limitations.append("No LedgerCore startup record before the window end, so it is unknown whether "
                           "delivery records were being written.")
    complete, problem, check = delivery_record_check(context)
    if not complete:
        coverage = worst_coverage(coverage, "PARTIAL")
        limitations.append(problem)
    params = (context.service, window.start, window.end)
    counts = Counter({category: count for category, count in context.store.execute(
        f"SELECT category, count(*) FROM ({ATTEMPTS_SQL}) attempts GROUP BY category", params).fetchall()})
    outcome_filter = "" if arguments.outcome == "any" else "WHERE category = %s"
    filter_params = params if arguments.outcome == "any" else params + (arguments.outcome,)
    rows = context.store.execute(
        f"SELECT observed_at, event_id, fields, category FROM ({ATTEMPTS_SQL}) attempts {outcome_filter} "
        "ORDER BY observed_at, event_id LIMIT %s", filter_params + (arguments.limit,)).fetchall()
    matching = sum(counts.values()) if arguments.outcome == "any" else counts.get(arguments.outcome, 0)
    first_last = context.store.execute(
        f"SELECT min(observed_at), max(observed_at) FROM ({ATTEMPTS_SQL}) attempts", params).fetchone()
    data = {
        "outcome_filter": arguments.outcome,
        "matching_records": matching,
        "returned_records": len(rows),
        "counts_by_outcome": {category: counts.get(category, 0) for category in CATEGORIES},
        "records": [delivery_row(observed_at, event_id, fields, category)
                    for observed_at, event_id, fields, category in rows],
        "ingestion": ingestion,
        "record_check": check,
        "note": "Records are LedgerCore's own delivery-attempt log lines, earliest first.",
    }
    if matching > len(rows):
        limitations.append(f"Only the earliest {len(rows)} of {matching} matching records are listed; "
                           "counts_by_outcome covers all of them.")
    return build_envelope("search_delivery_logs", arguments.model_dump(), window, coverage, "structured_logs", data,
                          limitations, [], observed_window={"first": iso(first_last[0]), "last": iso(first_last[1])},
                          target=target)


def delivery_row(observed_at, event_id, fields, category) -> dict:
    return {
        "started_at": iso(observed_at),
        "ended_at": fields.get("endedAt"),
        "event_id": event_id,
        "attempt": fields.get("attempt"),
        "total_attempt": fields.get("totalAttempt"),
        "result": fields.get("result"),
        "outcome": category,
        "http_status": fields.get("httpStatus") or None,
        "duration_ms": fields.get("durationMs"),
        "transition_recorded": fields.get("transitionRecorded"),
    }


def get_delivery_timings(context: ToolContext, arguments) -> dict:
    window = context.window
    target = target_config(context.store, context.service, window)
    coverage, limitations, ingestion = log_coverage(context, "ledgercore")
    complete, problem, check = delivery_record_check(context)
    if not complete:
        coverage = worst_coverage(coverage, "PARTIAL")
        limitations.append(problem)
    attempts = context.store.execute(
        f"SELECT observed_at, event_id, fields, category FROM ({ATTEMPTS_SQL}) attempts "
        "ORDER BY observed_at LIMIT %s", (context.service, window.start, window.end, MAX_TIMING_ROWS)).fetchall()
    fixture = context.store.execute(
        "SELECT observed_at, event_id, fields FROM log_records WHERE service = %s AND source = 'fixture_consumer' "
        "AND record_type = 'fixture.delivery' AND observed_at >= %s AND observed_at < %s ORDER BY observed_at",
        (context.service, window.start - timedelta(seconds=5), window.end + timedelta(seconds=5))).fetchall()

    matches, unmatched_fixture = correlate(attempts, fixture)
    consumer_target = (target or {}).get("fields", {}).get("consumerTarget")
    correlated = [(attempt, record) for attempt, record in matches if record is not None]
    totals = [float(a[2].get("durationMs")) for a in attempts if a[2].get("durationMs") is not None]
    auth = [float(r[2]["auth_ms"]) for _, r in correlated if r[2].get("auth_ms") is not None]
    handler = [float(r[2]["handler_ms"]) for _, r in correlated if r[2].get("handler_ms") is not None]
    remainder = [float(a[2]["durationMs"]) - float(r[2]["auth_ms"]) - float(r[2].get("handler_ms") or 0.0)
                 for a, r in correlated if a[2].get("durationMs") is not None and r[2].get("auth_ms") is not None]
    by_result: dict[str, list[float]] = defaultdict(list)
    for attempt in attempts:
        if attempt[2].get("durationMs") is not None:
            by_result[attempt[2].get("result") or "?"].append(float(attempt[2]["durationMs"]))

    fixture_coverage, fixture_limitations, fixture_ingestion = log_coverage(context, "fixture_consumer")
    if consumer_target == "in-process":
        coverage = worst_coverage(coverage, "PARTIAL")
        limitations.append("LedgerCore delivered to its in-process consumer, which has no authentication or "
                           "handler timer: only total attempt durations are measured.")
    elif not fixture:
        coverage = worst_coverage(coverage, "PARTIAL")
        limitations.append("No consumer-side timing records in the window: authentication and handler times "
                           "are unknown.")
    else:
        coverage = worst_coverage(coverage, fixture_coverage)
        limitations += fixture_limitations
    if len(attempts) == MAX_TIMING_ROWS:
        limitations.append(f"Only the first {MAX_TIMING_ROWS} attempts in the window were analysed.")
    if attempts and len(correlated) < len(attempts) and fixture:
        limitations.append(f"{len(attempts) - len(correlated)} attempt(s) had no matching consumer record "
                           "(for example refused connections or timeouts before the consumer logged).")

    data = {
        "attempts": len(attempts),
        "correlated_attempts": len(correlated),
        "uncorrelated_attempts": len(attempts) - len(correlated),
        "unmatched_consumer_records": unmatched_fixture,
        "total_ms": stats(totals),
        "total_ms_by_result": {result: stats(values) for result, values in sorted(by_result.items())},
        "consumer_auth_ms": stats(auth),
        "consumer_handler_ms": stats(handler),
        "unattributed_ms": stats(remainder),
        "consumer_measured_by": "fixture consumer (test fixture)" if fixture else None,
        "consumer_target": consumer_target,
        "meaning": "total_ms is LedgerCore's measured attempt duration. consumer_auth_ms and consumer_handler_ms "
                   "are measured sequentially inside the consumer. unattributed_ms is the remainder (network, "
                   "connection setup, request parsing) and is not broken down further.",
        "correlation": "same event ID, consumer receipt time inside the attempt's start and end (+/- 50 ms)",
        "ingestion": {"ledgercore": ingestion, "fixture_consumer": fixture_ingestion},
        "record_check": check,
    }
    first = iso(attempts[0][0]) if attempts else None
    last = iso(attempts[-1][0]) if attempts else None
    return build_envelope("get_delivery_timings", {}, window, coverage, "structured_logs", data, limitations, [],
                          observed_window={"first": first, "last": last}, target=target)


def correlate(attempts: list, fixture: list) -> tuple[list, int]:
    """Pairs each LedgerCore attempt with the consumer record for the same event whose receipt time
    lies inside the attempt (+/- 50 ms). Retries resend identical bytes, so the event ID alone is
    ambiguous; with one sequential poller, time containment makes the pairing unique. Each consumer
    record is used at most once. Returns [(attempt, record or None)] and the unused record count."""
    by_event: dict[str, list] = defaultdict(list)
    for record in fixture:
        if record[1]:
            by_event[record[1]].append(record)
    used: set[int] = set()
    matches = []
    for attempt in attempts:
        observed_at, event_id, fields, _ = attempt
        started = observed_at
        ended = parse_iso(fields.get("endedAt")) or started
        pick = None
        for record in by_event.get(event_id, []):
            if id(record) not in used and started - CORRELATION_SLACK <= record[0] <= ended + CORRELATION_SLACK:
                pick = record
                used.add(id(record))
                break
        matches.append((attempt, pick))
    return matches, len(fixture) - len(used)
