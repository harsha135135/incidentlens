"""TEST FIXTURE: a local stand-in for LedgerCore's event consumer, used to cause consumer faults
without changing LedgerCore (decisions D1). It is not an independent deployed service, and every
result produced with it is labelled a fixture result.

LedgerCore delivers to it through its existing LEDGERCORE_CONSUMER_URL setting. For each delivery
it verifies the publisher's HTTP Basic credential with real BCrypt (the hash LedgerCore's own
consumer would use, at its cost), then runs a tiny handler, and appends one JSON line with the
measured authentication and handler times. The line never says which fault mode is active.

Two loopback listeners:
  events   127.0.0.1:18090  POST /events   (LedgerCore's deliveries)
  control  127.0.0.1:18091  GET/POST /mode (used only by the evaluation controller)

Modes (POST /mode with {"mode": ..., "handler_delay_ms": ...}):
  normal       verify, handle, 200
  slow         as normal, plus handler_delay_ms inside the handler (keep it below LedgerCore's
               3 s delivery timeout, or slow deliveries turn into timeouts)
  unavailable  the events listener is closed: connections are refused
  reject       the credential is checked against a different hash, as after a credential
               rotation the publisher missed: 401, after the full BCrypt cost

Environment: STUB_PUBLISHER_HASH ({bcrypt}$2a$10$... as in LedgerCore's settings),
STUB_TIMING_LOG (JSON lines file path).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bcrypt

EVENTS_ADDRESS = ("127.0.0.1", 18090)
CONTROL_ADDRESS = ("127.0.0.1", 18091)
MODES = ("normal", "slow", "unavailable", "reject")
MAX_BODY_BYTES = 64 * 1024


class StubState:
    """Mode, credentials and the timing log, shared by both listeners."""

    def __init__(self, publisher_hash: str, timing_log: Path):
        self.lock = threading.Lock()
        self.mode = "normal"
        self.handler_delay_ms = 0
        self.expected_hash = publisher_hash.removeprefix("{bcrypt}").encode()
        # A hash of a different random password at the same cost: "reject" verifies against it.
        cost = int(self.expected_hash.split(b"$")[2])
        self.rotated_hash = bcrypt.hashpw(os.urandom(18).hex().encode(), bcrypt.gensalt(rounds=cost))
        self.seen_event_ids: set[str] = set()
        timing_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = timing_log.open("a", buffering=1, encoding="utf-8")
        self.events_server: ThreadingHTTPServer | None = None

    def write_timing(self, record: dict) -> None:
        with self.lock:
            self.log_file.write(json.dumps(record, separators=(",", ":")) + "\n")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def password_from_basic(header: str | None) -> bytes | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode()
    except ValueError:
        return None
    _, _, password = decoded.partition(":")
    return password.encode()


def make_events_handler(state: StubState):
    class EventsHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            received_at = utc_now_text()
            started = time.perf_counter()
            if self.path != "/events":
                self.reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY_BYTES))
            with state.lock:
                mode, delay_ms = state.mode, state.handler_delay_ms
            expected = state.rotated_hash if mode == "reject" else state.expected_hash

            auth_started = time.perf_counter()
            password = password_from_basic(self.headers.get("Authorization"))
            authenticated = password is not None and bcrypt.checkpw(password, expected)
            auth_ms = (time.perf_counter() - auth_started) * 1000

            event_id = None
            handler_ms = None
            if authenticated:
                handler_started = time.perf_counter()
                try:
                    event_id = str(json.loads(body)["eventId"])
                    status, outcome = 200, ("DUPLICATE" if event_id in state.seen_event_ids else "APPLIED")
                    state.seen_event_ids.add(event_id)
                except (ValueError, KeyError, TypeError):
                    status, outcome = 400, "MALFORMED_EVENT"
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                handler_ms = (time.perf_counter() - handler_started) * 1000
            else:
                status, outcome = 401, "AUTHENTICATION_FAILED"
                try:
                    event_id = str(json.loads(body)["eventId"])
                except (ValueError, KeyError, TypeError):
                    event_id = None

            total_ms = (time.perf_counter() - started) * 1000
            state.write_timing({
                "record": "fixture.delivery",
                "received_at": received_at,
                "event_id": event_id,
                "status": status,
                "outcome": outcome,
                "auth_ms": round(auth_ms, 3),
                "handler_ms": None if handler_ms is None else round(handler_ms, 3),
                "total_ms": round(total_ms, 3),
            })
            self.reply(status, {"status": outcome})

        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # the timing log is the only record
            pass

    return EventsHandler


def start_events_listener(state: StubState) -> None:
    server = ThreadingHTTPServer(EVENTS_ADDRESS, make_events_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.events_server = server


def stop_events_listener(state: StubState) -> None:
    if state.events_server is not None:
        state.events_server.shutdown()
        state.events_server.server_close()
        state.events_server = None


def make_control_handler(state: StubState):
    class ControlHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            with state.lock:
                self.reply(200, {"mode": state.mode, "handler_delay_ms": state.handler_delay_ms})

        def do_POST(self) -> None:  # noqa: N802
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                mode = request["mode"]
                delay_ms = int(request.get("handler_delay_ms", 0))
                if mode not in MODES or not 0 <= delay_ms <= 60_000:
                    raise ValueError(mode)
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": f"mode must be one of {MODES}; handler_delay_ms 0..60000"})
                return
            # Open or close the events listener outside the state lock (shutdown waits for requests).
            if mode == "unavailable":
                stop_events_listener(state)
            elif state.events_server is None:
                start_events_listener(state)
            with state.lock:
                state.mode, state.handler_delay_ms = mode, (delay_ms if mode == "slow" else 0)
                self.reply(200, {"mode": state.mode, "handler_delay_ms": state.handler_delay_ms})

        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    return ControlHandler


def main() -> None:
    state = StubState(os.environ["STUB_PUBLISHER_HASH"], Path(os.environ["STUB_TIMING_LOG"]))
    start_events_listener(state)
    control = ThreadingHTTPServer(CONTROL_ADDRESS, make_control_handler(state))
    print(f"fixture consumer: events on {EVENTS_ADDRESS}, control on {CONTROL_ADDRESS}", flush=True)
    control.serve_forever()


if __name__ == "__main__":
    main()
