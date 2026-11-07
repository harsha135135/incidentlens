"""get_database_blockers and get_runtime_config over records in real PostgreSQL. Includes the §17
item: a historical blocker query never substitutes current database state without explicit mode."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from incidentlens.actions import DatabaseBlockerArguments, NoArguments
from incidentlens.config import load_settings
from incidentlens.tools.blockers import get_database_blockers
from incidentlens.tools.config_snapshot import get_runtime_config
from incidentlens.tools.envelope import Window
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import PrometheusClient

T0 = datetime(2026, 11, 6, 5, 0, 0, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(minutes=2))


def context(store, frozen=False) -> ToolContext:
    return ToolContext(WINDOW, load_settings(), store, PrometheusClient("http://127.0.0.1:9"), frozen=frozen)


def edge(blocked: int, blocker: int, role: str, wait: float) -> dict:
    return {"observed_at": "x", "blocked_pid": blocked, "blocked_wait_event_type": "Lock",
            "blocked_wait_event": "transactionid", "blocked_wait_seconds": wait, "blocker_pid": blocker,
            "blocker_role": role, "blocker_state": "idle in transaction", "blocker_transaction_seconds": wait + 1}


def sample(owner, seconds: float, edges: list, coverage: str = "COMPLETE") -> None:
    payload = {"edge_count": len(edges), "truncated": False, "edges": edges} if coverage == "COMPLETE" else \
        {"error": "OperationalError", "detail": "connection refused"}
    owner.execute("INSERT INTO telemetry_samples (service, kind, observed_at, collector_version, coverage, payload) "
                  "VALUES ('ledgercore-local', 'db_blockers', %s, '1.0', %s, %s)",
                  (T0 + timedelta(seconds=seconds), coverage, Jsonb(payload)))


def historical(store, limit=50) -> dict:
    return get_database_blockers(context(store), DatabaseBlockerArguments(mode="historical", limit=limit))


def test_samples_are_aggregated_into_edges_with_roles_and_waits(owner, app_store):
    for second in range(0, 120, 2):
        edges = [edge(101, 900, "other", 0.5 + (second - 40) / 2)] if 40 <= second < 60 else []
        sample(owner, second, edges)
    envelope = historical(app_store)
    data = envelope["data"]
    assert envelope["coverage"] == "COMPLETE"
    assert (data["relationships"], data["blocked_sessions"], data["blocker_roles"]) == (1, 1, {"other": 1})
    assert data["samples"]["with_blocking"] == 10 and data["edges"][0]["samples"] == 10
    assert data["first_blocking_at"] == "2026-11-06T05:00:40.000Z"
    assert data["max_blocked_wait_seconds"] == 9.5
    assert any("sampled every 2 s" in note for note in envelope["limitations"])


def test_gaps_and_failed_samples_make_coverage_partial(owner, app_store):
    for second in list(range(0, 30, 2)) + list(range(50, 120, 2)):
        sample(owner, second, [])
    sample(owner, 31, [], "UNAVAILABLE")
    envelope = historical(app_store)
    assert envelope["coverage"] == "PARTIAL"
    assert envelope["data"]["samples"]["failed"] == 1
    assert envelope["data"]["samples"]["gaps"][0]["start"] == "2026-11-06T05:00:28.000Z"


def test_a_historical_query_without_samples_is_unavailable_never_a_live_read(owner, app_store):
    envelope = historical(app_store)
    assert envelope["coverage"] == "UNAVAILABLE"
    assert envelope["data"]["mode"] == "historical" and envelope["source"]["kind"] == "postgres_samples"


def test_current_mode_is_labelled_as_outside_the_window_and_refused_from_a_frozen_bundle(owner, app_store):
    live = get_database_blockers(context(app_store), DatabaseBlockerArguments(mode="current", limit=10))
    assert live["data"]["mode"] == "current" and live["data"]["outside_requested_window"] is True
    assert any("follow-up evidence" in note for note in live["limitations"])
    frozen = get_database_blockers(context(app_store, frozen=True), DatabaseBlockerArguments(mode="current", limit=10))
    assert frozen["coverage"] == "UNAVAILABLE"


def test_edges_are_listed_up_to_the_limit(owner, app_store):
    sample(owner, 0, [edge(100 + i, 900, "ledger_app", 1.0) for i in range(5)])
    envelope = historical(app_store, limit=2)
    assert envelope["data"]["relationships"] == 5 and len(envelope["data"]["edges"]) == 2
    assert any("first 2 of 5" in note for note in envelope["limitations"])


def config_record(owner, seconds: float, pool: int) -> None:
    file_id = owner.execute("INSERT INTO log_files (service, source, path, device, inode, state) VALUES "
                            "('ledgercore-local', 'ledgercore', '/x', 1, %s, 'FINISHED') RETURNING id",
                            (int(seconds * 10) + 7,)).fetchone()[0]
    owner.execute("INSERT INTO log_records (source_file_id, byte_offset, service, source, observed_at, record_type, "
                  "fields) VALUES (%s, 0, 'ledgercore-local', 'ledgercore', %s, 'ledgercore.config', %s)",
                  (file_id, T0 + timedelta(seconds=seconds), Jsonb({"record": "ledgercore.config", "revision": "r1",
                                                                    "instanceId": f"i{seconds}", "poolMaximumSize": pool,
                                                                    "outboxBatchSize": 20, "password": "never"})))


def test_runtime_config_shows_the_version_in_effect_and_a_restart_inside_the_window(owner, app_store):
    config_record(owner, -600, 10)
    config_record(owner, 45, 1)
    config_record(owner, 300, 99)  # after the window: never presented as applying to it
    envelope = get_runtime_config(context(app_store), NoArguments())
    versions = envelope["data"]["versions"]
    assert [v["settings"]["poolMaximumSize"] for v in versions] == [10, 1]
    assert envelope["data"]["restarts_in_window"] == ["2026-11-06T05:00:45.000Z"]
    assert any("poolMaximumSize" in note for note in envelope["limitations"])
    assert "password" not in str(envelope)


def test_runtime_config_without_any_record_is_unavailable(owner, app_store):
    assert get_runtime_config(context(app_store), NoArguments())["coverage"] == "UNAVAILABLE"
