"""The gateway's action executor (spec §8): one thread that runs admitted actions. The database is
the source of truth; the in-memory wake event only shortens the wait. Each action is claimed in
the database with a generation check before any I/O, and its result is committed with the same
check, so work for a job that moved on can never become evidence."""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import anthropic
import psycopg

from incidentlens import evidence
from incidentlens.config import Settings
from incidentlens.database import connect
from incidentlens.config import MAX_INPUT_TOKENS_PER_CALL
from incidentlens.model_gateway import (
    EVIDENCE_PROMPT_CHARS,
    ModelSettings,
    build_messages,
    count_input_tokens,
    decide,
)
from incidentlens.tools.envelope import Window, build_envelope
from incidentlens.tools.registry import live_context, run_tool
from incidentlens.tools.replay import replay_tool

log = logging.getLogger("incidentlens.executor")


class Executor(threading.Thread):
    def __init__(self, settings: Settings, model: ModelSettings, conninfo: str | None = None,
                 replay_bundles: Path | None = None):
        super().__init__(name="action-executor", daemon=True)
        self.settings = settings
        self.model = model
        self.conninfo = conninfo or settings.app_conninfo
        self.replay_bundles = replay_bundles  # evaluation replay mode (D51): never read live sources
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                with connect(self.conninfo, autocommit=True) as conn:
                    while not self.stopping.is_set():
                        if not self.run_one(conn):
                            self.wake.wait(timeout=1.0)
                            self.wake.clear()
            except psycopg.OperationalError as error:
                # IncidentLens's database is unavailable: start nothing until it returns (fail closed).
                log.warning("database unavailable (%s); executor waiting", type(error).__name__)
                self.stopping.wait(2.0)

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def run_one(self, conn: psycopg.Connection) -> bool:
        """Runs the oldest PENDING action, if any. Returns whether there was one."""
        row = conn.execute("SELECT id, kind FROM actions WHERE status = 'PENDING' ORDER BY created_at, id "
                           "LIMIT 1").fetchone()
        if row is None:
            return False
        action_id, kind = row
        try:
            if kind == "tool":
                self.run_tool_action(conn, action_id)
            else:
                self.run_model_action(conn, action_id)
        except psycopg.OperationalError:
            raise
        except Exception:  # an unexpected failure ends this action with an error, never silently
            log.exception("action %s failed unexpectedly", action_id)
            with conn.transaction():
                action = evidence.lock_action_with_job(conn, action_id)
                if action and action["status"] in evidence.ACTIVE_ACTION:
                    evidence.finish_without_result(conn, action, "ERROR", "internal error while running the action")
        return True

    def run_tool_action(self, conn: psycopg.Connection, action_id: uuid.UUID) -> None:
        action = evidence.start_tool(conn, action_id)
        if action is None:
            return
        start, end = conn.execute("SELECT window_start, window_end FROM investigations WHERE id = %s",
                                  (action["investigation_id"],)).fetchone()
        window = Window(start, end)
        try:
            if self.replay_bundles is not None:
                envelope = replay_tool(self.replay_bundles, window, action["tool"], action["arguments"])
            else:
                envelope = run_tool(action["tool"], action["arguments"], live_context(window, conn, self.settings))
        except psycopg.OperationalError:
            raise
        except Exception as error:  # a source adapter failed unexpectedly: save that as an observation
            log.exception("tool %s failed", action["tool"])
            envelope = build_envelope(action["tool"], action["arguments"], window, "UNAVAILABLE", "error", {},
                                      ["The tool failed unexpectedly; its source is unknown for this window."],
                                      [type(error).__name__])
        evidence.commit_tool_result(conn, action_id, envelope)

    def run_model_action(self, conn: psycopg.Connection, action_id: uuid.UUID) -> None:
        job_id = conn.execute("SELECT investigation_id FROM actions WHERE id = %s", (action_id,)).fetchone()[0]
        messages = build_messages(conn, job_id)
        try:
            estimate = count_input_tokens(self.model, messages)  # free; no reservation needed yet
            if estimate > MAX_INPUT_TOKENS_PER_CALL:
                # Shorten more of the older observations (marked as such) and count once more.
                messages = build_messages(conn, job_id, EVIDENCE_PROMPT_CHARS // 2)
                estimate = count_input_tokens(self.model, messages)
        except anthropic.APIError as error:
            # Without a count there is no honest reservation, so the call is not sent (decisions D10).
            with conn.transaction():
                action = evidence.lock_action_with_job(conn, action_id)
                if action and action["status"] == "PENDING":
                    evidence.finish_without_result(conn, action, "ERROR",
                                                   f"token count failed ({type(error).__name__}); call not sent")
            return
        if evidence.reserve_model_call(conn, action_id, estimate) is None:
            return  # refused (too large, over budget) or the job moved on; recorded already
        outcome = decide(self.model, messages)
        evidence.commit_model_result(conn, action_id, outcome.decision, outcome.input_tokens, outcome.output_tokens,
                                     outcome.provider_request_id, outcome.status, outcome.error)
