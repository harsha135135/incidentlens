"""Telemetry collector: the small, model-free process that makes history exist (spec §5, §20).

Three jobs, in one loop:
  * Blocker samples. Every 2 seconds it calls LedgerCore's diagnostics.blocking_sessions() as
    the read-only login and stores the result. A failed collection is stored as an UNAVAILABLE
    sample, so a gap is explicit; time without any sample (collector stopped) is also a gap.
  * Log tailing. It reads complete lines from LedgerCore's JSON log and the fixture consumer's
    timing log, redacts them, and stores them in the same transaction that advances the saved
    byte offset. (source_file_id, byte_offset) is unique, so re-reading after a crash is harmless.
    It follows rotation by file identity (device, inode), finishes the rotated file, and records
    a discontinuity when a file is truncated, goes missing, or rotated away while stopped.
  * Heartbeats. Every 5 seconds it stores how far each log has been read, so tools can tell
    whether ingestion had caught up when they answer.

Run: PYTHONPATH=src uv run python -m incidentlens.collector
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from incidentlens.config import COLLECTOR_VERSION, SERVICE_ID, Settings, load_settings
from incidentlens.database import connect
from incidentlens.redaction import redact
from incidentlens.tools.envelope import parse_iso

log = logging.getLogger("incidentlens.collector")

BLOCKER_INTERVAL_S = 2.0
LOG_INTERVAL_S = 1.0
HEARTBEAT_INTERVAL_S = 5.0
RETENTION_INTERVAL_S = 600.0
MAX_BLOCKER_ROWS = 200
MAX_READ_BYTES = 1024 * 1024

# Fields kept from LedgerCore's ECS lines: diagnostic record keys plus the plain log basics.
# Process, service and ECS metadata are dropped.
LEDGERCORE_RECORD_FIELDS = {
    "record", "outboxEventId", "eventId", "attempt", "totalAttempt", "startedAt", "endedAt", "durationMs", "result",
    "httpStatus", "transitionRecorded", "selected", "published", "failed", "markedExhausted",
    "batchSize", "configuredDelayMs", "instanceId", "revision", "effectiveAt", "outboxPollerEnabled",
    "outboxWorkers", "outboxPollIntervalMs", "outboxBatchSize", "outboxDeliveryTimeoutMs",
    "outboxMaxAttempts", "outboxBackoffBaseMs", "outboxBackoffMaxMs", "consumerTarget",
    "consumerAuthentication", "inProcessConsumerBcryptCost", "poolMaximumSize", "poolMinimumIdle",
    "poolConnectionTimeoutMs", "databaseSessionOptions", "requestId",
}

@dataclass
class ParsedRecord:
    observed_at: datetime | None  # event time: when the measured thing started, else when logged
    record_type: str
    event_id: str | None
    fields: dict


def parse_ledgercore_line(text: str) -> ParsedRecord:
    try:
        line = json.loads(text)
    except ValueError:
        return ParsedRecord(None, "unparsed", None, {"text": redact(text)})
    fields = {key: line[key] for key in LEDGERCORE_RECORD_FIELDS if key in line}
    fields["timestamp"] = line.get("@timestamp")
    fields["level"] = (line.get("log") or {}).get("level")
    fields["logger"] = (line.get("log") or {}).get("logger")
    fields["message"] = line.get("message")
    record_type = fields.get("record") or "log"
    observed_at = parse_iso(fields.get("startedAt")) or parse_iso(line.get("@timestamp"))
    # Delivery records name their event in outboxEventId; other lines written during a delivery
    # carry it as the MDC field eventId.
    event_id = fields.get("outboxEventId") or fields.get("eventId")
    return ParsedRecord(observed_at, record_type, event_id, redact(fields))


def parse_fixture_line(text: str) -> ParsedRecord:
    try:
        line = json.loads(text)
    except ValueError:
        return ParsedRecord(None, "unparsed", None, {"text": redact(text)})
    keys = ("received_at", "event_id", "status", "outcome", "auth_ms", "handler_ms", "total_ms")
    fields = {key: line.get(key) for key in keys}
    return ParsedRecord(parse_iso(line.get("received_at")), line.get("record") or "fixture.delivery",
                        line.get("event_id"), redact(fields))


@dataclass
class LogSource:
    name: str
    path: Path
    parse: Callable[[str], ParsedRecord]


def insert_sample(conn: psycopg.Connection, kind: str, observed_at: datetime, coverage: str, payload: dict,
                  service: str = SERVICE_ID) -> None:
    conn.execute(
        "INSERT INTO telemetry_samples (service, kind, observed_at, collector_version, coverage, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (service, kind, observed_at, COLLECTOR_VERSION, coverage, Jsonb(payload)))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LogTailer:
    """Follows one log file across rotation and truncation, storing complete lines exactly once."""

    def __init__(self, conn: psycopg.Connection, source: LogSource, service: str = SERVICE_ID):
        self.conn = conn
        self.source = source
        self.service = service
        self.handle = None
        self.file_id: int | None = None
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.reported_missing = False

    def poll(self) -> None:
        if self.handle is None and not self.open_current():
            return
        self.read_new_lines()
        self.check_rotation_and_truncation()

    def status(self) -> dict:
        size = os.fstat(self.handle.fileno()).st_size if self.handle else None
        return {"path": str(self.source.path), "file_id": self.file_id, "offset": self.offset,
                "file_size": size, "lag_bytes": None if size is None else max(size - self.offset, 0)}

    def open_current(self) -> bool:
        """Opens the file at the path, resuming a saved offset when it is the same file."""
        try:
            stat = self.source.path.stat()
        except FileNotFoundError:
            if not self.reported_missing:
                self.record_gap("missing", "log file does not exist")
                self.reported_missing = True
            return False
        self.reported_missing = False
        identity = (stat.st_dev, stat.st_ino)
        active = self.conn.execute(
            "SELECT id, device, inode, read_offset FROM log_files "
            "WHERE service = %s AND source = %s AND state = 'ACTIVE'", (self.service, self.source.name)).fetchone()
        if active and (active[1], active[2]) == identity and stat.st_size >= active[3]:
            self.attach(active[0], identity, active[3])
            return True
        if active:
            self.finish_previous(active)
        with self.conn.transaction():
            file_id = self.conn.execute(
                "INSERT INTO log_files (service, source, path, device, inode, state) "
                "VALUES (%s, %s, %s, %s, %s, 'ACTIVE') RETURNING id",
                (self.service, self.source.name, str(self.source.path), identity[0], identity[1])).fetchone()[0]
        self.attach(file_id, identity, 0)
        return True

    def finish_previous(self, active) -> None:
        """The saved file is no longer at the path (rotated or truncated while the collector was
        stopped). Finish the rotated file if it is still beside the path; otherwise record the loss."""
        file_id, device, inode, offset = active
        rotated = self.find_rotated(device, inode)
        if rotated is not None:
            with rotated.open("rb") as old:
                self.drain(file_id, old, offset)
        else:
            self.record_gap("rotated_or_truncated_while_stopped",
                            "the previously read file is gone; lines after the saved offset may be lost")
        with self.conn.transaction():
            self.conn.execute("UPDATE log_files SET state = 'FINISHED', updated_at = now() WHERE id = %s", (file_id,))

    def find_rotated(self, device: int, inode: int) -> Path | None:
        for candidate in self.source.path.parent.glob(self.source.path.name + ".*"):
            stat = candidate.stat()
            if (stat.st_dev, stat.st_ino) == (device, inode):
                return candidate
        return None

    def attach(self, file_id: int, identity: tuple[int, int], offset: int) -> None:
        self.handle = self.source.path.open("rb")
        self.handle.seek(offset)
        self.file_id, self.identity, self.offset = file_id, identity, offset

    def read_new_lines(self) -> None:
        self.offset = self.store_lines(self.file_id, self.handle, self.offset)

    def drain(self, file_id: int, handle, offset: int) -> int:
        """Stores every remaining complete line of a file that will not grow any more."""
        while True:
            new_offset = self.store_lines(file_id, handle, offset)
            if new_offset == offset:
                return offset
            offset = new_offset

    def store_lines(self, file_id: int, handle, start_offset: int) -> int:
        """Stores every complete line from start_offset, and the new offset, in one transaction.
        A partial last line is left for the next poll."""
        handle.seek(start_offset)
        chunk = handle.read(MAX_READ_BYTES)
        end = chunk.rfind(b"\n")
        if end < 0:
            return start_offset
        rows = []
        position = start_offset
        for raw in chunk[: end + 1].split(b"\n")[:-1]:
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                record = self.source.parse(text)
                rows.append((file_id, position, self.service, self.source.name, record.observed_at,
                             record.record_type, record.event_id, Jsonb(record.fields)))
            position += len(raw) + 1
        with self.conn.transaction():
            if rows:
                with self.conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO log_records (source_file_id, byte_offset, service, source, observed_at, "
                        "record_type, event_id, fields) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (source_file_id, byte_offset) DO NOTHING", rows)
            self.conn.execute("UPDATE log_files SET read_offset = %s, updated_at = now() WHERE id = %s",
                              (position, file_id))
        handle.seek(position)
        return position

    def check_rotation_and_truncation(self) -> None:
        try:
            stat = self.source.path.stat()
            current = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            current = None
        size_now = os.fstat(self.handle.fileno()).st_size
        if current == self.identity and size_now >= self.offset:
            return
        if current == self.identity:
            self.record_gap("truncated", f"file shrank below the read offset {self.offset}; restarting at 0")
        else:
            # Rotated (or removed): finish what is left in the file we hold open.
            self.offset = self.drain(self.file_id, self.handle, self.offset)
        with self.conn.transaction():
            self.conn.execute("UPDATE log_files SET state = 'FINISHED', updated_at = now() WHERE id = %s",
                              (self.file_id,))
        self.handle.close()
        self.handle = None
        self.identity = None

    def record_gap(self, reason: str, detail: str) -> None:
        with self.conn.transaction():
            insert_sample(self.conn, "log_gap", utc_now(), "UNAVAILABLE",
                          {"source": self.source.name, "path": str(self.source.path), "reason": reason,
                           "detail": detail}, self.service)


class BlockerSampler:
    """Calls diagnostics.blocking_sessions() as ledger_diag_reader on its own connection, outside
    LedgerCore's pool, so it still works when that pool is exhausted (spec §5)."""

    def __init__(self, store: psycopg.Connection, target_conninfo: str, service: str = SERVICE_ID):
        self.store = store
        self.target_conninfo = target_conninfo
        self.service = service
        self.target: psycopg.Connection | None = None

    def sample(self) -> None:
        started = utc_now()
        try:
            rows = self.query()
        except psycopg.Error as error:
            self.close_target()
            with self.store.transaction():
                insert_sample(self.store, "db_blockers", started, "UNAVAILABLE",
                              {"error": type(error).__name__,
                               "detail": redact((str(error).splitlines() or [""])[0][:200])},
                              self.service)
            return
        truncated = len(rows) > MAX_BLOCKER_ROWS
        edges = [self.edge(row) for row in rows[:MAX_BLOCKER_ROWS]]
        with self.store.transaction():
            insert_sample(self.store, "db_blockers", started, "COMPLETE",
                          {"edge_count": len(edges), "truncated": truncated, "edges": edges}, self.service)

    def query(self) -> list:
        if self.target is None:
            self.target = psycopg.connect(self.target_conninfo, autocommit=False)
            self.target.read_only = True  # every transaction BEGINs READ ONLY (adapter-side, D4)
        with self.target.transaction():
            self.target.execute("SET LOCAL statement_timeout = 2000")
            return self.target.execute(
                "SELECT observed_at, blocked_pid, blocked_wait_event_type, blocked_wait_event, "
                "blocked_lock_wait_started, blocker_pid, blocker_role, blocker_state, blocker_wait_event_type, "
                "blocker_transaction_started FROM diagnostics.blocking_sessions() LIMIT %s",
                (MAX_BLOCKER_ROWS + 1,)).fetchall()

    @staticmethod
    def edge(row) -> dict:
        (observed_at, blocked_pid, wait_type, wait_event, wait_started, blocker_pid, blocker_role,
         blocker_state, blocker_wait_type, blocker_xact_started) = row
        return {
            "observed_at": observed_at.isoformat(),
            "blocked_pid": blocked_pid,
            "blocked_wait_event_type": wait_type,
            "blocked_wait_event": wait_event,
            "blocked_wait_seconds": None if wait_started is None else
            round((observed_at - wait_started).total_seconds(), 3),
            "blocker_pid": blocker_pid,
            "blocker_role": blocker_role,
            "blocker_state": blocker_state,
            "blocker_wait_event_type": blocker_wait_type,
            "blocker_transaction_seconds": None if blocker_xact_started is None else
            round((observed_at - blocker_xact_started).total_seconds(), 3),
        }

    def close_target(self) -> None:
        if self.target is not None:
            try:
                self.target.close()
            finally:
                self.target = None


