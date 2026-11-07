"""Log tools over records stored in real PostgreSQL: the delivery outcome mapping, ordering and
limits, and coverage that reflects what the collector actually confirmed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from incidentlens.actions import DeliveryLogArguments, NoArguments
from incidentlens.config import load_settings
from incidentlens.tools.envelope import Window
from incidentlens.tools.logs import get_delivery_timings, search_delivery_logs
from incidentlens.tools.registry import ToolContext
from incidentlens.tools.sources import PrometheusClient

T0 = datetime(2026, 11, 6, 5, 0, 0, tzinfo=timezone.utc)
WINDOW = Window(T0, T0 + timedelta(minutes=5))

# (LedgerCore result, HTTP status) -> expected outcome category
CASES = [
    ("SUCCESS", 200, "success"),
    ("SUCCESS", 204, "success"),
    ("HTTP_401", 401, "auth_error"),
    ("HTTP_403", 403, "auth_error"),
    ("TIMEOUT", 0, "network_error"),
    ("CONNECTION_FAILED", 0, "network_error"),
    ("HTTP_500", 500, "server_error"),
    ("HTTP_503", 503, "server_error"),
    ("HTTP_409", 409, "other"),
    ("INTERRUPTED", 0, "other"),
]


def context(store) -> ToolContext:
    return ToolContext(WINDOW, load_settings(), store, PrometheusClient("http://127.0.0.1:9"))


def add_file(store, source: str) -> int:
    return store.execute("INSERT INTO log_files (service, source, path, device, inode, state) "
                         "VALUES ('ledgercore-local', %s, '/x', 1, %s, 'ACTIVE') RETURNING id",
                         (source, abs(hash(source)) % 100000)).fetchone()[0]


def add_record(store, file_id: int, offset: int, source: str, record_type: str, observed_at, event_id, fields):
    store.execute("INSERT INTO log_records (source_file_id, byte_offset, service, source, observed_at, record_type, "
                  "event_id, fields) VALUES (%s, %s, 'ledgercore-local', %s, %s, %s, %s, %s)",
                  (file_id, offset, source, observed_at, record_type, event_id, Jsonb(fields)))


def add_heartbeat(store, observed_at, lag_by_source: dict):
    sources = {name: {"file_id": 1, "lag_bytes": lag} for name, lag in lag_by_source.items()}
    store.execute("INSERT INTO telemetry_samples (service, kind, observed_at, collector_version, coverage, payload) "
                  "VALUES ('ledgercore-local', 'collector_heartbeat', %s, '1.0', 'COMPLETE', %s)",
                  (observed_at, Jsonb({"sources": sources})))


def add_config(store, file_id: int, consumer_target: str):
    add_record(store, file_id, 999_999, "ledgercore", "ledgercore.config", T0 - timedelta(minutes=1), None,
               {"record": "ledgercore.config", "revision": "86e4bf1+dirty.test", "instanceId": "i-1",
                "consumerTarget": consumer_target})


def test_every_result_maps_to_its_outcome_category(owner, app_store):
    file_id = add_file(owner, "ledgercore")
    add_config(owner, file_id, "http://127.0.0.1:18090/events")
    for i, (result, status, _) in enumerate(CASES):
        add_record(owner, file_id, i, "ledgercore", "outbox.delivery_attempt", T0 + timedelta(seconds=i), f"e{i}",
                   {"result": result, "httpStatus": status, "durationMs": 10.0, "attempt": 1})
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0})

    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=100))
    by_event = {row["event_id"]: row["outcome"] for row in envelope["data"]["records"]}
    assert by_event == {f"e{i}": expected for i, (_, _, expected) in enumerate(CASES)}
    assert envelope["data"]["counts_by_outcome"] == {"success": 2, "auth_error": 2, "network_error": 2,
                                                     "server_error": 2, "other": 2}
    assert envelope["coverage"] == "COMPLETE"
    assert envelope["source"]["target_revision"] == "86e4bf1+dirty.test"

    errors = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="network_error", limit=1))
    assert errors["data"]["matching_records"] == 2 and errors["data"]["returned_records"] == 1
    assert errors["data"]["records"][0]["result"] == "TIMEOUT"  # earliest first
    assert any("Only the earliest 1 of 2" in note for note in errors["limitations"])


def test_records_outside_the_window_are_excluded(owner, app_store):
    file_id = add_file(owner, "ledgercore")
    add_config(owner, file_id, "in-process")
    add_record(owner, file_id, 1, "ledgercore", "outbox.delivery_attempt", WINDOW.end, "late",
               {"result": "SUCCESS", "httpStatus": 200})
    add_record(owner, file_id, 2, "ledgercore", "outbox.delivery_attempt", WINDOW.start - timedelta(microseconds=1),
               "early", {"result": "SUCCESS", "httpStatus": 200})
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0})
    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=100))
    assert envelope["data"]["matching_records"] == 0
    assert envelope["coverage"] == "COMPLETE"  # nothing recorded, and the log was read past the end


def test_coverage_is_partial_until_the_collector_has_read_past_the_window_end(owner, app_store):
    file_id = add_file(owner, "ledgercore")
    add_config(owner, file_id, "in-process")
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 512})  # still behind
    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=10))
    assert envelope["coverage"] == "PARTIAL"
    assert any("not yet confirmed reading" in note for note in envelope["limitations"])


def test_a_log_discontinuity_makes_coverage_partial(owner, app_store):
    file_id = add_file(owner, "ledgercore")
    add_config(owner, file_id, "in-process")
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0})
    owner.execute("INSERT INTO telemetry_samples (service, kind, observed_at, collector_version, coverage, payload) "
                  "VALUES ('ledgercore-local', 'log_gap', %s, '1.0', 'UNAVAILABLE', %s)",
                  (T0 + timedelta(minutes=2), Jsonb({"source": "ledgercore", "reason": "truncated"})))
    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=10))
    assert envelope["coverage"] == "PARTIAL"
    assert envelope["data"]["ingestion"]["discontinuities"][0]["reason"] == "truncated"


def test_a_log_never_read_is_unavailable(owner, app_store):
    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=10))
    assert envelope["coverage"] == "UNAVAILABLE"


def test_timings_split_measured_consumer_time_from_the_unattributed_rest(owner, app_store):
    ledgercore = add_file(owner, "ledgercore")
    fixture = add_file(owner, "fixture_consumer")
    add_config(owner, ledgercore, "http://127.0.0.1:18090/events")
    for i in range(3):
        start = T0 + timedelta(seconds=10 * i)
        add_record(owner, ledgercore, i, "ledgercore", "outbox.delivery_attempt", start, f"e{i}",
                   {"result": "SUCCESS", "httpStatus": 200, "durationMs": 260.0,
                    "endedAt": (start + timedelta(milliseconds=260)).isoformat()})
        add_record(owner, fixture, i, "fixture_consumer", "fixture.delivery", start + timedelta(milliseconds=5),
                   f"e{i}", {"auth_ms": 55.0, "handler_ms": 200.0, "total_ms": 255.0})
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0, "fixture_consumer": 0})

    data = get_delivery_timings(context(app_store), NoArguments())["data"]
    assert data["attempts"] == 3 and data["correlated_attempts"] == 3
    assert data["consumer_handler_ms"]["p50"] == 200.0
    assert data["consumer_auth_ms"]["p50"] == 55.0
    assert data["unattributed_ms"]["p50"] == 5.0
    assert data["consumer_measured_by"] == "fixture consumer (test fixture)"


def test_timings_for_the_in_process_consumer_say_auth_and_handler_are_not_measured(owner, app_store):
    ledgercore = add_file(owner, "ledgercore")
    add_config(owner, ledgercore, "in-process")
    add_record(owner, ledgercore, 1, "ledgercore", "outbox.delivery_attempt", T0, "e1",
               {"result": "SUCCESS", "httpStatus": 200, "durationMs": 70.0})
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0})
    envelope = get_delivery_timings(context(app_store), NoArguments())
    assert envelope["coverage"] == "PARTIAL"
    assert envelope["data"]["consumer_auth_ms"]["count"] == 0
    assert any("no authentication or handler timer" in note for note in envelope["limitations"])


def test_missing_delivery_records_are_detected_from_the_poll_batches(owner, app_store):
    """The failure Stage 1 found: LedgerCore delivered events, but its delivery records were
    dropped. An empty result must then be PARTIAL, not COMPLETE "no deliveries"."""
    file_id = add_file(owner, "ledgercore")
    add_config(owner, file_id, "http://127.0.0.1:18090/events")
    for i in range(5):
        add_record(owner, file_id, i, "ledgercore", "outbox.poll_batch", T0 + timedelta(seconds=10 * i), None,
                   {"record": "outbox.poll_batch", "selected": 20, "published": 20, "failed": 0})
    add_heartbeat(owner, WINDOW.end + timedelta(seconds=3), {"ledgercore": 0})

    envelope = search_delivery_logs(context(app_store), DeliveryLogArguments(outcome="any", limit=10))
    assert envelope["coverage"] == "PARTIAL"
    assert envelope["data"]["record_check"] == {"attempt_records": 0, "attempts_reported_by_poll_batches": 100}
    assert any("look incomplete" in note for note in envelope["limitations"])
    assert get_delivery_timings(context(app_store), NoArguments())["coverage"] == "PARTIAL"
