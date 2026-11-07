"""Local configuration. Secrets live in ignored files (`.env.local`, `deploy/local/`) and are
never logged or put in prompts."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.conninfo import make_conninfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ENV_FILE = PROJECT_ROOT / ".env.local"
LOCAL_DEPLOY_DIR = PROJECT_ROOT / "deploy" / "local"
INCIDENTLENS_ENV_FILE = LOCAL_DEPLOY_DIR / "incidentlens.env"
LEDGERCORE_ENV_FILE = LOCAL_DEPLOY_DIR / "ledgercore.env"
LOG_DIR = PROJECT_ROOT / "logs"

# The one model the standard profile uses. Pinned by its API identifier (decisions D9);
# a change requires rerunning the model contract checks.
MODEL_ID = "claude-sonnet-5-5"

# Standard profile limits (spec §13).
MAX_INPUT_TOKENS_PER_CALL = 6_000
MAX_OUTPUT_TOKENS_PER_CALL = 1_500
MODEL_REQUEST_TIMEOUT_SECONDS = 25

# The one target service in v1, and the version stamped on collected telemetry.
SERVICE_ID = "ledgercore-local"
COLLECTOR_VERSION = "1.0"

# Fixed local ports. Everything listens on 127.0.0.1.
LEDGERCORE_PORT = 18080
TARGET_DB_PORT = 55432  # LedgerCore's PostgreSQL (Compose project incidentlens-target)
INCIDENTLENS_DB_PORT = 55433
PROMETHEUS_PORT = 19090
STUB_EVENTS_PORT = 18090  # fixture consumer: receives LedgerCore's deliveries
STUB_CONTROL_PORT = 18091  # fixture consumer: fault switches, used only by the controller
PUBLIC_PORT = 8088
INTERNAL_PORT = 8089

# Source limits (spec §9).
SOURCE_DB_STATEMENT_TIMEOUT_MS = 2_000
SOURCE_HTTP_CONNECT_TIMEOUT_S = 2.0
SOURCE_HTTP_READ_TIMEOUT_S = 5.0
TOOL_RESULT_MAX_BYTES = 64 * 1024

# Prometheus scrape interval (deploy/prometheus/prometheus.yml); gap detection depends on it.
SCRAPE_INTERVAL_SECONDS = 5.0

# Longest ledger_app transaction (transaction_timeout=10s, verified in LedgerCore's
# DiagnosticDatabaseAccessTest). Arrival buckets newer than this may still grow (decisions D3).
TARGET_SETTLE_SECONDS = 10.0


def load_local_env(path: Path = LOCAL_ENV_FILE) -> list[str]:
    """Reads KEY=VALUE lines into the process environment without overriding variables
    that are already set. Returns the names loaded, never the values."""
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")
            loaded.append(name)
    return loaded


@dataclass(frozen=True)
class Settings:
    """Where IncidentLens finds its own database and its sources. Connection strings carry
    passwords, so they are excluded from repr."""

    # IncidentLens's database: the owner runs migrations and provisioning only; the application
    # (gateway, supervisor, API) and the collector use their own non-superuser logins.
    owner_conninfo: str = field(repr=False)
    app_conninfo: str = field(repr=False)
    collector_conninfo: str = field(repr=False)
    target_conninfo: str = field(repr=False)
    prometheus_url: str
    ledgercore_url: str
    ledgercore_log_file: Path
    fixture_log_file: Path
    stub_control_url: str


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set: run `uv run python scripts/setup_local.py` first")
    return value


def load_settings() -> Settings:
    load_local_env()
    load_local_env(INCIDENTLENS_ENV_FILE)
    def incidentlens_db(user: str, password_variable: str, application: str) -> str:
        return make_conninfo(host="127.0.0.1", port=INCIDENTLENS_DB_PORT, dbname="incidentlens", user=user,
                             password=required(password_variable), application_name=application)

    return Settings(
        owner_conninfo=incidentlens_db("incidentlens", "INCIDENTLENS_DB_PASSWORD", "incidentlens-owner"),
        app_conninfo=incidentlens_db("incidentlens_app", "INCIDENTLENS_APP_DB_PASSWORD", "incidentlens-app"),
        collector_conninfo=incidentlens_db("incidentlens_collector", "INCIDENTLENS_COLLECTOR_DB_PASSWORD",
                                           "incidentlens-collector"),
        # The collector and tools read LedgerCore's database only as ledger_diag_reader.
        target_conninfo=make_conninfo(
            host="127.0.0.1", port=TARGET_DB_PORT, dbname="ledgercore",
            user="ledger_diag_reader", password=required("LEDGERCORE_DIAG_READER_PASSWORD"),
            application_name="incidentlens-diagnostics", connect_timeout="2"),
        prometheus_url=f"http://127.0.0.1:{PROMETHEUS_PORT}",
        ledgercore_url=f"http://127.0.0.1:{LEDGERCORE_PORT}",
        ledgercore_log_file=LOG_DIR / "ledgercore" / "ledgercore.json",
        fixture_log_file=LOG_DIR / "fixture-consumer" / "consumer.jsonl",
        stub_control_url=f"http://127.0.0.1:{STUB_CONTROL_PORT}",
    )
