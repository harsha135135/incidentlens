"""Collector behaviour against real PostgreSQL and real files: every complete line stored once,
offsets advanced in the same transaction, rotation and truncation followed, and every
discontinuity recorded instead of silently skipped."""

from __future__ import annotations

import json
import os

from incidentlens.collector import BlockerSampler, LogSource, LogTailer, parse_fixture_line, parse_ledgercore_line


def ledgercore_line(n: int, **extra) -> str:
    record = {"@timestamp": f"2026-11-06T05:00:{n:02d}.000000Z", "log": {"level": "INFO", "logger": "x"},
              "message": f"line {n}", "process": {"pid": 1}, **extra}
    return json.dumps(record) + "\n"


def rows(store, source="ledgercore"):
    return store.execute("SELECT f.id, r.byte_offset, r.fields->>'message' FROM log_records r "
                         "JOIN log_files f ON f.id = r.source_file_id WHERE r.source = %s ORDER BY r.id",
                         (source,)).fetchall()


def gaps(store):
    return [row[0] for row in store.execute(
        "SELECT payload->>'reason' FROM telemetry_samples WHERE kind = 'log_gap' ORDER BY id").fetchall()]


def tailer_for(store, path):
    return LogTailer(store, LogSource("ledgercore", path, parse_ledgercore_line))


def test_complete_lines_are_stored_once_and_partial_lines_wait(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1) + ledgercore_line(2))
    tailer = tailer_for(collector_store, path)
    tailer.poll()
    tailer.poll()
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 2"]

    with path.open("a") as f:
        f.write(ledgercore_line(3)[:20])  # half a line
    tailer.poll()
    assert len(rows(collector_store)) == 2
    with path.open("a") as f:
        f.write(ledgercore_line(3)[20:])
    tailer.poll()
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 2", "line 3"]
    saved = collector_store.execute("SELECT read_offset FROM log_files").fetchone()[0]
    assert saved == path.stat().st_size


def test_a_restarted_collector_resumes_from_the_saved_offset(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1))
    tailer_for(collector_store, path).poll()
    with path.open("a") as f:
        f.write(ledgercore_line(2))
    tailer_for(collector_store, path).poll()  # a new tailer object, as after a restart
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 2"]
    assert collector_store.execute("SELECT count(*) FROM log_files").fetchone()[0] == 1


def test_rotation_finishes_the_old_file_then_follows_the_new_one(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1))
    tailer = tailer_for(collector_store, path)
    tailer.poll()
    with path.open("a") as f:
        f.write(ledgercore_line(2))  # written just before rotation, not yet read
    path.rename(tmp_path / "ledgercore.json.2026-11-06.0")
    path.write_text(ledgercore_line(3))
    tailer.poll()  # finishes the rotated file
    tailer.poll()  # opens and reads the new file
    stored = rows(collector_store)
    assert [r[2] for r in stored] == ["line 1", "line 2", "line 3"]
    assert stored[0][0] == stored[1][0] != stored[2][0]
    states = collector_store.execute("SELECT state FROM log_files ORDER BY id").fetchall()
    assert [s[0] for s in states] == ["FINISHED", "ACTIVE"]
    assert gaps(collector_store) == []


def test_rotation_while_stopped_is_finished_from_the_rotated_file(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1))
    tailer_for(collector_store, path).poll()
    with path.open("a") as f:
        f.write(ledgercore_line(2))
    path.rename(tmp_path / "ledgercore.json.2026-11-06.0")
    path.write_text(ledgercore_line(3))
    tailer = tailer_for(collector_store, path)  # collector restarts after the rotation
    tailer.poll()
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 2", "line 3"]
    assert gaps(collector_store) == []


def test_a_rotated_file_that_is_gone_is_recorded_as_a_gap(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1))
    tailer_for(collector_store, path).poll()
    path.unlink()
    path.write_text(ledgercore_line(5))
    tailer_for(collector_store, path).poll()
    assert gaps(collector_store) == ["rotated_or_truncated_while_stopped"]
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 5"]


