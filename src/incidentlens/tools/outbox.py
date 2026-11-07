"""get_outbox_stats: outbox gauge history from Prometheus plus durable arrival and publication
counts from LedgerCore's database, read through the diagnostics view."""

from __future__ import annotations

import math
from datetime import timedelta

from incidentlens.config import TARGET_SETTLE_SECONDS
from incidentlens.tools.envelope import Deadline, Window, build_envelope, iso, utc_now, worst_coverage
from incidentlens.tools.metrics import scrape_coverage
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import SourceError, target_config, target_query

TOOL = "get_outbox_stats"
GAUGES = {
    "pending": "ledgercore_outbox_pending_events",
    "failed": "ledgercore_outbox_failed_events",
    "oldest_undelivered_age_seconds": "ledgercore_outbox_oldest_undelivered_age_seconds",
}
MAX_BUCKETS = 30
MAX_GAUGE_POINTS = 30
ARRIVAL_MEANING = "committed outbox records grouped by creation (transaction-start) timestamp"
PUBLICATION_MEANING = "recorded publications grouped by publication-transaction timestamp"
# Fixed templates over the payload-free view. The column name comes from this module, never input.
COUNT_SQL = ("SELECT date_bin(%s::interval, {column}, %s) AS bucket, count(*) "
             "FROM diagnostics.outbox_event_times WHERE {column} >= %s AND {column} < %s "
             "GROUP BY bucket ORDER BY bucket")


def get_outbox_stats(context: ToolContext, arguments) -> dict:
    deadline = Deadline()
    window = context.window
    target = target_config(context.store, context.service, window)
    limitations, errors = [], []
    data: dict = {}

    gauge_coverage = "UNAVAILABLE"
    observed_window = {"first": None, "last": None}
    try:
        scrape = scrape_coverage(context.prometheus, window, deadline)
        data["gauges"] = {name: gauge_summary(context, metric, window, deadline) for name, metric in GAUGES.items()}
        data["scrapes"] = scrape["summary"]
        gauge_coverage = scrape["coverage"]
        observed_window = scrape["observed_window"]
        limitations += scrape["limitations"]
    except SourceError as error:
        errors.append(str(error))
        limitations.append("Outbox gauge history is unavailable (Prometheus could not be queried).")

    durable_coverage = "UNAVAILABLE"
    try:
        data["durable_counts"], durable_coverage, notes = durable_counts(context, window, deadline)
        limitations += notes
    except SourceError as error:
        errors.append(str(error))
        limitations.append("Durable arrival and publication counts are unavailable (target database).")

    unreadable = sum((g or {}).get("unreadable_samples", 0) for g in (data.get("gauges") or {}).values())
    if unreadable:
        limitations.append(f"{unreadable} gauge sample(s) were exported as NaN (LedgerCore could not read the value, "
                           "for example while its connection pool was exhausted); they are treated as missing.")
    limitations.append("Pending is a gauge sampled at scrape times; arrivals and publications are durable "
                       "counts by timestamp. Transitions to FAILED and the different sampling instants mean "
                       "pending change need not equal arrivals minus publications.")
    if durable_coverage == "UNAVAILABLE" and gauge_coverage == "UNAVAILABLE":
        coverage = "UNAVAILABLE"
    else:
        coverage = worst_coverage(gauge_coverage, durable_coverage)
        if coverage == "UNAVAILABLE":
            coverage = "PARTIAL"  # one of the two sources answered
    return build_envelope(TOOL, {}, window, coverage, "prometheus+postgres_aggregate", data, limitations, errors,
                          observed_window=observed_window, target=target)


def gauge_summary(context: ToolContext, metric: str, window: Window, deadline: Deadline) -> dict:
    series = context.prometheus.raw_samples(f'{metric}{{job="ledgercore"}}', window, deadline)
    points = sorted((t, v) for s in series for t, v in zip(s.times, s.values))
    unreadable = sum(s.unreadable for s in series)
    if not points:
        return {"samples": 0, "unreadable_samples": unreadable, "first": None, "last": None, "min": None,
                "max": None, "points": []}
    values = [v for _, v in points]
    step = max(1, math.ceil(len(points) / MAX_GAUGE_POINTS))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return {
        "samples": len(points),
        "unreadable_samples": unreadable,
        "first": {"at": iso(points[0][0]), "value": points[0][1]},
        "last": {"at": iso(points[-1][0]), "value": points[-1][1]},
        "min": min(values),
        "max": max(values),
        "points": [[iso(t), v] for t, v in sampled],
    }


def bucket_seconds(window: Window) -> int:
    """At most 30 buckets, at least 10 seconds each, in whole tens of seconds."""
    return max(10, math.ceil(window.seconds / MAX_BUCKETS / 10) * 10)


def durable_counts(context: ToolContext, window: Window, deadline: Deadline) -> tuple[dict, str, list[str]]:
    size = bucket_seconds(window)
    interval = f"{size} seconds"
    params = (interval, window.start, window.start, window.end)
    arrivals = target_query(context.settings.target_conninfo, COUNT_SQL.format(column="created_at"), params, deadline)
    publications = target_query(context.settings.target_conninfo, COUNT_SQL.format(column="published_at"), params,
                                deadline)
    by_bucket: dict = {}
    for bucket, count in arrivals:
        by_bucket.setdefault(bucket, [0, 0])[0] = count
    for bucket, count in publications:
        by_bucket.setdefault(bucket, [0, 0])[1] = count
    buckets = []
    moment = window.start
    while moment < window.end:  # every bucket listed, including empty ones
        arrived, published = by_bucket.get(moment, [0, 0])
        bucket = {"start": iso(moment), "arrivals": arrived, "publications": published}
        if moment + timedelta(seconds=size) > window.end:
            bucket["partial_seconds"] = round((window.end - moment).total_seconds(), 3)
        buckets.append(bucket)
        moment += timedelta(seconds=size)
    total_arrivals = sum(count for _, count in arrivals)
    total_publications = sum(count for _, count in publications)

    notes = []
    settled = (utc_now() - window.end).total_seconds() >= TARGET_SETTLE_SECONDS
    if not settled:
        notes.append(f"The window ended less than {TARGET_SETTLE_SECONDS:.0f} s ago; a transaction that "
                     "started inside it may still commit, so the latest arrival counts can still grow.")
    data = {
        "committed_arrivals": total_arrivals,
        "recorded_publications": total_publications,
        "window_seconds": round(window.seconds, 3),
        "arrivals_per_second": round(total_arrivals / window.seconds, 3),
        "publications_per_second": round(total_publications / window.seconds, 3),
        "bucket_seconds": size,
        "buckets": buckets,
        "arrival_timestamp_meaning": ARRIVAL_MEANING,
        "publication_timestamp_meaning": PUBLICATION_MEANING,
        "settled": settled,
    }
    return data, ("COMPLETE" if settled else "PARTIAL"), notes
