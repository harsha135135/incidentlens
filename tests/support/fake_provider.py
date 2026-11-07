"""TEST ONLY: a loopback stand-in for the provider's Messages API, so failure behaviour can be
tested deterministically (spec §17 allows mocked providers only for deterministic failure tests).
The gateway uses it only with INCIDENTLENS_TEST_MODE=1 and a 127.0.0.1 URL.

Each POST /v1/messages takes the next scripted step:
  {"reply": "tool", "name": "...", "input": {...}}   one tool_use block
  {"reply": "status", "code": 429}                   an error response
  {"reply": "hang", "seconds": 30}                   no answer for that long
  {"reply": "malformed"}                             two tool calls (not one action)
  {"reply": "cut_off"}                               stop_reason max_tokens
When the script is empty, the reply is stop_inconclusive. count_tokens always answers.

Control (also loopback): POST /control/script (a JSON list), GET /control/log (requests seen).
Run as a process: python tests/support/fake_provider.py PORT
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider:
    def __init__(self, port: int = 0):
        self.script: list[dict] = []
        self.log: list[dict] = []
        self.query_delay = 0.0
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeProvider":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def set_script(self, steps: list[dict]) -> None:
        with self.lock:
            self.script = list(steps)

    def source_requests(self) -> list[dict]:
        with self.lock:
            return [entry for entry in self.log if entry["path"] == "/api/v1/query"]

    def messages_requests(self) -> list[dict]:
        with self.lock:
            return [entry for entry in self.log if entry["path"] == "/v1/messages"]

    def handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/control/log":
                    with provider.lock:
                        self.reply(200, provider.log)
                elif path in ("/api/v1/query", "/-/ready"):
                    # Prometheus stand-in for tests: an empty answer after query_delay seconds.
                    with provider.lock:
                        provider.log.append({"path": path, "at": time.time()})
                        delay = provider.query_delay
                    time.sleep(delay)
                    self.reply(200, {"status": "success", "data": {"resultType": "matrix", "result": []}})
                else:
                    self.reply(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                path = self.path.split("?")[0]
                if path == "/control/script":
                    provider.set_script(body)
                    self.reply(200, {"steps": len(body)})
                    return
                if path == "/control/query_delay":
                    provider.query_delay = float(body["seconds"])
                    self.reply(200, {"seconds": provider.query_delay})
                    return
                with provider.lock:
                    provider.log.append({"path": path, "at": time.time()})
                if path == "/v1/messages/count_tokens":
                    self.reply(200, {"input_tokens": 1_000 + len(json.dumps(body)) // 4})
                elif path == "/v1/messages":
                    with provider.lock:
                        step = provider.script.pop(0) if provider.script else {"reply": "stop"}
                    self.answer(step)
                else:
                    self.reply(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

            def answer(self, step: dict) -> None:
                kind = step["reply"]
                if kind == "hang":
                    time.sleep(step.get("seconds", 30))
                    kind = "stop"
                if kind == "status":
                    error_type = "rate_limit_error" if step["code"] == 429 else "api_error"
                    self.reply(step["code"], {"type": "error", "error": {"type": error_type, "message": "scripted"}},
                               {"retry-after": "1"} if step["code"] == 429 else None)
                    return
                if kind == "tool":
                    content = [tool_use(step["name"], step.get("input", {}))]
                    stop = "tool_use"
                elif kind == "malformed":
                    content = [tool_use("get_outbox_stats", {}), tool_use("get_delivery_timings", {})]
                    stop = "tool_use"
                elif kind == "cut_off":
                    content = [{"type": "text", "text": "partial"}]
                    stop = "max_tokens"
                else:
                    content = [tool_use("stop_inconclusive", {"reason": "scripted stop", "missing_evidence": []})]
                    stop = "tool_use"
                self.reply(200, {"id": f"msg_fake_{int(time.time() * 1000)}", "type": "message", "role": "assistant",
                                 "model": "claude-sonnet-5-5", "content": content, "stop_reason": stop,
                                 "stop_sequence": None, "usage": {"input_tokens": 1_200, "output_tokens": 80}})

            def reply(self, status: int, body, headers: dict | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        return Handler


def tool_use(name: str, arguments: dict) -> dict:
    return {"type": "tool_use", "id": f"toolu_{name[:8]}_{int(time.time() * 1e6) % 1_000_000}", "name": name,
            "input": arguments}


if __name__ == "__main__":
    FakeProvider(int(sys.argv[1])).server.serve_forever()