def test_truncation_is_recorded_and_reading_restarts(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1) + ledgercore_line(2))
    tailer = tailer_for(collector_store, path)
    tailer.poll()
    with path.open("r+") as f:
        f.truncate(0)
    with path.open("a") as f:
        f.write(ledgercore_line(7))
    tailer.poll()  # notices the truncation
    tailer.poll()  # reads from the start under a new file row
    assert gaps(collector_store) == ["truncated"]
    assert [r[2] for r in rows(collector_store)] == ["line 1", "line 2", "line 7"]
    assert collector_store.execute("SELECT count(*) FROM log_files").fetchone()[0] == 2


def test_a_missing_file_is_recorded_once_until_it_appears(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    tailer = tailer_for(collector_store, path)
    tailer.poll()
    tailer.poll()
    assert gaps(collector_store) == ["missing"]
    path.write_text(ledgercore_line(1))
    tailer.poll()
    assert [r[2] for r in rows(collector_store)] == ["line 1"]


def test_credentials_are_redacted_before_storage(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(1, message="retry with password=hunter2 and Basic dXNlcjpzZWNyZXQ=")
                    .replace('"line 1"', '"x"'))
    tailer_for(collector_store, path).poll()
    stored = collector_store.execute("SELECT fields::text FROM log_records").fetchone()[0]
    assert "hunter2" not in stored and "dXNlcjpzZWNyZXQ=" not in stored
    assert "[REDACTED]" in stored


def test_diagnostic_records_keep_their_fields_and_event_time(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    path.write_text(ledgercore_line(9, record="outbox.delivery_attempt", outboxEventId="e-1", eventId="e-1", attempt=1,
                                    startedAt="2026-11-06T05:00:08.500000Z", result="HTTP_503", httpStatus=503))
    tailer_for(collector_store, path).poll()
    record_type, event_id, observed_at, fields = collector_store.execute(
        "SELECT record_type, event_id, observed_at, fields FROM log_records").fetchone()
    assert (record_type, event_id) == ("outbox.delivery_attempt", "e-1")
    assert observed_at.isoformat() == "2026-11-06T05:00:08.500000+00:00"
    assert fields["result"] == "HTTP_503" and "process" not in fields


def test_fixture_timing_lines_are_parsed(collector_store, tmp_path):
    path = tmp_path / "consumer.jsonl"
    path.write_text(json.dumps({"record": "fixture.delivery", "received_at": "2026-11-06T05:00:01.000000Z",
                                "event_id": "e-2", "status": 200, "outcome": "APPLIED", "auth_ms": 51.2,
                                "handler_ms": 0.4, "total_ms": 52.0}) + "\n")
    LogTailer(collector_store, LogSource("fixture_consumer", path, parse_fixture_line)).poll()
    record_type, event_id, fields = collector_store.execute(
        "SELECT record_type, event_id, fields FROM log_records WHERE source = 'fixture_consumer'").fetchone()
    assert (record_type, event_id, fields["auth_ms"]) == ("fixture.delivery", "e-2", 51.2)


def test_an_unreachable_target_is_stored_as_an_unavailable_sample(collector_store):
    sampler = BlockerSampler(collector_store, "host=127.0.0.1 port=9 dbname=x user=x password=x connect_timeout=1")
    sampler.sample()
    coverage, payload = collector_store.execute(
        "SELECT coverage, payload FROM telemetry_samples WHERE kind = 'db_blockers'").fetchone()
    assert coverage == "UNAVAILABLE"
    assert payload["error"] == "OperationalError"
    assert "password" not in json.dumps(payload).lower() or "[REDACTED]" in json.dumps(payload)


def test_byte_offsets_are_line_starts(collector_store, tmp_path):
    path = tmp_path / "ledgercore.json"
    first, second = ledgercore_line(1), ledgercore_line(2)
    path.write_text(first + second)
    tailer_for(collector_store, path).poll()
    assert [r[1] for r in rows(collector_store)] == [0, len(first.encode())]
    assert os.path.getsize(path) == len((first + second).encode())
