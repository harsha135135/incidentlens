"""get_database_blockers: lock-wait relationships where a LedgerCore (ledger_app) session waits.

Two explicit modes (spec §6). A historical query never substitutes current state:
  historical  the collector's samples (every 2 s) inside the job window, aggregated into edges,
              with sampling gaps and failed samples reported
  current     one live read of diagnostics.blocking_sessions() now, labelled as outside the
              incident window; refused when the job is answered from a frozen bundle (D6)

Two-second sampling can miss shorter blocks, and no samples is not proof of no contention (§5)."""

from __future__ import annotations

from datetime import timedelta

from incidentlens.tools.envelope import Deadline, Window, build_envelope, iso, utc_now
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import SourceError, target_config, target_query

TOOL = "get_database_blockers"
SAMPLE_INTERVAL_SECONDS = 2.0
GAP_SECONDS = 5.0  # more than two missed samples
SAMPLING_NOTE = ("Blocking is sampled every 2 s: shorter blocks can be missed, and no blocking in the samples "
                 "is not proof that no session waited.")


def get_database_blockers(context: ToolContext, arguments) -> dict:
    if arguments.mode == "current":
        return current_blockers(context, arguments)
    return historical_blockers(context, arguments)


def historical_blockers(context: ToolContext, arguments) -> dict:
    window = context.window
    target = target_config(context.store, context.service, window)
    rows = context.store.execute(
        "SELECT observed_at, coverage, payload FROM telemetry_samples WHERE service = %s AND kind = 'db_blockers' "
        "AND observed_at >= %s AND observed_at < %s ORDER BY observed_at", (context.service, window.start, window.end)
    ).fetchall()
    good = [(at, payload) for at, coverage, payload in rows if coverage == "COMPLETE"]
    failed = [(at, payload) for at, coverage, payload in rows if coverage != "COMPLETE"]

    gaps, previous = [], window.start
    for moment in [at for at, _ in good] + [window.end]:
        if (moment - previous).total_seconds() > GAP_SECONDS:
            gaps.append({"start": iso(previous), "end": iso(moment), "seconds": round((moment - previous).total_seconds(), 1)})
        previous = moment

    edges: dict[tuple, dict] = {}
    blocked_samples = 0
    truncated_samples = 0
    for at, payload in good:
        if payload.get("edge_count"):
            blocked_samples += 1
        truncated_samples += bool(payload.get("truncated"))
        for edge in payload.get("edges", []):
            key = (edge["blocker_pid"], edge["blocked_pid"])
            item = edges.setdefault(key, {
                "blocked_pid": edge["blocked_pid"], "blocker_pid": edge["blocker_pid"],
                "blocker_role": edge["blocker_role"], "first_seen": iso(at), "last_seen": iso(at), "samples": 0,
                "max_blocked_wait_seconds": None, "blocker_states": set(), "blocked_wait_event": edge.get("blocked_wait_event"),
                "max_blocker_transaction_seconds": None})
            item["last_seen"] = iso(at)
            item["samples"] += 1
            item["blocker_states"].add(edge.get("blocker_state") or "unknown")
            for field, value in (("max_blocked_wait_seconds", edge.get("blocked_wait_seconds")),
                                 ("max_blocker_transaction_seconds", edge.get("blocker_transaction_seconds"))):
                if value is not None and (item[field] is None or value > item[field]):
                    item[field] = value
    listed = sorted(edges.values(), key=lambda item: item["first_seen"])
    for item in listed:
        item["blocker_states"] = sorted(item["blocker_states"])
    roles: dict[str, int] = {}
    for item in listed:
        roles[item["blocker_role"]] = roles.get(item["blocker_role"], 0) + 1

    limitations = [SAMPLING_NOTE]
    if not good:
        coverage = "UNAVAILABLE"
        limitations.append("No successful blocker samples exist for the window.")
    elif gaps or failed:
        coverage = "PARTIAL"
        seconds = sum(gap["seconds"] for gap in gaps)
        limitations.append(f"Blocker sampling is missing for {seconds:.0f} s ({len(gaps)} gap(s)) and "
                           f"{len(failed)} sample(s) failed; blocking there is unknown.")
    else:
        coverage = "COMPLETE"
    if len(listed) > arguments.limit:
        limitations.append(f"Only the first {arguments.limit} of {len(listed)} blocking relationships are listed.")
    if truncated_samples:
        limitations.append(f"{truncated_samples} sample(s) had more than 200 relationships and were cut by the collector.")
    data = {
        "mode": "historical",
        "samples": {"expected": round(window.seconds / SAMPLE_INTERVAL_SECONDS), "successful": len(good),
                    "failed": len(failed), "with_blocking": blocked_samples, "gaps": gaps},
        "relationships": len(listed),
        "blocked_sessions": len({item["blocked_pid"] for item in listed}),
        "blocker_roles": roles,
        "max_blocked_wait_seconds": max((item["max_blocked_wait_seconds"] or 0 for item in listed), default=None),
        "first_blocking_at": listed[0]["first_seen"] if listed else None,
        "last_blocking_at": max((item["last_seen"] for item in listed), default=None),
        "edges": listed[: arguments.limit],
        "meaning": "Each edge: a waiting ledger_app session (blocked_pid) and the session holding the lock "
                   "(blocker_pid), labelled only ledger_app or other. No query text is collected.",
    }
    observed = {"first": iso(good[0][0]) if good else None, "last": iso(good[-1][0]) if good else None}
    return build_envelope(TOOL, arguments.model_dump(), window, coverage, "postgres_samples", data, limitations, [],
                          observed_window=observed, target=target)


