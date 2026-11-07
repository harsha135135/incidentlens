"""Stage 1 healthy calibration (decisions D8). FIXTURE RESULT: deliveries go to the fixture
consumer at its baseline BCrypt cost.

1. Capacity. Send a burst of transfers, then measure LedgerCore's publication rate while the
   backlog drains, from durable publication counts (get_outbox_stats).
2. Healthy rate. 40 % of the measured capacity, rounded down to 0.1 events/s, with a fixed seed.
3. Baseline. At least 60 s (here 90 s) at the healthy rate; pending must stay bounded and
   publications must keep up with arrivals.

Writes evaluation/results/stage1/calibration-<timestamp>.json and, only when the baseline passes,
evaluation/healthy_profile.json. Needs the stack: uv run python scripts/stack.py up

Usage: PYTHONPATH=src uv run python evaluation/calibrate.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation.controller import Controller, iso, utc_now  # noqa: E402
from incidentlens.actions import NoArguments  # noqa: E402
from incidentlens.config import PROJECT_ROOT, TARGET_SETTLE_SECONDS, load_settings  # noqa: E402
from incidentlens.database import connect  # noqa: E402
from incidentlens.tools.envelope import Deadline, Window  # noqa: E402
from incidentlens.tools.outbox import get_outbox_stats  # noqa: E402
from incidentlens.tools.registry import live_context  # noqa: E402
from incidentlens.tools.sources import target_query  # noqa: E402

SEED = 20261106
BURST_RATE, BURST_SECONDS = 30.0, 20.0
BASELINE_SECONDS = 90.0
HEALTHY_FRACTION = 0.4
BATCH_SIZE = 20  # LedgerCore's ledgercore.outbox.batch-size
RESULTS = PROJECT_ROOT / "evaluation" / "results" / "stage1"


def pending_now(settings) -> int:
    rows = target_query(settings.target_conninfo,
                        "SELECT count(*) FROM diagnostics.outbox_event_times WHERE state = 'PENDING'", (), Deadline())
    return rows[0][0]


def wait_for_drain(settings, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while pending_now(settings) > 0:
        if time.monotonic() > deadline:
            raise SystemExit("the outbox backlog did not drain")
        time.sleep(1)


def outbox_stats(store, settings, window: Window) -> dict:
    wait = TARGET_SETTLE_SECONDS + 1 - (utc_now() - window.end).total_seconds()
    if wait > 0:
        time.sleep(wait)  # let late commits land (D3)
    return get_outbox_stats(live_context(window, store, settings), NoArguments())


def main() -> int:
    settings = load_settings()
    controller = Controller(run_id="calibration-" + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    controller.set_consumer_mode("normal")
    accounts = controller.prepare_accounts(pairs=20, seed=SEED)
    wait_for_drain(settings)
    report: dict = {"label": "FIXTURE RESULT (fixture consumer at baseline BCrypt cost 10)", "seed": SEED,
                    "run_id": controller.run_id}

    with connect(settings.app_conninfo, autocommit=True) as store:
        # 1. Capacity: a backlog, then the publication rate while it drains.
        burst = controller.run_workload(accounts, BURST_RATE, BURST_SECONDS, SEED + 1)
        controller.record("burst_sent", **burst.as_json())
        wait_for_drain(settings)
        drained_at = utc_now()
        # Count only while the backlog was certainly larger than one batch: skip the first and last 10 s.
        measure = Window(burst.started_at + timedelta(seconds=10), drained_at - timedelta(seconds=10))
        drain = outbox_stats(store, settings, measure)
        capacity = drain["data"]["durable_counts"]["publications_per_second"]
        report["capacity"] = {"burst": burst.as_json(), "drained_at": iso(drained_at),
                              "measured_window": measure.as_json(), "publications_per_second": capacity,
                              "evidence": drain}

        # 2. Healthy rate, well below capacity.
        healthy_rate = math.floor(capacity * HEALTHY_FRACTION * 10) / 10
        report["healthy_rate_per_second"] = healthy_rate

        # 3. Baseline at that rate.
        controller.record("baseline_started", rate_per_second=healthy_rate)
        baseline = controller.run_workload(accounts, healthy_rate, BASELINE_SECONDS, SEED + 2)
        controller.record("baseline_finished", **baseline.as_json())
        window = Window(baseline.started_at + timedelta(seconds=10), baseline.ended_at)
        evidence = outbox_stats(store, settings, window)
        gauges, counts = evidence["data"]["gauges"], evidence["data"]["durable_counts"]
        checks = {
            "baseline_seconds_at_least_60": window.seconds >= 60,
            "pending_max_at_most_two_batches": gauges["pending"]["max"] <= 2 * BATCH_SIZE,
            "pending_last_at_most_one_batch": gauges["pending"]["last"]["value"] <= BATCH_SIZE,
            "publications_keep_up": counts["recorded_publications"] >= 0.9 * counts["committed_arrivals"],
            "no_failed_events": gauges["failed"]["max"] == 0,
            "all_transfers_accepted": set(baseline.statuses) == {201} and not baseline.errors,
            "coverage_complete": evidence["coverage"] == "COMPLETE",
        }
        report["baseline"] = {"workload": baseline.as_json(), "window": window.as_json(), "checks": checks,
                              "pending_max": gauges["pending"]["max"], "pending_last": gauges["pending"]["last"],
                              "arrivals": counts["committed_arrivals"],
                              "publications": counts["recorded_publications"], "evidence": evidence}
        report["passed"] = all(checks.values())

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"calibration-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"capacity_per_second": capacity, "healthy_rate_per_second": healthy_rate,
                      "checks": checks, "passed": report["passed"]}, indent=2))
    print(f"wrote {out.relative_to(PROJECT_ROOT)}")
    if report["passed"]:
        profile = {"label": report["label"], "rate_per_second": healthy_rate, "seed": SEED, "account_pairs": 20,
                   "measured_capacity_per_second": capacity, "calibrated_at": iso(utc_now()),
                   "calibration_file": str(out.relative_to(PROJECT_ROOT))}
        (PROJECT_ROOT / "evaluation" / "healthy_profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
