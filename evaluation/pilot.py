"""Stage 3 pilot (spec §16, §19): two faults (unavailable vs slow consumer) plus healthy runs,
each investigated by A (rules), B (one call) and C (the agent: real provider, runner in OpenShell).

FIXTURE RESULTS. Live recorded-window pilot: for each run the controller records one window; A
and B share one fixed bundle collected from it, and C investigates the same window through the
public API. Systems run in a seeded random order. The fault label exists only in the controller's
files; every system gets the same neutral symptom. This is the early-stop check (decisions D13),
not the evaluation: no usefulness claim is made from it.

Needs: scripts/stack.py up, evaluation/calibrate.py, scripts/stack.py gateway up (openshell).
Usage: PYTHONPATH=src:. uv run python evaluation/pilot.py [runs_per_scenario] [stage3|extended]
       PILOT_HEALTHY_WINDOWS=1 adds healthy windows (the extended pilot uses 1).
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation.baselines import baseline_a, baseline_b, classify_report, collect_bundle  # noqa: E402
from evaluation.controller import Controller, iso, utc_now  # noqa: E402
from incidentlens.config import PROJECT_ROOT, TARGET_SETTLE_SECONDS, load_settings  # noqa: E402
from incidentlens.database import connect  # noqa: E402
from incidentlens.tools.envelope import Deadline, Window  # noqa: E402
from incidentlens.tools.sources import target_query  # noqa: E402

LATE_NOTIFICATIONS = "Customers say that transfer notifications are arriving late."
FAILING_TRANSFERS = "Some customers say their transfers are failing or taking several seconds."
SYMPTOM = LATE_NOTIFICATIONS
LOCKED_FRACTION = 0.3
SEED = int(__import__("os").environ.get("PILOT_SEED", "20261107"))
FAULT_START, FAULT_SECONDS, WINDOW_SECONDS = 30, 90, 150
SCENARIOS = {  # name -> (fault, handler delay ms, ground-truth primary cause, symptom); frozen per D41/D45
    "healthy": (None, 0, "none", LATE_NOTIFICATIONS),
    "unavailable_consumer": ("unavailable", 0, "consumer_unavailable", LATE_NOTIFICATIONS),
    "slow_consumer": ("slow", 400, "slow_consumer_handler", LATE_NOTIFICATIONS),
    "credential_rejection": ("reject", 0, "consumer_credential_rejected", LATE_NOTIFICATIONS),
    "lock_contention": ("locks", 0, "lock_contention", FAILING_TRANSFERS),
    "lock_pool_exhaustion": ("locks_small_pool", 0, "lock_contention", FAILING_TRANSFERS),
}
SCENARIO_SETS = {
    "stage3": {"healthy": 1, "unavailable_consumer": 1, "slow_consumer": 1},
    "extended": {"credential_rejection": 1, "lock_contention": 1, "lock_pool_exhaustion": 1},
    # After the rescope (D48), "C" is the product's job pipeline: fixed bundle, one model decision.
    "product_smoke": {"slow_consumer": 1},
}
PUBLIC = "http://127.0.0.1:8088"
PRICING = json.loads((PROJECT_ROOT / "evaluation/pricing/anthropic-2026-11-04.json").read_text())


def price(input_tokens: int | None, output_tokens: int | None) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    model = PRICING["models"]["claude-sonnet-5-5"]
    return round(input_tokens / 1e6 * model["input"] + output_tokens / 1e6 * model["output"], 5)


def wait_for_empty_outbox(settings, timeout: float = 300) -> None:
    """Each window starts clean: a backlog from the previous run must not leak into this one."""
    deadline = time.monotonic() + timeout
    while target_query(settings.target_conninfo, "SELECT count(*) FROM diagnostics.outbox_event_times "
                       "WHERE state = 'PENDING'", (), Deadline())[0][0] > 0:
        if time.monotonic() > deadline:
            raise SystemExit("the outbox did not drain before the next window")
        time.sleep(1)


def record_window(controller: Controller, accounts, scenario: str, profile: dict, seed: int, settings) -> Window:
    mode, delay, _, _ = SCENARIOS[scenario]
    wait_for_empty_outbox(settings)
    stop, thread, holder = controller.workload_in_background(accounts, profile["rate_per_second"], seed)
    start = utc_now()
    time.sleep(FAULT_START)
    fault_began = time.monotonic()
    locked = [source for source, _ in accounts[: max(1, int(len(accounts) * LOCKED_FRACTION))]]
    if mode in ("unavailable", "slow", "reject"):
        controller.set_consumer_mode(mode, delay)
        time.sleep(FAULT_SECONDS)
        controller.set_consumer_mode("normal")
    elif mode == "locks":
        controller.hold_account_locks(locked, FAULT_SECONDS)
    elif mode == "locks_small_pool":
        controller.restart_ledgercore(pool_size=2, connection_timeout_ms=250)  # part of the fault, inside the window
        controller.hold_account_locks(locked, max(10.0, FAULT_SECONDS - (time.monotonic() - fault_began)))
    else:
        time.sleep(FAULT_SECONDS)
    time.sleep(max(0.0, WINDOW_SECONDS - FAULT_START - (time.monotonic() - fault_began)))
    end = utc_now()
    controller.stop_workload(stop, thread, holder)
    if mode == "locks_small_pool":
        controller.restart_ledgercore()  # back to the default pool, after the window
    controller.record("window", start=iso(start), end=iso(end))
    time.sleep(TARGET_SETTLE_SECONDS + 6)  # late commits land; the collector reads past the end
    return Window(start, end)


def browser_client() -> httpx.Client:
    secret = (PROJECT_ROOT / "deploy" / "local" / "access-secret.txt").read_text().strip()
    client = httpx.Client(base_url=PUBLIC, headers={"Origin": PUBLIC, "Host": "127.0.0.1:8088"}, timeout=15)
    client.post("/api/v1/session", json={"secret": secret}).raise_for_status()
    client.headers["X-CSRF-Token"] = client.get("/api/v1/session").json()["csrf_token"]
    return client


def run_agent(client: httpx.Client, window: Window, key: str, symptom: str = SYMPTOM) -> dict:
    started = time.monotonic()
    created = client.post("/api/v1/investigations", headers={"Idempotency-Key": key},
                          json={"service_id": "ledgercore-local", "symptom": symptom,
                                "window": {"start": window.start.isoformat(), "end": window.end.isoformat()}})
    created.raise_for_status()
    job_id = created.json()["id"]
    while True:
        job = client.get(f"/api/v1/investigations/{job_id}").json()
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED") or time.monotonic() - started > 300:
            break
        time.sleep(2)
    report = client.get(f"/api/v1/investigations/{job_id}/report")
    body = report.json() if report.status_code == 200 else {}
    events = client.get(f"/api/v1/investigations/{job_id}/events").json()["events"]
    usage = (body.get("report") or {}).get("usage", {})
    tools = [e["payload"].get("tool") for e in events if e["type"] == "action_admitted" and e["payload"]["kind"] == "tool"]
    return {"job_id": job_id, "status": job["status"], "final_reason": job.get("final_reason"),
            "elapsed_seconds": round(time.monotonic() - started, 1), "report": body.get("report"),
            "report_source": body.get("source"), "model_calls": job.get("model_actions"),
            "tool_calls": job.get("tool_actions"), "tools": tools, "recoveries": job.get("recoveries"),
            "input_tokens": usage.get("reported_input_tokens"), "output_tokens": usage.get("reported_output_tokens"),
            "unknown_usage_calls": usage.get("model_calls_with_unknown_usage")}


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    scenario_set = sys.argv[2] if len(sys.argv) > 2 else "stage3"
    healthy_extra = int(os.environ.get("PILOT_HEALTHY_WINDOWS", "0"))
    settings = load_settings()
    profile = json.loads((PROJECT_ROOT / "evaluation/healthy_profile.json").read_text())
    rng = random.Random(SEED)
    out = PROJECT_ROOT / "evaluation/results/stage3" / ("pilot-" + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=True)
    plan = [scenario for scenario in SCENARIO_SETS[scenario_set] for _ in range(runs)] + ["healthy"] * healthy_extra
    rng.shuffle(plan)
    client = browser_client()
    accounts = Controller(run_id="pilot-accounts").prepare_accounts(profile["account_pairs"], profile["seed"])
    summary = []
    with connect(settings.app_conninfo, autocommit=True) as store:
        for index, scenario in enumerate(plan, 1):
            controller = Controller(run_id=f"pilot-{index:02d}-" + utc_now().strftime("%H%M%S"))
            controller.set_consumer_mode("normal")
            window = record_window(controller, accounts, scenario, profile, SEED + index, settings)
            truth, symptom = SCENARIOS[scenario][2], SCENARIOS[scenario][3]
            order = ["A", "B", "C"]
            rng.shuffle(order)
            results, bundle = {}, None
            for system in order:
                if system in ("A", "B") and bundle is None:
                    bundle = collect_bundle(window, store, settings)
                if system == "A":
                    results["A"] = baseline_a(bundle) | {"model_calls": 0, "tool_calls": len(bundle)}
                    results["A"]["classified"] = results["A"]["primary_cause"]
                elif system == "B":
                    results["B"] = baseline_b(bundle, symptom, window)
                    results["B"]["classified"] = classify_report(results["B"]["report"])
                    results["B"]["cost_usd"] = price(results["B"]["input_tokens"], results["B"]["output_tokens"])
                else:
                    results["C"] = run_agent(client, window, f"pilot-{controller.run_id}", symptom)
                    results["C"]["classified"] = classify_report(results["C"]["report"])
                    results["C"]["cost_usd"] = price(results["C"]["input_tokens"], results["C"]["output_tokens"])
            record = {"label": "FIXTURE RESULT: pilot (live recorded window), not the evaluation",
                      "scenario_set": scenario_set, "symptom": symptom,
                      "run": index, "scenario": scenario, "ground_truth_cause": truth, "window": window.as_json(),
                      "system_order": order, "controller_run": controller.run_id,
                      "bundle": [{"handle": h, "tool": t, "arguments": a, "coverage": e["coverage"]} for h, t, a, e in bundle],
                      "results": results}
            (out / f"run-{index:02d}-{scenario}.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
            line = {"run": index, "scenario": scenario, "truth": truth,
                    **{f"{s}_cause": results[s]["classified"] for s in "ABC"},
                    **{f"{s}_outcome": (results[s].get("report") or {}).get("outcome", results[s].get("outcome")) for s in "ABC"},
                    "C_status": results["C"]["status"], "C_tools": results["C"]["tool_calls"],
                    "C_model_calls": results["C"]["model_calls"], "C_seconds": results["C"]["elapsed_seconds"],
                    "B_cost": results["B"]["cost_usd"], "C_cost": results["C"]["cost_usd"]}
            summary.append(line)
            print(json.dumps(line), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {out.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