def delete_expired(conn: psycopg.Connection) -> None:
    """Telemetry is kept for 24 hours (spec §11)."""
    with conn.transaction():
        conn.execute("DELETE FROM telemetry_samples WHERE observed_at < now() - interval '24 hours'")
        conn.execute("DELETE FROM log_records WHERE ingested_at < now() - interval '24 hours'")


def log_sources(settings: Settings) -> list[LogSource]:
    return [
        LogSource("ledgercore", settings.ledgercore_log_file, parse_ledgercore_line),
        LogSource("fixture_consumer", settings.fixture_log_file, parse_fixture_line),
    ]


def run(settings: Settings, should_stop: Callable[[], bool]) -> None:
    # Autocommit, with every write inside an explicit transaction block: a block then always means
    # BEGIN ... COMMIT, never a savepoint inside a forgotten implicit transaction.
    store = connect(settings.collector_conninfo, autocommit=True)
    tailers = [LogTailer(store, source) for source in log_sources(settings)]
    sampler = BlockerSampler(store, settings.target_conninfo)
    next_blocker = next_logs = next_heartbeat = time.monotonic()
    next_retention = time.monotonic() + RETENTION_INTERVAL_S
    while not should_stop():
        now = time.monotonic()
        if now >= next_blocker:
            sampler.sample()
            next_blocker += BLOCKER_INTERVAL_S
            if next_blocker < now:  # fell behind (for example after a pause): skip, do not burst
                next_blocker = now + BLOCKER_INTERVAL_S
        if now >= next_logs:
            for tailer in tailers:
                tailer.poll()
            next_logs = now + LOG_INTERVAL_S
        if now >= next_heartbeat:
            with store.transaction():
                insert_sample(store, "collector_heartbeat", utc_now(), "COMPLETE",
                              {"sources": {t.source.name: t.status() for t in tailers}})
            next_heartbeat = now + HEARTBEAT_INTERVAL_S
        if now >= next_retention:
            delete_expired(store)
            next_retention = now + RETENTION_INTERVAL_S
        time.sleep(max(0.0, min(next_blocker, next_logs, next_heartbeat) - time.monotonic()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()  # migrations are applied by the owner (scripts/stack.py), not here
    stopping = {"flag": False}

    def request_stop(signum, frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log.info("collector %s started", COLLECTOR_VERSION)
    while not stopping["flag"]:
        try:
            run(settings, lambda: stopping["flag"])
        except psycopg.OperationalError as error:
            # IncidentLens's own database is unavailable. Logs stay in their files and are read
            # later from the saved offsets; blocker samples are missed and show up as a gap.
            log.warning("IncidentLens database unavailable (%s); retrying in 5 s", type(error).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
