"""The agent runner (spec §8): standard library only, so it can run inside the OpenShell sandbox.

It holds one capability for one job and generation, and talks only to the internal gateway. It has
no model key, no database credentials and no telemetry access: the gateway does all of that. The
runner only sequences the steps:

  1. Fetch the job context. Resume an action already in flight before starting another.
  2. Collect the initial bundle the server names in the context: in the product, every
     diagnostic tool (D48); in the agent mode, request metrics then outbox statistics.
  3. Ask the gateway for one model decision.
  4. A tool decision is executed, then back to 3. A decision that repeats an observation already
     saved ends the loop with the fallback report.
  5. A report or stop decision is submitted by reference to that model action.
  6. Budget, deadline or cancellation: obey the server; ask for the fallback where appropriate.

Every action uses the server-issued next_action_id, so a retried POST is an exact replay, never a
second action. The runner never starts processes (decisions: Stage 0 limitation on child processes).

Environment: INCIDENTLENS_INTERNAL_URL, INCIDENTLENS_JOB_ID. The capability arrives on stdin.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

POLL_SECONDS = 0.5
REQUEST_TIMEOUT = 10
MODEL_RETRIES = 1  # at most one explicit retry after a provider error (spec §12)
UNAVAILABLE_PATIENCE = 30  # seconds to keep repeating a request while the gateway is unavailable


class Gateway:
    def __init__(self, base_url: str, job_id: str, capability: str):
        self.base = f"{base_url}/internal/v1/jobs/{job_id}"
        self.capability = capability

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """One request, retried on network failure. Safe: GETs are reads and POSTs replay by ID."""
        data = None if body is None else json.dumps(body).encode()
        deadline = time.monotonic() + UNAVAILABLE_PATIENCE
        attempt = 0
        while True:
            request = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.capability}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    return response.status, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as error:
                if error.code != 503:
                    return error.code, json.loads(error.read() or b"{}")
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            # Unreachable gateway, or 503 (its database is down): wait and repeat the same request.
            if time.monotonic() > deadline:
                raise SystemExit("the internal gateway stayed unavailable")
            time.sleep(min(2 ** attempt * 0.5, 5))
            attempt += 1

    def context(self) -> dict:
        status, body = self.call("GET", "/context")
        if status != 200:
            raise SystemExit(f"context refused ({status}): the job is over or this runner was replaced")
        return body

    def wait_for(self, action_id: str) -> dict:
        while True:
            status, body = self.call("GET", f"/actions/{action_id}")
            if status != 200:
                raise SystemExit(f"polling refused ({status})")
            if body["status"] not in ("PENDING", "RUNNING"):
                return body
            time.sleep(POLL_SECONDS)

    def act(self, action_id: str, kind: str, tool: str | None = None, arguments: dict | None = None) -> dict | None:
        body = {"action_id": action_id, "kind": kind}
        if kind == "tool":
            body |= {"tool": tool, "arguments": arguments or {}}
        status, reply = self.call("POST", "/actions", body)
        if status in (200, 202):
            return self.wait_for(action_id)
        if status == 409 and reply.get("error", {}).get("code", "").endswith("BUDGET_EXHAUSTED"):
            self.give_up("the action budget is exhausted")
            return None
        if status in (403, 409):
            raise SystemExit(f"action refused ({status} {reply.get('error', {}).get('code')})")
        raise SystemExit(f"action rejected ({status})")

    def report(self, action_id: str) -> dict:
        return self.call("POST", "/report", {"action_id": action_id})[1]

    def give_up(self, reason: str) -> None:
        self.call("POST", "/report", {"give_up": reason})


def normalized(tool: str, arguments: dict) -> str:
    return json.dumps([tool, arguments], sort_keys=True)


def run(gateway: Gateway) -> None:
    model_errors_in_a_row = 0
    while True:
        context = gateway.context()
        if context["status"] != "RUNNING":
            return
        if context["in_flight"]:
            gateway.wait_for(context["in_flight"]["action_id"])
            continue
        phase, next_id, last = context["next_phase"], context["next_action_id"], context["last_decision"] or {}
        collected = {normalized(e["tool"], e["arguments"]) for e in context["evidence"]}

        if phase == "initial_collection":
            # The bundle comes from the server: the whole fixed bundle in the product (D48).
            bundle = [(item["tool"], item["arguments"]) for item in context["initial_bundle"]]
            tool, arguments = next((t, a) for t, a in bundle if normalized(t, a) not in collected)
            gateway.act(next_id, "tool", tool, arguments)
        elif phase == "model_decision":
            result = gateway.act(next_id, "model")
            if result is None:
                return
            if result["status"] in ("ERROR", "UNKNOWN"):
                model_errors_in_a_row += 1
                if model_errors_in_a_row > MODEL_RETRIES:
                    gateway.give_up("the model call failed twice")
                    return
            else:
                model_errors_in_a_row = 0
        elif phase == "tool_observation":
            if last.get("type") == "call_tool" and not last.get("executed"):
                if normalized(last["tool"], last.get("arguments") or {}) in collected:
                    gateway.give_up("the model asked to repeat an observation that is already saved")
                    return
                gateway.act(next_id, "tool", last["tool"], last.get("arguments"))
            else:
                gateway.act(next_id, "model")
        elif phase == "finalization":
            gateway.report(last["action_id"])
        else:
            raise SystemExit(f"unknown phase {phase!r}")


def main() -> None:
    capability = sys.stdin.readline().strip()
    run(Gateway(os.environ["INCIDENTLENS_INTERNAL_URL"], os.environ["INCIDENTLENS_JOB_ID"], capability))


if __name__ == "__main__":
    main()
