"""Stage 1 exit gate: known fixture scenarios give correct observations, with accurate gaps and
timestamps (spec §19). Each test drives the live stack through the evaluation controller and
compares what the four tools report with the controller's ground truth. FIXTURE RESULTS, except
the last test, which uses LedgerCore's original in-process consumer (decisions D1).

Run: uv run pytest tests/stack -v   (needs scripts/stack.py up and evaluation/calibrate.py)
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime

from tests.stack.support import observe, plus, seconds_between
from incidentlens.config import PROJECT_ROOT
from incidentlens.tools.envelope import Window, parse_iso

SLOW_HANDLER_MS = 400  # well below LedgerCore's 3 s delivery timeout
SCRAPE_INTERVAL = 5.0  # deploy/prometheus/prometheus.yml
SCRAPE_TIMEOUT = 4.0


def at(text: str) -> datetime:
    return parse_iso(text)


def test_healthy_load_is_observed_as_healthy(settings, store, profile, accounts, controller, results_dir):
    rate = profile["rate_per_second"]
    stop, thread, holder = controller.workload_in_background(accounts, rate, profile["seed"] + 10)
    time.sleep(70)
    workload = controller.stop_workload(stop, thread, holder)
    window = Window(plus(workload.started_at, 5), workload.ended_at)
    seen = observe(settings, store, window, controller, results_dir, "healthy")

    requests = seen["requests_transfers"]
    assert requests["coverage"] == "COMPLETE"
    expected = rate * window.seconds
    assert abs(requests["data"]["requests"] - expected) <= rate * 10 + 2  # two scrape intervals
    assert {row["status"] for row in requests["data"]["by_status"]} == {"201"}
    assert requests["data"]["server_error_ratio"] == 0
    assert requests["data"]["latency_seconds"]["p95"] is not None

    outbox = seen["outbox"]
    assert outbox["coverage"] == "COMPLETE"
    counts = outbox["data"]["durable_counts"]
    assert abs(counts["committed_arrivals"] - expected) <= 0.1 * expected + 2
    assert counts["recorded_publications"] >= 0.9 * counts["committed_arrivals"]
    assert outbox["data"]["gauges"]["pending"]["max"] <= 40
    assert outbox["data"]["gauges"]["failed"]["max"] == 0

    logs = seen["delivery_logs"]
    assert logs["coverage"] == "COMPLETE"
    outcomes = logs["data"]["counts_by_outcome"]
    assert outcomes["success"] == logs["data"]["matching_records"] > 0
    assert sum(count for name, count in outcomes.items() if name != "success") == 0

    timings = seen["timings"]["data"]
    assert timings["consumer_measured_by"] == "fixture consumer (test fixture)"
    assert timings["correlated_attempts"] == timings["attempts"] > 0
    assert 20 <= timings["consumer_auth_ms"]["p50"] <= 300  # BCrypt cost 10
    assert timings["consumer_handler_ms"]["p50"] < 5


def test_an_unavailable_consumer_shows_refused_connections_and_stalled_publications(
        settings, store, profile, accounts, controller, results_dir):
    stop, thread, holder = controller.workload_in_background(accounts, profile["rate_per_second"],
                                                             profile["seed"] + 20)
    time.sleep(20)
    fault_start = at(controller.set_consumer_mode("unavailable"))
    time.sleep(40)
    fault_end = at(controller.set_consumer_mode("normal"))
    time.sleep(30)
    controller.stop_workload(stop, thread, holder)
    window = Window(plus(fault_start, -15), plus(fault_end, 25))
    seen = observe(settings, store, window, controller, results_dir, "unavailable_consumer")

    errors = seen["network_errors"]["data"]["records"]
    assert errors, "no network errors were observed during the outage"
    assert {row["result"] for row in errors} == {"CONNECTION_FAILED"}
    first, last = at(errors[0]["started_at"]), at(errors[-1]["started_at"])
    assert -0.05 <= seconds_between(fault_start, first) <= 3, (fault_start, first)
    assert seconds_between(last, fault_end) >= -2, (last, fault_end)
    assert seen["delivery_logs"]["data"]["counts_by_outcome"]["success"] > 0  # before and after

    outbox = seen["outbox"]["data"]
    inside = [bucket for bucket in outbox["durable_counts"]["buckets"]
              if at(bucket["start"]) >= plus(fault_start, 2)
              and plus(at(bucket["start"]), outbox["durable_counts"]["bucket_seconds"]) <= fault_end]
    assert inside and all(bucket["publications"] == 0 for bucket in inside), inside
    assert outbox["gauges"]["pending"]["max"] >= 0.5 * profile["rate_per_second"] * 40

    timings = seen["timings"]["data"]
    assert "CONNECTION_FAILED" in timings["total_ms_by_result"]
    assert timings["uncorrelated_attempts"] >= len(errors)

    transfers = seen["requests_transfers"]["data"]
    assert transfers["server_error_ratio"] == 0  # the ledger API itself was unaffected


def test_a_slow_consumer_shows_long_handler_time_without_errors(
        settings, store, profile, accounts, controller, results_dir):
    stop, thread, holder = controller.workload_in_background(accounts, profile["rate_per_second"],
                                                             profile["seed"] + 30)
    time.sleep(15)
    slow_start = at(controller.set_consumer_mode("slow", SLOW_HANDLER_MS))
    time.sleep(45)
    slow_end = at(controller.set_consumer_mode("normal"))
    controller.stop_workload(stop, thread, holder)
    window = Window(plus(slow_start, 5), slow_end)
    seen = observe(settings, store, window, controller, results_dir, "slow_consumer")

    outcomes = seen["delivery_logs"]["data"]["counts_by_outcome"]
    assert outcomes["success"] > 0
    assert sum(count for name, count in outcomes.items() if name != "success") == 0  # no timeouts, no errors

    timings = seen["timings"]["data"]
    assert SLOW_HANDLER_MS - 5 <= timings["consumer_handler_ms"]["p50"] <= SLOW_HANDLER_MS + 60
    assert 20 <= timings["consumer_auth_ms"]["p50"] <= 300  # authentication unchanged
    assert timings["total_ms"]["p50"] >= SLOW_HANDLER_MS + timings["consumer_auth_ms"]["p50"] * 0.8
    assert "TIMEOUT" not in timings["total_ms_by_result"]

    counts = seen["outbox"]["data"]["durable_counts"]
    assert counts["publications_per_second"] < counts["arrivals_per_second"]
    pending = seen["outbox"]["data"]["gauges"]["pending"]
    assert pending["last"]["value"] > pending["first"]["value"]


def test_an_induced_scrape_gap_is_reported_with_accurate_bounds(
        settings, store, profile, accounts, controller, results_dir):
    stop, thread, holder = controller.workload_in_background(accounts, profile["rate_per_second"],
                                                             profile["seed"] + 40)
    time.sleep(15)
    paused, resumed = controller.pause_ledgercore(20)
    time.sleep(20)
    controller.stop_workload(stop, thread, holder)
    window = Window(plus(paused, -15), plus(resumed, 20))
    seen = observe(settings, store, window, controller, results_dir, "induced_gap")

    requests = seen["requests_transfers"]
    assert requests["coverage"] == "PARTIAL"
    gaps = requests["data"]["scrapes"]["gaps"]
    assert len(gaps) == 1, gaps
    gap_start, gap_end = at(gaps[0]["start"]), at(gaps[0]["end"])
    # Prometheus timestamps samples with the scrape's start time. The gap starts at the last good
    # scrape before the pause (up to one interval earlier). It ends at the first good scrape after the
    # resume, which may have started up to one scrape timeout (4 s) before the resume and waited.
    assert -SCRAPE_INTERVAL - 1 <= seconds_between(paused, gap_start) <= 1, (paused, gap_start)
    assert -SCRAPE_TIMEOUT - 0.5 <= seconds_between(resumed, gap_end) <= 2 * SCRAPE_INTERVAL, (resumed, gap_end)
    assert any("Scrapes are missing" in note for note in requests["limitations"])
    assert seen["outbox"]["coverage"] == "PARTIAL"
    assert seen["outbox"]["data"]["scrapes"]["gaps"] == gaps


def test_smoke_with_ledgercores_original_in_process_consumer(settings, store, profile, accounts, controller,
                                                             results_dir):
    restart = [sys.executable, str(PROJECT_ROOT / "scripts" / "stack.py"), "restart-ledgercore"]
    subprocess.run(restart + ["--consumer", "in-process"], check=True, cwd=PROJECT_ROOT)
    controller.record("ledgercore_restarted", consumer="in-process")
    try:
        time.sleep(10)  # one startup record ingested, scrapes resumed
        workload = controller.run_workload(accounts, 2.0, 40, profile["seed"] + 50)
        window = Window(plus(workload.started_at, 5), workload.ended_at)
        seen = observe(settings, store, window, controller, results_dir, "in_process_consumer_smoke")
    finally:
        subprocess.run(restart + ["--consumer", "fixture"], check=True, cwd=PROJECT_ROOT)
        controller.record("ledgercore_restarted", consumer="fixture")

    logs = seen["delivery_logs"]["data"]
    assert logs["counts_by_outcome"]["success"] == logs["matching_records"] > 0
    counts = seen["outbox"]["data"]["durable_counts"]
    assert counts["recorded_publications"] > 0
    consumer_route = seen["requests_consumer"]["data"]
    assert consumer_route["requests"] > 0
    assert {row["status"] for row in consumer_route["by_status"]} == {"200"}
    timings = seen["timings"]
    assert timings["data"]["consumer_target"] == "in-process"
    assert timings["data"]["consumer_auth_ms"]["count"] == 0
    assert any("no authentication or handler timer" in note for note in timings["limitations"])
