"""Compact, model-facing summaries of saved observations (spec §13: "never silently truncate
essential source metadata to fit a prompt: select summaries with links to evidence and mark
omissions").

Each tool has a small renderer that puts the decisive numbers first, always keeps coverage, gaps
and limitations, shortens long lists explicitly, and ends with a reference to the full saved
observation. The same renderer feeds the agent (C) and the one-call baseline (B), so they see
identical evidence."""

from __future__ import annotations

import json

MAX_LIMITATIONS = 2
MAX_SAMPLE_ROWS = 4
MAX_LINE = 160


def short(text: str, limit: int = MAX_LINE) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stats_text(stats: dict | None, unit: str = "ms") -> str:
    if not stats or not stats.get("count"):
        return "no measurements"
    return f"n={stats['count']} p50={stats['p50']}{unit} p95={stats['p95']}{unit} max={stats['max']}{unit}"


def gaps_text(scrapes: dict | None) -> str:
    if not scrapes:
        return ""
    gaps = scrapes.get("gaps") or []
    text = f"scrapes {scrapes.get('successful_scrapes')}/{scrapes.get('expected_scrapes')} ok"
    if gaps:
        listed = ", ".join(f"{g['start'][11:19]}–{g['end'][11:19]} ({g['seconds']} s)" for g in gaps[:3])
        text += f"; gaps: {listed}" + (f" and {len(gaps) - 3} more" if len(gaps) > 3 else "")
        text += f" ({scrapes.get('gap_bounds', 'bounds about one scrape interval')})"
    return text


