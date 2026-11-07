"""Shared fixtures: tests run against the real IncidentLens PostgreSQL container (deploy/compose.yaml).
Each test session gets its own freshly migrated database, dropped afterwards. Tests connect
through the role each component really uses: the collector role, the application role, or the
owner for test setup only."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from incidentlens.config import INCIDENTLENS_ENV_FILE, load_local_env, load_settings
from incidentlens.database import apply_migrations, provision_logins


def in_database(conninfo: str, name: str) -> str:
    params = conninfo_to_dict(conninfo)
    params["dbname"] = name
    return make_conninfo(**params)


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def test_database(settings) -> dict[str, str]:
    """A fresh database; returns connection strings for each role, all pointing at it."""
    name = "incidentlens_test_" + uuid.uuid4().hex[:10]
    try:
        admin = psycopg.connect(settings.owner_conninfo, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as error:
        pytest.fail(f"IncidentLens PostgreSQL is not reachable ({error}); start deploy/compose.yaml first")
    with admin:
        admin.execute(f"CREATE DATABASE {name}")
    conninfos = {role: in_database(getattr(settings, f"{role}_conninfo"), name)
                 for role in ("owner", "app", "collector")}
    apply_migrations(conninfos["owner"])
    # Roles are cluster-wide; provisioning uses the same passwords as the live stack, so it is harmless.
    load_local_env(INCIDENTLENS_ENV_FILE)
    import os
    provision_logins(conninfos["owner"], {"incidentlens_app": os.environ["INCIDENTLENS_APP_DB_PASSWORD"],
                                          "incidentlens_collector": os.environ["INCIDENTLENS_COLLECTOR_DB_PASSWORD"]})
    yield conninfos
    with psycopg.connect(settings.owner_conninfo, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def owner(test_database):
    """Owner connection, for test setup and inspection only. Empties every table first."""
    with psycopg.connect(test_database["owner"], autocommit=True) as conn:
        conn.execute("TRUNCATE log_records, log_files, telemetry_samples, progress_events, evidence, reports, "
                     "actions, submission_keys, investigations, sessions, login_attempts RESTART IDENTITY CASCADE")
        conn.execute("UPDATE control SET supervisor_instance = NULL, supervisor_heartbeat_at = NULL")
        yield conn


@pytest.fixture
def collector_store(test_database, owner):
    with psycopg.connect(test_database["collector"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_store(test_database, owner):
    with psycopg.connect(test_database["app"], autocommit=True) as conn:
        yield conn
