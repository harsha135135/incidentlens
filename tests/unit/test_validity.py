"""Stage 5 case set and validity rules (evaluation/protocol.md §2, §4), on synthetic bundles."""

from __future__ import annotations

from evaluation import cases
from evaluation.validity import backlog, check_validity


def envelope(tool, coverage="COMPLETE", **data):
    return ("E1", tool, {}, {"tool": tool, "coverage": coverage, "data": data})


def bundle(logs=None, outbox=None, requests=None, blockers=None, pool=None, timings=None, coverage=None):
    coverage = coverage or {}
    return [
        envelope("get_request_metrics", coverage.get("requests", "COMPLETE"), **(requests or {"server_error_ratio": 0})),
        envelope("get_outbox_stats", **(outbox or {"gauges": {"pending": {"max": 5}}})),
        envelope("search_delivery_logs", coverage.get("logs", "COMPLETE"), **(logs or {"counts_by_outcome": {"success": 300}})),
        envelope("get_delivery_timings", **(timings or {})),
        envelope("get_pool_metrics", **(pool or {"acquisition_timeouts": 0})),
        envelope("get_database_blockers", coverage.get("blockers", "COMPLETE"),
                 **(blockers or {"blocker_roles": {}, "samples": {"with_blocking": 0}})),
        envelope("get_runtime_config"),
    ]


def buckets(pairs):
    return {"durable_counts": {"buckets": [{"arrivals": a, "publications": p} for a, p in pairs]}}


def test_the_case_set_follows_its_structural_rules():
    cases.check_templates()


def test_backlog_accumulates_over_buckets():
    assert backlog({"data": buckets([(32, 32), (32, 0), (32, 0), (32, 80)])}) == 64
    assert backlog({"data": {}}) is None


def test_healthy_needs_every_source_complete_and_no_fault_signal():
    template = cases.BY_ID["he-d1"]
    assert check_validity(template, bundle())[0]
    assert not check_validity(template, bundle(coverage={"logs": "PARTIAL"}))[0]
    assert not check_validity(template, bundle(requests={"server_error_ratio": 0.02}))[0]


def test_consumer_unavailable_needs_network_errors_as_most_failures():
    template = cases.BY_ID["cu-d1"]
    assert check_validity(template, bundle(logs={"counts_by_outcome": {"success": 200, "network_error": 90}}))[0]
    assert not check_validity(template, bundle(logs={"counts_by_outcome": {"success": 200, "network_error": 3}}))[0]


def test_lock_pool_needs_complete_blockers_and_pool_timeouts():
    template = cases.BY_ID["lp-d1"]
    blockers = {"blocker_roles": {"other": 3}, "samples": {"with_blocking": 9}}
    assert check_validity(template, bundle(blockers=blockers, pool={"acquisition_timeouts": 12}))[0]
    assert not check_validity(template, bundle(blockers=blockers, pool={"acquisition_timeouts": 0}))[0]
    assert not check_validity(template, bundle(blockers=blockers, pool={"acquisition_timeouts": 12},
                                               coverage={"blockers": "PARTIAL"}))[0]


def test_missing_evidence_needs_the_deciding_source_missing_and_the_symptom_visible():
    consumer = cases.BY_ID["me-d1"]  # logs decide; symptom is a durable backlog
    stalled = buckets([(32, 32)] + [(32, 0)] * 3)
    assert check_validity(consumer, bundle(outbox=stalled, coverage={"logs": "UNAVAILABLE"}))[0]
    assert not check_validity(consumer, bundle(outbox=stalled))[0]  # logs present: not a missing-evidence case
    assert not check_validity(consumer, bundle(outbox=buckets([(32, 30)] * 4), coverage={"logs": "UNAVAILABLE"}))[0]
    locks = cases.BY_ID["me-d4"]  # blockers decide; symptom is transfer server errors
    assert check_validity(locks, bundle(requests={"server_error_ratio": 0.1}, coverage={"blockers": "UNAVAILABLE"}))[0]
    assert not check_validity(locks, bundle(coverage={"blockers": "UNAVAILABLE"}))[0]
