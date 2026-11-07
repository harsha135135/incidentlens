"""Evaluation controller: drives a seeded workload against LedgerCore and switches faults on and
off. It is the only component that knows the ground truth. It writes every action, with its time,
to evaluation/runs/<run-id>/controller.jsonl, which IncidentLens's gateway, runner and collector
never read. Its logins come from deploy/local/controller.env, which they never load.

Faults: the fixture consumer's unavailable, slow and reject modes; pausing LedgerCore's process (an
induced telemetry gap); row locks held by a separate (non-ledger_app) session (lock contention); and a
LedgerCore restart with a smaller connection pool (pool saturation)."""

from __future__ import annotations

import json
import os
import random
import signal
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
import subprocess
import sys

from incidentlens.config import LEDGERCORE_PORT, LOCAL_DEPLOY_DIR, LOG_DIR, PROJECT_ROOT, STUB_CONTROL_PORT, TARGET_DB_PORT

RUNS_DIR = PROJECT_ROOT / "evaluation" / "runs"
CUSTOMER_A_ID = "a0000000-0000-4000-8000-00000000000a"
CUSTOMER_B_ID = "b0000000-0000-4000-8000-00000000000b"
LEDGERCORE_DELIVERY_TIMEOUT_MS = 3_000  # LedgerCore's ledgercore.outbox.delivery-timeout


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("'").strip('"')
    return values


@dataclass
class WorkloadResult:
    sent: int = 0
    statuses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def as_json(self) -> dict:
        return {"sent": self.sent, "statuses": dict(self.statuses), "errors": dict(self.errors),
                "started_at": iso(self.started_at) if self.started_at else None,
                "ended_at": iso(self.ended_at) if self.ended_at else None}


