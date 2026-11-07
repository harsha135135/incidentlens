"""Stage 0 stand-in for the internal gateway: one route that checks a capability token.

Usage: python3 listener.py HOST PORT NAME
The expected token is read from the STAGE0_CAPABILITY environment variable. Every request is
printed as one JSON line (peer address, path, outcome) so the smoke test can show where
sandbox traffic actually arrives from. Standard library only; this is not the real gateway.
"""

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECTED_TOKEN = os.environ.get("STAGE0_CAPABILITY", "")


class Handler(BaseHTTPRequestHandler):
    server_name_label = "listener"

    def do_GET(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.path != "/internal/v1/ping":
            self.reply(404, "not_found")
        elif EXPECTED_TOKEN and hmac.compare_digest(supplied, EXPECTED_TOKEN):
            self.reply(200, "ok")
        else:
            self.reply(401, "invalid_capability")

    def reply(self, status, outcome):
        body = json.dumps({"listener": self.server_name_label, "outcome": outcome}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "listener": self.server_name_label,
            "peer": self.client_address[0],
            "path": self.path,
            "status": status,
        }
        print(json.dumps(record), flush=True)

    def log_message(self, format, *args):
        pass  # requests are printed as JSON lines in reply()


def main():
    host, port, name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    Handler.server_name_label = name
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"listener": name, "bound": f"{host}:{port}"}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