def series_text(points: list, limit: int = 12) -> str:
    """A few evenly spaced values from a gauge series."""
    if not points:
        return "none"
    step = max(1, len(points) // limit)
    picked = points[::step][:limit]
    if picked[-1] != points[-1]:
        picked.append(points[-1])
    return " ".join(f"{p[0][11:19]}={p[1]:g}" for p in picked)


def render_request_metrics(data: dict) -> list[str]:
    latency = data.get("latency_seconds") or {}
    statuses = ", ".join(f"{row['status']}×{row['count']}" for row in data.get("by_status", [])) or "none"
    lines = [f"route {data.get('route_template')}: {data.get('requests')} requests ({data.get('requests_per_second')}/s); "
             f"status {statuses}; server_error_ratio {data.get('server_error_ratio')}",
             f"latency p50 {latency.get('p50')} s, p95 {latency.get('p95')} s, p99 {latency.get('p99')} s; "
             f"counter restarts {data.get('counter_resets')}"]
    if data.get("scrapes"):
        lines.append(gaps_text(data["scrapes"]))
    return lines


def render_outbox(data: dict) -> list[str]:
    lines = []
    counts = data.get("durable_counts")
    if counts:
        lines.append(f"committed arrivals {counts['committed_arrivals']} ({counts['arrivals_per_second']}/s); "
                     f"recorded publications {counts['recorded_publications']} ({counts['publications_per_second']}/s); "
                     f"arrivals minus publications {counts['committed_arrivals'] - counts['recorded_publications']}; "
                     f"over {counts['window_seconds']} s; settled {counts['settled']}")
        buckets = counts.get("buckets") or []
        size = counts.get("bucket_seconds")
        lines.append(f"per {size} s bucket, arrivals: " + " ".join(str(b["arrivals"]) for b in buckets))
        lines.append(f"per {size} s bucket, publications: " + " ".join(str(b["publications"]) for b in buckets))
    for name, gauge in (data.get("gauges") or {}).items():
        if gauge.get("samples"):
            lines.append(f"{name}: first {gauge['first']['value']:g} at {gauge['first']['at'][11:19]}, "
                         f"last {gauge['last']['value']:g} at {gauge['last']['at'][11:19]}, "
                         f"min {gauge['min']:g}, max {gauge['max']:g}; samples: {series_text(gauge.get('points', []), 4)}")
        else:
            lines.append(f"{name}: no samples")
    if data.get("scrapes"):
        lines.append(gaps_text(data["scrapes"]))
    return lines


def render_delivery_logs(data: dict) -> list[str]:
    counts = data.get("counts_by_outcome") or {}
    lines = [f"filter {data.get('outcome_filter')}: {data.get('matching_records')} matching records; by outcome: "
             + ", ".join(f"{k} {v}" for k, v in counts.items())]
    records = data.get("records") or []
    if records:
        lines.append(f"first {records[0]['started_at'][11:23]}, last listed {records[-1]['started_at'][11:23]}")
        sample = records[:MAX_SAMPLE_ROWS // 2] + (records[-(MAX_SAMPLE_ROWS // 2):] if len(records) > MAX_SAMPLE_ROWS else [])
        for row in sample:
            lines.append(f"  {row['started_at'][11:23]} attempt {row.get('attempt')} {row.get('result')} "
                         f"status {row.get('http_status')} {row.get('duration_ms')} ms")
        if len(records) > MAX_SAMPLE_ROWS:
            lines.append(f"  ({len(records) - MAX_SAMPLE_ROWS} more listed rows omitted here)")
    check = data.get("record_check")
    if check:
        lines.append(f"record check: {check['attempt_records']} attempt records vs "
                     f"{check['attempts_reported_by_poll_batches']} attempts reported by poll batches")
    return lines


def render_delivery_timings(data: dict) -> list[str]:
    lines = [f"attempts {data.get('attempts')}, correlated with consumer records {data.get('correlated_attempts')}",
             f"total (LedgerCore-measured) {stats_text(data.get('total_ms'))}"]
    for result, stats in (data.get("total_ms_by_result") or {}).items():
        lines.append(f"  {result}: {stats_text(stats)}")
    if data.get("consumer_measured_by"):
        lines.append(f"consumer auth {stats_text(data.get('consumer_auth_ms'))}; consumer handler "
                     f"{stats_text(data.get('consumer_handler_ms'))}; unattributed {stats_text(data.get('unattributed_ms'))} "
                     f"(measured by {data['consumer_measured_by']})")
    else:
        lines.append(f"consumer-side timing: not measured (consumer target {data.get('consumer_target')})")
    return lines


def render_pool(data: dict) -> list[str]:
    gauges = data.get("gauges") or {}

    def gauge(name: str) -> str:
        g = gauges.get(name) or {}
        if not g.get("samples"):
            return f"{name}: no samples"
        extra = (f", above zero in {g['samples_above_zero']} of {g['samples']} scrapes "
                 f"({g['first_above_zero_at'][11:19]}–{g['last_above_zero_at'][11:19]})") if g.get("samples_above_zero") else ""
        return f"{name}: max {g['max']:g}, mean {g['mean']:g}, last {g['last']:g}{extra}"

    lines = [f"acquisition timeouts {data.get('acquisition_timeouts')}; acquisitions {data.get('acquisitions')}; "
             f"acquire mean {data.get('acquire_mean_ms')} ms, max per scrape {data.get('acquire_max_ms_per_scrape')} ms; "
             f"configured maximum {data.get('configured_maximum_at_window_end')}, connection timeout "
             f"{data.get('configured_connection_timeout_ms')} ms",
             "; ".join(gauge(name) for name in ("pending", "active", "maximum"))]
    if data.get("scrapes"):
        lines.append(gaps_text(data["scrapes"]))
    return lines


def render_blockers(data: dict) -> list[str]:
    if data.get("mode") == "current":
        lines = [f"CURRENT state at {str(data.get('queried_at'))[11:19]}"
                 f"{' (outside the requested window)' if data.get('outside_requested_window') else ''}: "
                 f"{data.get('relationships')} blocking relationships"]
    else:
        samples = data.get("samples") or {}
        gaps = samples.get("gaps") or []
        lines = [f"historical samples: {samples.get('successful')}/{samples.get('expected')} ok, {samples.get('failed')} failed, "
                 f"{samples.get('with_blocking')} with blocking"
                 + (f"; sampling gaps {', '.join(g['start'][11:19] + '–' + g['end'][11:19] for g in gaps[:3])}" if gaps else ""),
                 f"{data.get('relationships')} relationships, {data.get('blocked_sessions')} blocked ledger_app sessions, "
                 f"blocker roles {data.get('blocker_roles')}, max wait {data.get('max_blocked_wait_seconds')} s"
                 + (f", from {data['first_blocking_at'][11:19]} to {data['last_blocking_at'][11:19]}"
                    if data.get("first_blocking_at") else "")]
    for edge in (data.get("edges") or [])[:3]:
        lines.append(f"  blocked {edge.get('blocked_pid')} waits on {edge.get('blocker_role')} session "
                     f"{edge.get('blocker_pid')} ({', '.join(edge.get('blocker_states') or [edge.get('blocker_state') or '?'])})"
                     + (f", seen in {edge['samples']} samples" if edge.get("samples") else ""))
    return lines


def render_runtime_config(data: dict) -> list[str]:
    lines = []
    keys = ("poolMaximumSize", "poolConnectionTimeoutMs", "outboxPollIntervalMs", "outboxBatchSize",
            "outboxDeliveryTimeoutMs", "outboxMaxAttempts", "consumerTarget", "revision")
    for version in data.get("versions") or []:
        settings = version["settings"]
        lines.append(f"from {version['effective_at'][11:19]}: " + ", ".join(f"{k}={settings.get(k)}" for k in keys if k in settings))
    if data.get("restarts_in_window"):
        lines.append("restarts inside the window at " + ", ".join(t[11:19] for t in data["restarts_in_window"]))
    return lines


RENDERERS = {
    "get_request_metrics": render_request_metrics,
    "get_outbox_stats": render_outbox,
    "search_delivery_logs": render_delivery_logs,
    "get_delivery_timings": render_delivery_timings,
    "get_pool_metrics": render_pool,
    "get_database_blockers": render_blockers,
    "get_runtime_config": render_runtime_config,
}


def render_observation(handle: str, tool: str, arguments: dict, envelope: dict) -> str:
    """One observation for the model: header, decisive numbers, limitations, link to the full record."""
    header = (f"[{handle}] {tool}{' ' + json.dumps(arguments, separators=(',', ':')) if arguments else ''} "
              f"coverage {envelope.get('coverage')}{' TRUNCATED' if envelope.get('truncated') else ''}")
    data = envelope.get("data") or {}
    renderer = RENDERERS.get(tool)
    if envelope.get("coverage") == "UNAVAILABLE" and not data:
        body = ["no data: the source was unavailable"]
    elif renderer:
        body = renderer(data)
    else:
        body = [short(json.dumps(data, separators=(",", ":")), 1200)]
    limitations = envelope.get("limitations") or []
    body += [f"limitation: {short(note)}" for note in limitations[:MAX_LIMITATIONS]]
    if len(limitations) > MAX_LIMITATIONS:
        body.append(f"({len(limitations) - MAX_LIMITATIONS} more limitations in the saved observation)")
    body += [f"error: {short(error)}" for error in (envelope.get("errors") or [])[:2]]
    return "\n".join([header] + [f"  {line}" for line in body if line] + [f"  (full observation saved as {handle})"])


def render_compact(handle: str, tool: str, arguments: dict, envelope: dict) -> str:
    """One line: the header and the decisive numbers, with an explicit omission marker."""
    full = render_observation(handle, tool, arguments, envelope).splitlines()
    first = full[1].strip() if len(full) > 2 else ""
    return f"{full[0]}\n  {short(first, 200)}\n  (shortened in this prompt; full observation saved as {handle})"


def render_minimal(handle: str, tool: str, arguments: dict, envelope: dict) -> str:
    return f"[{handle}] {tool} coverage {envelope.get('coverage')} (omitted from this prompt to fit; saved as {handle})"


def evidence_section(observations: list[tuple[str, str, dict, dict]], max_chars: int) -> str:
    """Renders observations [(handle, tool, arguments, envelope)] within max_chars. Recent
    observations keep their full render; the oldest are shortened first, then reduced to a header,
    always with an explicit marker. Nothing is dropped silently (spec §13)."""
    levels = [[render_observation(*item), render_compact(*item), render_minimal(*item)] for item in observations]
    chosen = [0] * len(levels)
    for level in (1, 2):
        for index in range(len(levels)):  # oldest first
            if sum(len(levels[i][chosen[i]]) for i in range(len(levels))) <= max_chars:
                break
            chosen[index] = level
    return "\n".join(levels[i][chosen[i]] for i in range(len(levels)))
