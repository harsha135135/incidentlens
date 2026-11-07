"""get_request_metrics: request counts, status mix and latency percentiles for one fixed route,
computed from the raw Prometheus samples of LedgerCore's http_server_requests histogram."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from incidentlens.config import SCRAPE_INTERVAL_SECONDS
from incidentlens.tools.envelope import (
    Deadline,
    Window,
    bucket_quantile,
    build_envelope,
    counter_increase,
    iso,
    scrape_gaps,
)
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import PrometheusClient, Series, SourceError, target_config

TOOL = "get_request_metrics"
# The only routes the tool can query: fixed templates, never model input.
ROUTE_TEMPLATES = {"transfers": "/v1/transfers", "consumer": "/demo-consumer/events"}


def get_request_metrics(context: ToolContext, arguments) -> dict:
    deadline = Deadline()
    window = context.window
    uri = ROUTE_TEMPLATES[arguments.route_group]
    target = target_config(context.store, context.service, window)
    try:
        scrape = scrape_coverage(context.prometheus, window, deadline)
        counts = context.prometheus.raw_samples(
            f'http_server_requests_seconds_count{{job="ledgercore",uri="{uri}"}}', window, deadline, True)
        buckets = context.prometheus.raw_samples(
            f'http_server_requests_seconds_bucket{{job="ledgercore",uri="{uri}"}}', window, deadline, True)
    except SourceError as error:
        return build_envelope(TOOL, arguments.model_dump(), window, "UNAVAILABLE", "prometheus", {},
                              ["Prometheus could not be queried, so request metrics are unknown."], [str(error)],
                              target=target)

    limitations = list(scrape["limitations"])
    by_status: dict[tuple[str, str], float] = defaultdict(float)
    resets = 0
    for series in counts:
        increase, series_resets = series_increase(series, window, scrape["first_scrape"])
        by_status[(series.labels.get("status", "?"), series.labels.get("outcome", "?"))] += increase
        resets += series_resets
    total = sum(by_status.values())

    by_bound: dict[float, float] = defaultdict(float)
    for series in buckets:
        increase, _ = series_increase(series, window, scrape["first_scrape"])
        by_bound[float(series.labels["le"])] += increase
    bucket_list = sorted(by_bound.items())

    if total == 0:
        limitations.append(f"No requests to {uri} were recorded in the window; percentiles are unavailable.")
    if resets:
        limitations.append(f"Counters restarted {resets} time(s) in the window (application restart); "
                           "counts include requests on both sides of each restart.")
    if arguments.route_group == "consumer" and target and target["fields"].get("consumerTarget") != "in-process":
        limitations.append("LedgerCore delivers to an external consumer in this window, so its in-process "
                           "consumer route is not expected to receive requests. Use get_delivery_timings.")

    server_errors = sum(count for (status, _), count in by_status.items() if status.startswith("5"))
    data = {
        "route_group": arguments.route_group,
        "route_template": uri,
        "requests": int(round(total)),
        "requests_per_second": round(total / window.seconds, 3),
        "server_error_ratio": round(server_errors / total, 4) if total else None,
        "by_status": [{"status": status, "outcome": outcome, "count": int(round(count))}
                      for (status, outcome), count in sorted(by_status.items())],
        "latency_seconds": {
            "p50": rounded(bucket_quantile(0.50, bucket_list)),
            "p95": rounded(bucket_quantile(0.95, bucket_list)),
            "p99": rounded(bucket_quantile(0.99, bucket_list)),
            "method": "interpolated within fixed histogram buckets summed across series",
        },
        "bucket_counts": [{"le": "+Inf" if bound == float("inf") else bound, "count": int(round(count))}
                          for bound, count in bucket_list],
        "counter_resets": resets,
        "scrapes": scrape["summary"],
        "counting_method": "counter increase between scrapes, from the last scrape before the window start "
                           "to the last scrape inside it (not extrapolated)",
    }
    return build_envelope(TOOL, arguments.model_dump(), window, scrape["coverage"], "prometheus", data, limitations,
                          [], observed_window=scrape["observed_window"], target=target)


def scrape_coverage(prometheus: PrometheusClient, window: Window, deadline: Deadline) -> dict:
    """Coverage from LedgerCore's `up` series: which parts of the window were actually scraped."""
    series = prometheus.raw_samples('up{job="ledgercore"}', window, deadline, include_previous_scrape=True)
    times = [t for s in series for t in s.times]
    values = [v for s in series for v in s.values]
    inside = [(t, v) for t, v in sorted(zip(times, values)) if t >= window.start]
    summary = scrape_gaps([t for t, _ in inside], [v for _, v in inside], window, SCRAPE_INTERVAL_SECONDS)
    # Successful scrapes: the last one before the window (baseline) and those inside it.
    good, _ = baseline_and_inside(sorted(t for t, v in zip(times, values) if v == 1), [], window)
    limitations = []
    if not any(v == 1 for _, v in inside):
        coverage = "UNAVAILABLE"
        limitations.append("Prometheus has no successful scrape of LedgerCore in the window.")
    elif summary["gaps"] or summary["failed_scrapes"]:
        coverage = "PARTIAL"
        seconds = sum(gap["seconds"] for gap in summary["gaps"])
        limitations.append(f"Scrapes are missing for {seconds:.0f} s of the window "
                           f"({len(summary['gaps'])} gap(s)); values there are unknown, not zero.")
    else:
        coverage = "COMPLETE"
    if not good or good[0] >= window.start:
        limitations.append("No successful scrape just before the window start, so requests in its first "
                           "seconds may be missing from the counts.")
    return {
        "coverage": coverage,
        "summary": summary,
        "limitations": limitations,
        "first_scrape": good[0] if good else None,
        "observed_window": {"first": iso(inside[0][0]) if inside else None,
                            "last": iso(inside[-1][0]) if inside else None},
    }