def current_blockers(context: ToolContext, arguments) -> dict:
    window = context.window
    target = target_config(context.store, context.service, window)
    if getattr(context, "frozen", False):
        return build_envelope(TOOL, arguments.model_dump(), window, "UNAVAILABLE", "none", {"mode": "current"},
                              ["Current blocking is not available when answering from a frozen evidence bundle."],
                              ["current mode disabled in bundle mode"], target=target)
    now = utc_now()
    try:
        rows = target_query(context.settings.target_conninfo,
                            "SELECT observed_at, blocked_pid, blocked_wait_event_type, blocked_wait_event, "
                            "blocked_lock_wait_started, blocker_pid, blocker_role, blocker_state "
                            "FROM diagnostics.blocking_sessions() LIMIT %s", (arguments.limit + 1,), Deadline())
    except SourceError as error:
        return build_envelope(TOOL, arguments.model_dump(), window, "UNAVAILABLE", "postgres_live",
                              {"mode": "current"}, ["The target database could not be read now."], [str(error)],
                              target=target)
    edges = [{"observed_at": iso(observed), "blocked_pid": blocked, "blocked_wait_event_type": wait_type,
              "blocked_wait_event": wait_event,
              "blocked_wait_seconds": None if started is None else round((observed - started).total_seconds(), 3),
              "blocker_pid": blocker, "blocker_role": role, "blocker_state": state}
             for observed, blocked, wait_type, wait_event, started, blocker, role, state in rows[: arguments.limit]]
    outside = now >= window.end + timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    limitations = ["This is the state at the time of the query" + (", after the requested window ended: it is "
                                                                    "follow-up evidence, not evidence of the incident window." if outside else ".")]
    if len(rows) > arguments.limit:
        limitations.append(f"Only the first {arguments.limit} relationships are listed.")
    data = {"mode": "current", "queried_at": iso(now), "outside_requested_window": outside,
            "relationships": len(edges), "edges": edges}
    return build_envelope(TOOL, arguments.model_dump(), window, "COMPLETE", "postgres_live", data, limitations, [],
                          observed_window={"first": iso(now), "last": iso(now)}, target=target)
