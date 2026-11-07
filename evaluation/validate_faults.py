"""Fault validation (decisions D2, D44): each new fault must produce its required signal before any
pilot uses it. A fault that cannot is reported, not forced with hooks in LedgerCore.

  credential_rejection  the fixture consumer rejects the publisher's credential:
                        delivery logs show auth_error dominating the failures
  lock_contention       a separate owner session holds row locks on workload accounts: the
                        historical blocker samples show edges whose blocker role is "other"
  pool_saturation       LedgerCore restarted with a smaller pool and a short connection timeout:
                        pending borrowers above zero or acquisition timeouts in the window

FIXTURE RESULTS. Usage: PYTHONPATH=src:. uv run python evaluation/validate_faults.py [fault ...]
Environment for pool calibration: POOL_SIZE (default 1), POOL_TIMEOUT_MS (default 250), POOL_RATE.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation.controller import Controller, utc_now  # noqa: E402
from incidentlens.config import PROJECT_ROOT, TARGET_SETTLE_SECONDS, load_settings  # noqa: E402
from incidentlens.database import connect  # noqa: E402
from incidentlens.tools.envelope import Window  # noqa: E402
from incidentlens.tools.registry import live_context, run_tool  # noqa: E402

LOCKED_FRACTION = 0.3


def check(fault: str, seen: dict) -> tuple[bool, str]:
    if fault == "credential_rejection":
        counts = seen["search_delivery_logs"]["data"]["counts_by_outcome"]
        failures = sum(v for k, v in counts.items() if k != "success")
        ok = counts["auth_error"] >= 5 and counts["auth_error"] >= 0.5 * failures
        return ok, f"auth_error {counts['auth_error']} of {failures} failures"
    if fault == "lock_contention":
        data = seen["get_database_blockers"]["data"]
        ok = data["blocker_roles"].get("other", 0) >= 1 and data["samples"]["with_blocking"] >= 2
        return ok, f"blocker roles {data['blocker_roles']}, samples with blocking {data['samples']['with_blocking']}"
    data = seen["get_pool_metrics"]["data"]
    pending = data["gauges"]["pending"]
    ok = (pending.get("samples_above_zero") or 0) >= 1 or data["acquisition_timeouts"] >= 1
    return ok, (f"pending above zero in {pending.get('samples_above_zero')} scrapes (max {pending.get('max')}), "
                f"acquisition timeouts {data['acquisition_timeouts']}, acquire mean {data['acquire_mean_ms']} ms")


def run(fault: str, settings, store, profile: dict, accounts) -> dict:
    controller = Controller(run_id=f"validate-{fault}-" + utc_now().strftime("%H%M%S"))
    controller.set_consumer_mode("normal")
    rate = float(os.environ.get("POOL_RATE", profile["rate_per_second"])) if fault == "pool_saturation" \
        else profile["rate_per_second"]
    stop, thread, holder = controller.workload_in_background(accounts, rate, profile["seed"] + 77)
    start = utc_now()
    time.sleep(15)
    if fault == "credential_rejection":
        controller.set_consumer_mode("reject")
        time.sleep(40)
        controller.set_consumer_mode("normal")
    elif fault == "lock_contention":
        locked = [source for source, _ in accounts[: max(1, int(len(accounts) * LOCKED_FRACTION))]]
        controller.hold_account_locks(locked, 40)
    elif fault == "pool_exhaustion_by_lock_waits":
        # Causal chain: transactions wait on held locks while holding pool connections.
        controller.restart_ledgercore(int(os.environ.get("POOL_SIZE", "2")), int(os.environ.get("POOL_TIMEOUT_MS", "250")))
        locked = [source for source, _ in accounts[: max(1, int(len(accounts) * LOCKED_FRACTION))]]
        controller.hold_account_locks(locked, 40)
    else:
        controller.restart_ledgercore(int(os.environ.get("POOL_SIZE", "1")), int(os.environ.get("POOL_TIMEOUT_MS", "250")))
        time.sleep(40)
    time.sleep(10)
    end = utc_now()
    workload = controller.stop_workload(stop, thread, holder)
    if fault in ("pool_saturation", "pool_exhaustion_by_lock_waits"):
        controller.restart_ledgercore()  # back to the default pool
    time.sleep(TARGET_SETTLE_SECONDS + 6)
    window = Window(start, end)
    context = live_context(window, store, settings)
    tools = {"credential_rejection": [("search_delivery_logs", {"outcome": "any", "limit": 20})],
             "lock_contention": [("get_database_blockers", {"mode": "historical", "limit": 10}),
                                 ("get_request_metrics", {"route_group": "transfers"})],
             "pool_saturation": [("get_pool_metrics", {}), ("get_request_metrics", {"route_group": "transfers"}),
                                 ("get_runtime_config", {})],
             "pool_exhaustion_by_lock_waits": [("get_pool_metrics", {}), ("get_database_blockers",
                                               {"mode": "historical", "limit": 10}),
                                               ("get_request_metrics", {"route_group": "transfers"})]}[fault]
    seen = {tool: run_tool(tool, arguments, context) for tool, arguments in tools}
    ok, detail = check(fault, seen)
    return {"fault": fault, "window": window.as_json(), "signal_present": ok, "detail": detail,
            "workload": workload.as_json(), "workload_rate": rate, "controller_run": controller.run_id,
            "parameters": {"pool_size": os.environ.get("POOL_SIZE", "1"), "pool_timeout_ms": os.environ.get("POOL_TIMEOUT_MS", "250")}
            if fault.startswith("pool") else {}, "observations": seen}


def main() -> int:
    faults = sys.argv[1:] or ["credential_rejection", "lock_contention", "pool_saturation"]
    settings = load_settings()
    profile = json.loads((PROJECT_ROOT / "evaluation/healthy_profile.json").read_text())
    accounts = Controller(run_id="validate-accounts").prepare_accounts(profile["account_pairs"], profile["seed"])
    out = PROJECT_ROOT / "evaluation/results/stage4"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with connect(settings.app_conninfo, autocommit=True) as store:
        for fault in faults:
            result = run(fault, settings, store, profile, accounts)
            results.append(result)
            print(json.dumps({k: result[k] for k in ("fault", "signal_present", "detail", "workload_rate", "parameters")}),
                  flush=True)
    path = out / f"fault-validation-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"label": "FIXTURE RESULT", "results": results}, indent=2, default=str) + "\n")
    print(f"wrote {path.relative_to(PROJECT_ROOT)}")
    return 0 if all(r["signal_present"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