def series_increase(series: Series, window: Window, first_scrape) -> tuple[float, int]:
    """Increase of one counter series over the window. A series first seen after scraping was
    already working was created inside the window (Micrometer creates meters on first use), so
    it counts from zero; a series present from the first scrape uses that scrape as its baseline."""
    times, values = baseline_and_inside(series.times, series.values, window)
    if not times:
        return 0.0, 0
    newly_created = first_scrape is not None and times[0] > first_scrape + timedelta(
        seconds=SCRAPE_INTERVAL_SECONDS * 1.5)
    if newly_created:
        values = [0.0] + values
    return counter_increase(values)


def baseline_and_inside(times: list, values: list, window: Window) -> tuple[list, list]:
    """Keeps the last sample before the window start (the baseline) and every sample inside it."""
    before = [i for i, t in enumerate(times) if t < window.start]
    keep = ([before[-1]] if before else []) + [i for i, t in enumerate(times) if window.start <= t < window.end]
    return [times[i] for i in keep], [values[i] for i in keep] if values else []


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


POOL_GAUGES = {"active": "hikaricp_connections_active", "idle": "hikaricp_connections_idle",
               "maximum": "hikaricp_connections_max", "pending": "hikaricp_connections_pending"}


def get_pool_metrics(context: ToolContext, arguments) -> dict:
    """LedgerCore's connection pool: active, idle, maximum and pending borrowers (gauges sampled at
    each scrape), acquisition timeouts and acquisition time (counters over the window)."""
    deadline = Deadline()
    window = context.window
    target = target_config(context.store, context.service, window)
    try:
        scrape = scrape_coverage(context.prometheus, window, deadline)
        gauges = {name: context.prometheus.raw_samples(f'{metric}{{job="ledgercore"}}', window, deadline)
                  for name, metric in POOL_GAUGES.items()}
        timeouts = context.prometheus.raw_samples('hikaricp_connections_timeout_total{job="ledgercore"}', window,
                                                  deadline, True)
        acquire_count = context.prometheus.raw_samples('hikaricp_connections_acquire_seconds_count{job="ledgercore"}',
                                                       window, deadline, True)
        acquire_sum = context.prometheus.raw_samples('hikaricp_connections_acquire_seconds_sum{job="ledgercore"}',
                                                     window, deadline, True)
        acquire_max = context.prometheus.raw_samples('hikaricp_connections_acquire_seconds_max{job="ledgercore"}',
                                                     window, deadline)
    except SourceError as error:
        return build_envelope("get_pool_metrics", {}, window, "UNAVAILABLE", "prometheus", {},
                              ["Prometheus could not be queried, so the pool state is unknown."], [str(error)],
                              target=target)

    def summary(series_list) -> dict:
        points = sorted((t, v) for s in series_list for t, v in zip(s.times, s.values) if t >= window.start)
        if not points:
            return {"samples": 0}
        values = [v for _, v in points]
        nonzero = [(t, v) for t, v in points if v > 0]
        return {"samples": len(points), "first": values[0], "last": values[-1], "min": min(values), "max": max(values),
                "mean": round(sum(values) / len(values), 3), "samples_above_zero": len(nonzero),
                "first_above_zero_at": iso(nonzero[0][0]) if nonzero else None,
                "last_above_zero_at": iso(nonzero[-1][0]) if nonzero else None}

    timeout_total = sum(series_increase(s, window, scrape["first_scrape"])[0] for s in timeouts)
    count = sum(series_increase(s, window, scrape["first_scrape"])[0] for s in acquire_count)
    seconds = sum(series_increase(s, window, scrape["first_scrape"])[0] for s in acquire_sum)
    max_points = [v for s in acquire_max for t, v in zip(s.times, s.values) if t >= window.start]
    limitations = list(scrape["limitations"])
    limitations.append("Pending borrowers and active connections are gauges read at each 5 s scrape; brief "
                       "saturation between scrapes may not appear in them (acquisition time and timeouts are counters).")
    data = {
        "gauges": {name: summary(series) for name, series in gauges.items()},
        "acquisition_timeouts": int(round(timeout_total)),
        "acquisitions": int(round(count)),
        "acquire_mean_ms": round(seconds / count * 1000, 3) if count else None,
        "acquire_max_ms_per_scrape": round(max(max_points) * 1000, 3) if max_points else None,
        "configured_maximum_at_window_end": (target or {}).get("fields", {}).get("poolMaximumSize"),
        "configured_connection_timeout_ms": (target or {}).get("fields", {}).get("poolConnectionTimeoutMs"),
        "scrapes": scrape["summary"],
    }
    return build_envelope("get_pool_metrics", {}, window, scrape["coverage"], "prometheus", data, limitations, [],
                          observed_window=scrape["observed_window"], target=target)
