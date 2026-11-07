"""Harness for the §17 process tests: the real gateway (uvicorn) and supervisor run as separate
processes, in test mode, against a fresh test database reached through a proxy the test can cut.
The provider and Prometheus are the loopback fake (tests/support/fake_provider.py), whose request
log is the evidence of what work was or was not started. Crashes are real signals (SIGKILL,
SIGSTOP), not exceptions. The runner runs in the labelled unsandboxed development mode (D14)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from incidentlens.config import LOG_DIR, PROJECT_ROOT
from tests.support.fake_provider import FakeProvider
from tests.support.tcp_proxy import TcpProxy

PUBLIC = "http://127.0.0.1:8088"
BROWSER_HEADERS = {"Origin": PUBLIC, "Host": "127.0.0.1:8088"}


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_until(check, what: str, timeout: float = 30.0, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


class Process:
    """A component started with `python -m incidentlens.<module>`, with its own log file."""

    def __init__(self, module: str, env: dict, log_dir: Path, args: list[str] | None = None):
        self.module, self.env, self.log_dir = module, env, log_dir
        self.args = args
        self.process: subprocess.Popen | None = None

    def start(self) -> "Process":
        log = (self.log_dir / f"{self.module}.log").open("ab")
        # The process tests use the labelled unsandboxed development runner (D14); sandboxed runs are Stage 3.
        extra = self.args if self.args is not None else (["--runner", "local"] if self.module == "supervisor" else [])
        self.process = subprocess.Popen([sys.executable, "-m", f"incidentlens.{self.module}", *extra], env=self.env,
                                        stdout=log, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT, start_new_session=True)
        return self

    def kill(self, sig=signal.SIGKILL) -> None:
        if self.process and self.process.poll() is None:
            os.kill(self.process.pid, sig)
            if sig in (signal.SIGKILL, signal.SIGTERM):
                self.process.wait(timeout=15)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


@pytest.fixture
def fake():
    provider = FakeProvider().start()
    yield provider
    provider.stop()


@pytest.fixture
def proxy(settings):
    params = conninfo_to_dict(settings.app_conninfo)
    db_proxy = TcpProxy(params["host"], int(params["port"]))
    yield db_proxy
    db_proxy.close()


@pytest.fixture
def env(test_database, fake, proxy, tmp_path):
    params = conninfo_to_dict(test_database["app"])
    params["port"] = str(proxy.port)
    environment = {
        "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", ""), "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "INCIDENTLENS_TEST_MODE": "1",
        "INCIDENTLENS_TEST_PROVIDER_URL": fake.url,
        "INCIDENTLENS_TEST_PROMETHEUS_URL": fake.url,
        "INCIDENTLENS_APP_CONNINFO_OVERRIDE": make_conninfo(**params),
    }
    return environment


@pytest.fixture
def gateway(env, owner, tmp_path):
    if not (port_free(8088) and port_free(8089)):
        pytest.fail("ports 8088/8089 are in use: stop any running IncidentLens server before these tests")
    server = Process("server", env, tmp_path).start()
    wait_until(lambda: not port_free(8088) and not port_free(8089), "the gateway to listen", 20)
    yield server
    server.stop()


@pytest.fixture
def supervisor(env, gateway, owner, tmp_path):
    process = Process("supervisor", env, tmp_path).start()
    wait_until(lambda: owner.execute("SELECT supervisor_heartbeat_at IS NOT NULL FROM control").fetchone()[0],
               "the supervisor's first heartbeat", 20)
    yield process
    process.stop()
    runner_pid = LOG_DIR / "run" / "runner.pid"
    if runner_pid.exists():
        try:
            os.kill(int(runner_pid.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def client(gateway):
    """A signed-in browser-like client (canonical Host and Origin, CSRF token attached)."""
    secret = (PROJECT_ROOT / "deploy" / "local" / "access-secret.txt").read_text().strip()
    http = httpx.Client(base_url=PUBLIC, headers=BROWSER_HEADERS, timeout=10)
    response = http.post("/api/v1/session", json={"secret": secret})
    assert response.status_code == 204, response.text
    http.headers["X-CSRF-Token"] = http.get("/api/v1/session").json()["csrf_token"]
    yield http
    http.close()


def create_job(client: httpx.Client, symptom: str = "Notifications arrive late") -> str:
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    response = client.post("/api/v1/investigations", headers={"Idempotency-Key": "test-" + uuid.uuid4().hex},
                           json={"service_id": "ledgercore-local", "symptom": symptom,
                                 "window": {"start": (end - timedelta(minutes=5)).isoformat(),
                                            "end": end.isoformat()}})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def job_row(owner: psycopg.Connection, job_id: str):
    return owner.execute("SELECT status, generation, recoveries, model_actions, tool_actions, tokens_reserved, "
                         "final_reason FROM investigations WHERE id = %s", (job_id,)).fetchone()


def runner_pid() -> int:
    return int((LOG_DIR / "run" / "runner.pid").read_text())


def report_step(*handles: str) -> dict:
    return {"reply": "tool", "name": "submit_report", "input": {
        "outcome": "TENTATIVE", "summary": "Publications fell behind arrivals in the window.",
        "findings": [{"claim": "Recorded publications were below arrivals.", "support": "observed",
                      "evidence_ids": list(handles), "reason": "Durable counts for the same window."}],
        "alternatives": [], "limitations": [], "next_checks": []}}