class Controller:
    def __init__(self, run_id: str | None = None):
        secrets = read_env_file(LOCAL_DEPLOY_DIR / "controller.env")
        self.admin = ("demo-admin", secrets["CONTROLLER_ADMIN_PASSWORD"])
        self.customer = ("customer-a", secrets["CONTROLLER_CUSTOMER_A_PASSWORD"])
        self.target_owner_password = secrets["CONTROLLER_TARGET_OWNER_PASSWORD"]
        self.base_url = f"http://127.0.0.1:{LEDGERCORE_PORT}"
        self.run_id = run_id or utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.run_dir = RUNS_DIR / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.ground_truth = self.run_dir / "controller.jsonl"
        self.lock = threading.Lock()

    # Ground truth -----------------------------------------------------------------------------

    def record(self, action: str, **details) -> dict:
        entry = {"at": iso(utc_now()), "action": action, **details}
        with self.lock, self.ground_truth.open("a") as out:
            out.write(json.dumps(entry) + "\n")
        return entry

    # Workload ---------------------------------------------------------------------------------

    def prepare_accounts(self, pairs: int, seed: int) -> list[tuple[str, str]]:
        """Creates `pairs` source accounts for customer A (each funded) and destination accounts for
        customer B. Idempotency keys come from the seed, so a rerun with the same seed reuses them."""
        rng = random.Random(seed)
        created = []
        with httpx.Client(base_url=self.base_url, timeout=10) as http:
            for index in range(pairs):
                source = self.post(http, self.admin, "/v1/accounts", {"ownerId": CUSTOMER_A_ID}, rng)["accountId"]
                destination = self.post(http, self.admin, "/v1/accounts", {"ownerId": CUSTOMER_B_ID}, rng)["accountId"]
                self.post(http, self.admin, "/v1/demo-funding", {
                    "destinationAccountId": source, "amountMinor": "100000000", "currency": "INR",
                    "reason": f"evaluation workload {seed}"}, rng)
                created.append((source, destination))
        self.record("accounts_prepared", pairs=pairs, seed=seed)
        return created

    def post(self, http: httpx.Client, login, path: str, body: dict, rng: random.Random) -> dict:
        key = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        response = http.post(path, json=body, auth=login, headers={"Idempotency-Key": key})
        if response.status_code not in (200, 201):
            raise RuntimeError(f"{path} returned {response.status_code}")
        return response.json()

    def run_workload(self, accounts: list[tuple[str, str]], rate: float, seconds: float, seed: int,
                     stop: threading.Event | None = None) -> WorkloadResult:
        """Sends transfers at a fixed rate (scheduled times, so slow responses do not lower it)
        for `seconds` or until `stop` is set. Pairs and amounts come from the seed. Idempotency keys
        are unique to this run: keys derived from the seed alone would make a rerun replay the
        earlier run's stored responses, and LedgerCore would create no new transfers."""
        rng = random.Random(seed)
        result = WorkloadResult(started_at=utc_now())
        interval = 1.0 / rate
        begin = time.monotonic()
        stop = stop or threading.Event()

        def send(source: str, destination: str, amount: int, key: str) -> None:
            try:
                response = http.post("/v1/transfers", auth=self.customer, headers={"Idempotency-Key": key},
                                     json={"sourceAccountId": source, "destinationAccountId": destination,
                                           "amountMinor": str(amount), "currency": "INR"})
                with self.lock:
                    result.statuses[response.status_code] += 1
            except httpx.HTTPError as error:
                with self.lock:
                    result.errors[type(error).__name__] += 1

        with httpx.Client(base_url=self.base_url, timeout=10,
                          limits=httpx.Limits(max_connections=16)) as http, ThreadPoolExecutor(16) as pool:
            index = 0
            while not stop.is_set() and index * interval < seconds:
                due = begin + index * interval
                time.sleep(max(0.0, due - time.monotonic()))
                source, destination = accounts[rng.randrange(len(accounts))]
                amount = rng.randint(1, 5_000)
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"incidentlens:{self.run_id}:{seed}:{index}"))
                pool.submit(send, source, destination, amount, key)
                result.sent += 1
                index += 1
        result.ended_at = utc_now()
        return result

    def workload_in_background(self, accounts, rate: float, seed: int, seconds: float = 3600):
        """Starts the workload on a thread; returns (stop event, thread, result holder)."""
        stop = threading.Event()
        holder: dict = {}
        thread = threading.Thread(
            target=lambda: holder.setdefault("result", self.run_workload(accounts, rate, seconds, seed, stop)),
            daemon=True)
        thread.start()
        self.record("workload_started", rate_per_second=rate, seed=seed)
        return stop, thread, holder

    def stop_workload(self, stop: threading.Event, thread: threading.Thread, holder: dict) -> WorkloadResult:
        stop.set()
        thread.join(timeout=30)
        result = holder["result"]
        self.record("workload_stopped", **result.as_json())
        return result

    # Faults -----------------------------------------------------------------------------------

    def set_consumer_mode(self, mode: str, handler_delay_ms: int = 0) -> str:
        """Switches the fixture consumer's mode. Returns the time the switch was requested (taken
        before the request, so nothing the switch causes can appear to precede it)."""
        if mode == "slow" and handler_delay_ms >= LEDGERCORE_DELIVERY_TIMEOUT_MS:
            raise ValueError("a slow consumer must stay below LedgerCore's 3 s delivery timeout, or it "
                             "becomes indistinguishable from an unavailable one")
        requested_at = iso(utc_now())
        response = httpx.post(f"http://127.0.0.1:{STUB_CONTROL_PORT}/mode",
                              json={"mode": mode, "handler_delay_ms": handler_delay_ms}, timeout=5)
        response.raise_for_status()
        self.record("consumer_mode", mode=mode, handler_delay_ms=handler_delay_ms, requested_at=requested_at)
        return requested_at

    def pause_ledgercore(self, seconds: float) -> tuple[datetime, datetime]:
        """Freezes LedgerCore's process (SIGSTOP), so scrapes fail and nothing is logged, then
        resumes it. Returns the exact pause interval."""
        pid = int((LOG_DIR / "run" / "ledgercore.pid").read_text())
        os.kill(pid, signal.SIGSTOP)
        paused = self.record("ledgercore_paused")
        try:
            time.sleep(seconds)
        finally:
            os.kill(pid, signal.SIGCONT)
        resumed = self.record("ledgercore_resumed")
        return datetime.fromisoformat(paused["at"].replace("Z", "+00:00")), \
            datetime.fromisoformat(resumed["at"].replace("Z", "+00:00"))

    def hold_account_locks(self, accounts: list[str], seconds: float) -> tuple[datetime, datetime]:
        """Holds row locks on `accounts` from a separate owner session (not ledger_app) for `seconds`.
        Transfers touching those accounts wait, then fail at LedgerCore's 2 s lock timeout."""
        with psycopg.connect(host="127.0.0.1", port=TARGET_DB_PORT, dbname="ledgercore", user="ledger_owner",
                             password=self.target_owner_password, application_name="maintenance",
                             autocommit=False) as conn:
            conn.execute("SELECT id FROM accounts WHERE id = ANY(%s::uuid[]) FOR UPDATE", (accounts,))
            held = self.record("account_locks_held", accounts=len(accounts))
            time.sleep(seconds)
            conn.rollback()
        released = self.record("account_locks_released")
        return datetime.fromisoformat(held["at"].replace("Z", "+00:00")), \
            datetime.fromisoformat(released["at"].replace("Z", "+00:00"))

    def restart_ledgercore(self, pool_size: int | None = None, connection_timeout_ms: int | None = None) -> str:
        """Restarts LedgerCore (fixture consumer), optionally with a smaller pool. Returns when it is ready."""
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "stack.py"), "restart-ledgercore", "--consumer", "fixture"]
        if pool_size is not None:
            command += ["--pool-size", str(pool_size)]
        if connection_timeout_ms is not None:
            command += ["--connection-timeout-ms", str(connection_timeout_ms)]
        requested = iso(utc_now())
        subprocess.run(command, check=True, cwd=PROJECT_ROOT, capture_output=True)
        self.record("ledgercore_restarted", pool_size=pool_size, connection_timeout_ms=connection_timeout_ms,
                    requested_at=requested)
        return requested


    # ---- Distractors (benign by construction, protocol §6) and missing-evidence switches ----

    def distractor_in_background(self, kind: str, accounts, seconds: float, seed: int, base_rate: float):
        """Starts a benign distractor for `seconds`: overspend (HTTP 422), replays (already-used
        idempotency keys) or a load bump (extra transfers keeping the total under 3.8/s)."""
        stop = threading.Event()
        errors = {"count": 0}

        def post(http: httpx.Client, key: str, body: dict) -> None:
            # LedgerCore restarts during lock-pool cases; like the workload, a distractor keeps going.
            try:
                http.post("/v1/transfers", auth=self.customer, headers={"Idempotency-Key": key}, json=body)
            except httpx.HTTPError:
                errors["count"] += 1

        def overspend() -> None:
            with httpx.Client(base_url=self.base_url, timeout=10) as http:
                index = 0
                while not stop.wait(3.3):  # about 0.3/s
                    source, destination = accounts[index % len(accounts)]
                    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"overspend:{self.run_id}:{index}"))
                    post(http, key, {"sourceAccountId": source, "destinationAccountId": destination,
                                     "amountMinor": "900000000000", "currency": "INR"})
                    index += 1

        def replays() -> None:
            with httpx.Client(base_url=self.base_url, timeout=10) as http:
                source, destination = accounts[0]
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"replay:{self.run_id}"))
                body = {"sourceAccountId": source, "destinationAccountId": destination, "amountMinor": "7",
                        "currency": "INR"}
                post(http, key, body)
                while not stop.wait(3.3):
                    post(http, key, body)

        def load_bump() -> None:
            extra = max(0.1, 3.8 - base_rate)
            self.run_workload(accounts, extra, seconds, seed + 991, stop)

        runners = {"overspend": [overspend], "replays": [replays], "load_bump": [load_bump],
                   "overspend_and_replays": [overspend, replays]}[kind]
        threads = [threading.Thread(target=runner, daemon=True) for runner in runners]
        for thread in threads:
            thread.start()
        self.record("distractor_started", kind=kind)

        def finish() -> None:
            stop.set()
            self.record("distractor_stopped", kind=kind, request_errors=errors["count"])

        timer = threading.Timer(seconds, finish)
        timer.start()
        return stop, threads

    def stop_component(self, name: str) -> None:
        """Missing evidence: stops the collector (no blocker samples or log ingestion) or Prometheus
        (no scrapes) until restart_component."""
        if name == "collector":
            subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import stack; "
                            "stack.stop_process('collector')"], check=True, cwd=PROJECT_ROOT, capture_output=True)
        else:
            subprocess.run(compose_command("stop", "prometheus"), check=True, cwd=PROJECT_ROOT, capture_output=True)
        self.record("component_stopped", component=name)

    def restart_component(self, name: str) -> None:
        if name == "collector":
            subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import stack; "
                            "stack.start_collector()"], check=True, cwd=PROJECT_ROOT, capture_output=True)
        else:
            subprocess.run(compose_command("start", "prometheus"), check=True, cwd=PROJECT_ROOT, capture_output=True)
        self.record("component_restarted", component=name)


def compose_command(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(PROJECT_ROOT / "deploy" / "compose.yaml"), "--env-file",
            str(LOCAL_DEPLOY_DIR / "incidentlens.env"), *args]
