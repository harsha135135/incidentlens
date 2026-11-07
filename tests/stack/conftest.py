"""Fixtures for tests that drive the live local stack (scripts/stack.py up). These tests take
minutes, inject faults through the evaluation controller, and compare what the tools report with
the controller's ground truth. FIXTURE RESULTS: the consumer is the test fixture unless a test
says otherwise. They fail, rather than skip, when the stack is not running."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from evaluation.controller import Controller, utc_now
from incidentlens.config import LEDGERCORE_PORT, LOG_DIR, PROJECT_ROOT, load_settings
from incidentlens.database import connect
from tests.stack.support import wait_for_empty_outbox

RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results" / "stage1"


@pytest.fixture(scope="session")
def settings():
    try:
        httpx.get(f"http://127.0.0.1:{LEDGERCORE_PORT}/actuator/health/readiness", timeout=2).raise_for_status()
    except httpx.HTTPError as error:
        pytest.fail(f"LedgerCore is not running ({error}); start it with: uv run python scripts/stack.py up")
    if not (LOG_DIR / "run" / "collector.pid").exists():
        pytest.fail("The collector is not running; start the stack with: uv run python scripts/stack.py up")
    return load_settings()


@pytest.fixture(scope="session")
def profile() -> dict:
    path = PROJECT_ROOT / "evaluation" / "healthy_profile.json"
    if not path.exists():
        pytest.fail("No healthy profile: run evaluation/calibrate.py first (decisions D8)")
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def store(settings):
    with connect(settings.app_conninfo, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def results_dir() -> Path:
    path = RESULTS_DIR / ("scenarios-" + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def accounts(settings, profile):
    return Controller(run_id="stage1-accounts-" + utc_now().strftime("%Y%m%dT%H%M%SZ")).prepare_accounts(
        profile["account_pairs"], profile["seed"])


@pytest.fixture
def controller(settings, request):
    controller = Controller(run_id=f"stage1-{request.node.name}-" + utc_now().strftime("%Y%m%dT%H%M%SZ"))
    controller.set_consumer_mode("normal")
    wait_for_empty_outbox(settings)
    yield controller
    controller.set_consumer_mode("normal")
