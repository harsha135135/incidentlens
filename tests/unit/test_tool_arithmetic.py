"""The deterministic arithmetic behind the tools. The model never does these calculations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from incidentlens.tools.envelope import (
    Window,
    bucket_quantile,
    build_envelope,
    counter_increase,
    fit_to_limit,
    scrape_gaps,
    stats,
)
from incidentlens.tools.logs import correlate
from incidentlens.tools.metrics import series_increase
from incidentlens.tools.sources import Series

T0 = datetime(2026, 11, 6, 5, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_window_rejects_naive_and_empty_ranges():
    with pytest.raises(ValueError):
        Window(datetime(2026, 1, 1), datetime(2026, 1, 2))
    with pytest.raises(ValueError):
        Window(at(10), at(10))


def test_stats_use_nearest_rank_percentiles():
    result = stats([float(v) for v in range(1, 101)])
    assert result == {"count": 100, "min": 1.0, "p50": 50.0, "p95": 95.0, "max": 100.0, "mean": 50.5}
    assert stats([])["count"] == 0 and stats([])["p95"] is None


def test_bucket_quantile_interpolates_inside_the_bucket():
    # 100 observations: 50 at <= 0.1 s, 40 more at <= 0.25 s, 10 more at <= 1 s.
    buckets = [(0.1, 50.0), (0.25, 90.0), (1.0, 100.0), (float("inf"), 100.0)]
    assert bucket_quantile(0.5, buckets) == pytest.approx(0.1)
    assert bucket_quantile(0.9, buckets) == pytest.approx(0.25)
    assert bucket_quantile(0.95, buckets) == pytest.approx(0.25 + 0.75 * 0.5)
    assert bucket_quantile(0.5, [(0.1, 0.0), (float("inf"), 0.0)]) is None


def test_bucket_quantile_above_the_highest_finite_bucket_reports_that_bound():
    assert bucket_quantile(0.99, [(1.0, 10.0), (float("inf"), 20.0)]) == 1.0


def test_counter_increase_treats_a_drop_as_a_restart():
    assert counter_increase([10, 15, 15, 20]) == (10, 0)
    assert counter_increase([10, 15, 3, 8]) == (5 + 3 + 5, 1)


def test_series_increase_uses_only_the_last_scrape_before_the_window_as_baseline():
    window = Window(at(0), at(60))
    series = Series({}, [at(-10), at(-5), at(0), at(30), at(55)], [100, 104, 106, 110, 120])
    increase, resets = series_increase(series, window, first_scrape=at(-5))
    assert (increase, resets) == (16, 0)  # from 104 (at -5 s) to 120, not from 100


def test_a_series_created_inside_the_window_counts_from_zero():
    window = Window(at(0), at(60))
    series = Series({"status": "503"}, [at(30), at(35)], [4, 9])
    assert series_increase(series, window, first_scrape=at(-5)) == (9, 0)


def test_a_series_present_since_the_first_scrape_is_not_counted_from_zero():
    # Scraping was down at the window start: the first sample is a baseline, not a new series.
    window = Window(at(0), at(60))
    series = Series({}, [at(20), at(25)], [5000, 5004])
    assert series_increase(series, window, first_scrape=at(20)) == (4, 0)


def test_scrape_gaps_report_bounds_within_one_interval():
    window = Window(at(0), at(60))
    times = [at(s) for s in (0, 5, 10, 15, 40, 45, 50, 55)]
    result = scrape_gaps(times, [1.0] * len(times), window, 5.0)
    assert result["gaps"] == [{"start": "2026-11-06T05:00:15.000Z", "end": "2026-11-06T05:00:40.000Z",
                               "seconds": 25.0}]
    assert result["successful_scrapes"] == 8 and result["expected_scrapes"] == 12


def test_failed_scrapes_count_as_gaps():
    window = Window(at(0), at(30))
    times = [at(s) for s in (0, 5, 10, 15, 20, 25)]
    result = scrape_gaps(times, [1, 1, 0, 0, 0, 1], window, 5.0)
    assert result["failed_scrapes"] == 3
    assert result["gaps"][0]["start"] == "2026-11-06T05:00:05.000Z"
    assert result["gaps"][0]["end"] == "2026-11-06T05:00:25.000Z"


def test_a_missing_tail_is_a_gap():
    window = Window(at(0), at(60))
    times = [at(s) for s in range(0, 31, 5)]
    assert scrape_gaps(times, [1.0] * len(times), window, 5.0)["gaps"][-1]["end"] == "2026-11-06T05:01:00.000Z"


def test_fit_to_limit_halves_the_longest_list_and_says_so():
    envelope = build_envelope("search_delivery_logs", {}, Window(at(0), at(60)), "COMPLETE", "test",
                              {"records": [{"text": "x" * 500, "i": i} for i in range(400)], "count": 400},
                              [], [])
    assert len(json.dumps(envelope).encode()) <= 64 * 1024
    assert envelope["truncated"] is True
    assert envelope["data"]["records"][0]["i"] == 0  # source order kept
    assert any("data.records truncated" in note for note in envelope["limitations"])
    assert envelope["data"]["count"] == 400


def test_small_results_are_not_truncated():
    envelope = {"data": {"records": [1, 2, 3]}, "limitations": [], "truncated": False}
    assert fit_to_limit(envelope)["truncated"] is False


def test_envelope_redacts_before_returning():
    envelope = build_envelope("x", {}, Window(at(0), at(60)), "COMPLETE", "test",
                              {"message": "auth with Basic dXNlcjpwYXNzd29yZA== failed"}, [], [])
    assert "dXNlcjpwYXNzd29yZA==" not in json.dumps(envelope)


def attempt(started: float, ended: float, event: str = "e1"):
    return (at(started), event, {"endedAt": at(ended).isoformat()}, "success")


def consumer(received: float, event: str = "e1"):
    return (at(received), event, {"auth_ms": 50.0, "handler_ms": 1.0})


def test_retries_of_the_same_event_pair_by_time_containment():
    attempts = [attempt(0.0, 0.2), attempt(5.0, 5.2)]
    records = [consumer(5.1), consumer(0.1)]
    matches, unused = correlate(attempts, records)
    assert [m[1][0] for m in matches] == [at(0.1), at(5.1)]
    assert unused == 0


def test_a_consumer_record_outside_the_attempt_is_not_paired():
    matches, unused = correlate([attempt(0.0, 0.2)], [consumer(1.0)])
    assert matches[0][1] is None and unused == 1


def test_a_consumer_record_is_used_once():
    matches, unused = correlate([attempt(0.0, 0.2), attempt(0.0, 0.2)], [consumer(0.1)])
    assert [m[1] is not None for m in matches] == [True, False]
    assert unused == 0


def test_non_finite_numbers_never_reach_storage():
    """Regression (extended pilot): NaN gauge values made PostgreSQL reject the evidence."""
    envelope = build_envelope("get_outbox_stats", {}, Window(at(0), at(60)), "PARTIAL", "test",
                              {"value": float("nan"), "nested": [float("inf"), 1.5]}, [], [])
    assert envelope["data"] == {"value": None, "nested": [None, 1.5]}
    json.dumps(envelope, allow_nan=False)
