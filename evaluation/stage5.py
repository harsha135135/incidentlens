"""Stage 5 evaluation harness (evaluation/protocol.md). FIXTURE RESULTS.

For each case-run:
  1. preflight (protocol §3.1) - refuses to record if anything is off;
  2. record one window with the template's workload, distractor and fault;
  3. settle, then freeze the 7-tool bundle (read-only files plus a SHA-256 manifest);
  4. decide validity from the frozen bundle alone (evaluation/validity.py);
  5. if valid, run A (rules) and P (the product, through the public API, with the gateway in replay
     mode answering from the frozen bundle) in a seeded random order.

Every case-run has its own directory with a state file, so an interrupted batch resumes where it
stopped. Ground truth stays in the controller's run directory and the state file; the bundle
directories have opaque names.

Needs: scripts/stack.py up; the gateway in replay mode, which this script starts:
       scripts/stack.py gateway up --replay evaluation/results/stage5/bundles
Usage: PYTHONPATH=src:. uv run python evaluation/stage5.py run development|held_out [--only cu-d1,sc-d1]
       PYTHONPATH=src:. uv run python evaluation/stage5.py replay development   (P again, after a prompt change)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation import cases, freeze  # noqa: E402
from evaluation.baselines import baseline_a, classify_report, collect_bundle  # noqa: E402
from evaluation.controller import LEDGERCORE_PORT, STUB_CONTROL_PORT, Controller, iso, utc_now  # noqa: E402
from evaluation.pilot import browser_client, price, run_agent, wait_for_empty_outbox  # noqa: E402
from evaluation.validity import check_validity  # noqa: E402
from incidentlens import model_gateway  # noqa: E402
from incidentlens.config import PROJECT_ROOT, TARGET_SETTLE_SECONDS, load_settings  # noqa: E402
from incidentlens.database import connect  # noqa: E402
from incidentlens.tools.envelope import Window, parse_iso  # noqa: E402
from incidentlens.tools.registry import live_context, run_tool  # noqa: E402
from incidentlens.tools.replay import verify  # noqa: E402

RESULTS = PROJECT_ROOT / "evaluation" / "results" / "stage5"
BUNDLES = RESULTS / "bundles"
CASE_RUNS = RESULTS / "case-runs"
MAX_INVALID_ATTEMPTS = 3  # protocol §4
RUNS_PER_SPLIT = {"development": 1, "held_out": 3}
LABEL = "FIXTURE RESULT: Stage 5 evaluation (recorded window, frozen bundle)"


# ---- small helpers ----------------------------------------------------------------------------

def seed_for(case_run: str, attempt: int) -> int:
    return int(hashlib.sha256(f"{case_run}:{attempt}".encode()).hexdigest()[:8], 16)


def opaque_name(case_run: str, attempt: int) -> str:
    """Bundle directory names carry no category, so nothing downstream can read the label off them."""
    return "b-" + hashlib.sha256(f"bundle:{case_run}:{attempt}".encode()).hexdigest()[:12]


def load_state(case_run: str) -> dict:
    path = CASE_RUNS / case_run / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(case_run: str, state: dict) -> None:
    folder = CASE_RUNS / case_run
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "state.json.tmp"
    temporary.write_text(json.dumps(state, indent=2, default=str) + "\n")
    temporary.replace(folder / "state.json")


def sleep_until(moment) -> None:
    time.sleep(max(0.0, (moment - utc_now()).total_seconds()))


# ---- preflight (protocol §3.1) ----------------------------------------------------------------

def preflight(settings, store, controller: Controller) -> None:
    problems = freeze.check()
    if problems:
        raise SystemExit("the tree does not match the active freeze:\n  " + "\n  ".join(problems[:10]))
    status = subprocess.run(["openshell", "status"], capture_output=True, text=True, timeout=20).stdout
    if "Connected" not in status:
        raise SystemExit("the OpenShell gateway is not connected")
    if httpx.get(f"{settings.prometheus_url}/-/ready", timeout=3).status_code != 200:
        raise SystemExit("Prometheus is not ready")
    if httpx.get(f"http://127.0.0.1:{LEDGERCORE_PORT}/actuator/health/readiness", timeout=3).status_code != 200:
        raise SystemExit("LedgerCore is not ready")
    controller.set_consumer_mode("normal")
    if httpx.get(f"http://127.0.0.1:{STUB_CONTROL_PORT}/mode", timeout=3).json().get("mode") != "normal":
        raise SystemExit("the fixture consumer is not in normal mode")
    deadline = time.monotonic() + 90  # a LedgerCore restart's config record takes a moment to be ingested
    while True:
        heartbeat_age, config = store.execute(
            "SELECT (SELECT extract(epoch FROM now() - max(observed_at)) FROM telemetry_samples "
            "        WHERE kind = 'collector_heartbeat'), "
            "       (SELECT fields FROM log_records WHERE record_type = 'ledgercore.config' "
            "        ORDER BY observed_at DESC LIMIT 1)").fetchone()
        default_pool = config and config.get("poolMaximumSize") == 10 and config.get("poolConnectionTimeoutMs") == 30000
        fixture = config and str(config.get("consumerTarget", "")).endswith(":18090/events")
        if heartbeat_age is not None and heartbeat_age < 15 and default_pool and fixture:
            break
        if time.monotonic() > deadline:
            raise SystemExit(f"preflight: collector heartbeat age {heartbeat_age}, latest config {config}")
        time.sleep(2)
    wait_for_empty_outbox(settings)


# ---- recording one window ---------------------------------------------------------------------

def record_window(template: cases.Template, controller: Controller, accounts, seed: int) -> Window:
    """Runs the template on a window aligned to whole seconds, so replay can match it exactly."""
    for component in template.stop:  # missing evidence: the component is down for the whole window
        controller.stop_component(component)
    stop, thread, holder = controller.workload_in_background(accounts, template.rate, seed)
    start = (utc_now() + timedelta(seconds=2)).replace(microsecond=0)
    end = start + timedelta(seconds=template.window_seconds)
    locked = [source for source, _ in accounts[: max(1, int(len(accounts) * template.locked_fraction))]]
    helpers = []

    def distractor() -> None:
        sleep_until(start + timedelta(seconds=template.distractor_start))
        controller.distractor_in_background(template.distractor, accounts, template.distractor_seconds, seed,
                                            template.rate)

    def fault() -> None:
        fault_start = start + timedelta(seconds=template.fault_start)
        fault_end = fault_start + timedelta(seconds=template.fault_seconds)
        sleep_until(fault_start)
        if template.fault in ("unavailable", "slow", "reject"):
            controller.set_consumer_mode(template.fault, template.delay_ms)
            sleep_until(fault_end)
            controller.set_consumer_mode("normal")
        elif template.fault == "locks":
            controller.hold_account_locks(locked, template.fault_seconds)
        elif template.fault == "locks_small_pool":
            controller.restart_ledgercore(pool_size=template.pool_size, connection_timeout_ms=250)
            controller.hold_account_locks(locked, max(10.0, (fault_end - utc_now()).total_seconds()))

    if template.distractor != "none":
        helpers.append(threading.Thread(target=distractor, daemon=True))
    if template.fault:
        helpers.append(threading.Thread(target=fault, daemon=True))
    for helper in helpers:
        helper.start()
    sleep_until(end)
    for helper in helpers:
        helper.join(timeout=60)
    controller.stop_workload(stop, thread, holder)
    if template.fault == "locks_small_pool":
        controller.restart_ledgercore()  # back to the default pool, after the window
    controller.record("window", start=iso(start), end=iso(end), template=template.template_id)
    return Window(start, end)


def settle(window: Window, template: cases.Template, store, settings) -> None:
    """Normal cases: wait for late commits (D3), then for the collector to read past the window end
    (logs COMPLETE). Missing-evidence cases freeze while the component is still stopped."""
    sleep_until(window.end + timedelta(seconds=TARGET_SETTLE_SECONDS + 1))
    if template.stop:
        return
    deadline = time.monotonic() + 60
    context = live_context(window, store, settings)
    while time.monotonic() < deadline:
        if run_tool("search_delivery_logs", {"outcome": "any", "limit": 1}, context)["coverage"] == "COMPLETE":
            return
        time.sleep(3)


def freeze_bundle(window: Window, store, settings, name: str) -> Path:
    folder = BUNDLES / name
    folder.mkdir(parents=True)
    files = []
    for handle, tool, arguments, envelope in collect_bundle(window, store, settings):
        file_name = f"{handle}-{tool}.json"
        text = json.dumps(envelope, indent=1, sort_keys=True) + "\n"
        (folder / file_name).write_text(text)
        files.append({"handle": handle, "file": file_name, "tool": tool, "arguments": arguments or {},
                      "sha256": hashlib.sha256(text.encode()).hexdigest()})
    manifest = {"window": window.as_json(), "frozen_at": iso(utc_now()), "files": files,
                "freeze": (freeze.active() or ("none",))[0]}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    for path in folder.iterdir():
        path.chmod(0o444)
    folder.chmod(0o555)
    return folder


def load_bundle(folder: Path) -> list[tuple]:
    manifest = verify(folder)
    return [(entry["handle"], entry["tool"], entry["arguments"], json.loads((folder / entry["file"]).read_text()))
            for entry in manifest["files"]]


# ---- the two systems ---------------------------------------------------------------------------

def run_a(folder: Path) -> dict:
    bundle = load_bundle(folder)
    return baseline_a(bundle) | {"model_calls": 0, "tool_calls": len(bundle)}


def run_p(client: httpx.Client, folder: Path, template: cases.Template, key: str) -> dict:
    verify(folder)
    window_json = json.loads((folder / "manifest.json").read_text())["window"]
    window = Window(parse_iso(window_json["start"]), parse_iso(window_json["end"]))
    result = run_agent(client, window, key, template.symptom)
    report = result.get("report") or {}
    result["replay_elapsed_seconds"] = result.pop("elapsed_seconds")  # protocol §5.6: not live latency
    result["classified_dev_aid"] = classify_report(report)  # development aid only (protocol §5.1)
    result["cost_usd"] = price(result["input_tokens"], result["output_tokens"])
    result["checks"] = report.get("checks")
    result["prompt_version"] = model_gateway.PROMPT_VERSION
    return result


# ---- one case-run ------------------------------------------------------------------------------

def run_case(template: cases.Template, run: int, accounts, settings, store) -> dict:
    case_run = f"{template.template_id}-r{run}"
    state = load_state(case_run) or {"label": LABEL, "case_run": case_run, "template": template.template_id,
                                     "split": template.split, "category": template.category,
                                     "ground_truth": cases.TRUTH[template.category], "attempts": []}
    for attempt in state["attempts"]:
        if attempt["status"] == "recording":
            attempt["status"] = "interrupted"  # the harness stopped mid-attempt; not a validity failure
    while state.get("status") not in ("valid", "no_valid_attempt"):
        if sum(1 for attempt in state["attempts"] if attempt["status"] == "invalid") >= MAX_INVALID_ATTEMPTS:
            state["status"] = "no_valid_attempt"
            break
        number = len(state["attempts"]) + 1
        seed = seed_for(case_run, number)
        controller = Controller(run_id=f"stage5-{case_run}-a{number}")
        attempt = {"attempt": number, "seed": seed, "controller_run": controller.run_id,
                   "bundle": opaque_name(case_run, number), "status": "recording"}
        state["attempts"].append(attempt)
        save_state(case_run, state)
        preflight(settings, store, controller)
        try:
            window = record_window(template, controller, accounts, seed)
            settle(window, template, store, settings)
            folder = freeze_bundle(window, store, settings, attempt["bundle"])
        finally:
            for component in template.stop:
                controller.restart_component(component)
        valid, reason = check_validity(template, load_bundle(folder))
        attempt |= {"window": window.as_json(), "status": "valid" if valid else "invalid", "validity": reason}
        print(f"{case_run} attempt {number}: {'valid' if valid else 'INVALID'} ({reason})", flush=True)
        if valid:
            state["status"] = "valid"
        save_state(case_run, state)
    if state["status"] == "valid" and not (state.get("A") and state.get("P")):
        attempt = next(a for a in state["attempts"] if a["status"] == "valid")
        folder = BUNDLES / attempt["bundle"]
        order = ["A", "P"]
        random.Random(attempt["seed"]).shuffle(order)
        state["system_order"] = order
        client = browser_client()  # a fresh session per case-run; a batch outlasts one session
        for system in order:
            if system == "A":
                state["A"] = run_a(folder)
            else:
                state["P"] = run_p(client, folder, template, f"stage5-{case_run}-a{attempt['attempt']}")
        save_state(case_run, state)
    return state


def replay_p(template: cases.Template, run: int, client) -> dict | None:
    """Runs P again on an existing valid bundle (after a recorded prompt or gateway change, §7)."""
    case_run = f"{template.template_id}-r{run}"
    state = load_state(case_run)
    if state.get("status") != "valid":
        return None
    attempt = next(a for a in state["attempts"] if a["status"] == "valid")
    tag = model_gateway.PROMPT_VERSION
    if state.get("P", {}).get("prompt_version") == tag or tag in state.get("P_replays", {}):
        return None
    result = run_p(client, BUNDLES / attempt["bundle"], template, f"stage5-{case_run}-a{attempt['attempt']}-{tag}")
    state.setdefault("P_replays", {})[tag] = result
    save_state(case_run, state)
    return result


def line(state: dict) -> dict:
    p = state.get("P") or {}
    return {"case_run": state["case_run"], "status": state.get("status"), "truth": state["ground_truth"],
            "A": (state.get("A") or {}).get("primary_cause"), "A_outcome": (state.get("A") or {}).get("outcome"),
            "P_outcome": (p.get("report") or {}).get("outcome"), "P_dev_aid": p.get("classified_dev_aid"),
            "P_status": p.get("status"), "P_cost": p.get("cost_usd"), "P_seconds": p.get("replay_elapsed_seconds")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "replay"])
    parser.add_argument("split", choices=["development", "held_out"])
    parser.add_argument("--only", help="comma-separated template ids")
    args = parser.parse_args()
    templates = cases.development() if args.split == "development" else cases.held_out()
    if args.only:
        wanted = set(args.only.split(","))
        templates = [template for template in templates if template.template_id in wanted]
    if args.split == "held_out" and (freeze.active() or ("",))[0] != "held_out":
        raise SystemExit("held-out runs need the held-out freeze (evaluation/freeze.py write held_out)")
    BUNDLES.mkdir(parents=True, exist_ok=True)  # the gateway refuses a replay directory that does not exist
    if os.environ.get("STAGE5_SKIP_GATEWAY_RESTART") != "1":
        stack = [sys.executable, str(PROJECT_ROOT / "scripts" / "stack.py"), "gateway"]
        subprocess.run(stack + ["down"], check=True, cwd=PROJECT_ROOT, capture_output=True)
        subprocess.run(stack + ["up", "--replay", str(BUNDLES)], check=True, cwd=PROJECT_ROOT, capture_output=True)
    settings = load_settings()
    plan = [(template, run) for template in templates for run in range(1, RUNS_PER_SPLIT[args.split] + 1)]
    random.Random(f"stage5:{args.split}").shuffle(plan)  # categories interleaved, order fixed per split
    if args.command == "replay":
        problems = freeze.check()  # protocol §8: no replay against a tree that differs from the freeze
        if problems:
            raise SystemExit("the tree differs from the active freeze: " + "; ".join(problems))
        for template, run in plan:
            result = replay_p(template, run, browser_client())
            if result:
                print(json.dumps({"case_run": f"{template.template_id}-r{run}", "truth": cases.TRUTH[template.category],
                                  "prompt_version": result["prompt_version"], "P_status": result.get("status"),
                                  "P_outcome": (result.get("report") or {}).get("outcome"),
                                  "P_dev_aid": result.get("classified_dev_aid"), "P_cost": result.get("cost_usd")}),
                      flush=True)
        return 0
    profile = json.loads((PROJECT_ROOT / "evaluation" / "healthy_profile.json").read_text())
    accounts = Controller(run_id="stage5-accounts").prepare_accounts(profile["account_pairs"], profile["seed"])
    with connect(settings.app_conninfo, autocommit=True) as store:
        for template, run in plan:
            state = run_case(template, run, accounts, settings, store)
            print(json.dumps(line(state)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
