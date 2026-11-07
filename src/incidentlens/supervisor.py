"""The single supervisor (spec §12).

- It holds a session-level advisory lock on its own connection. A second supervisor cannot get the
  lock and exits. If the lock connection is lost, it stops the runner and exits; generation fencing
  remains the final protection against stale writes.
- Every second it expires old queued jobs, claims the next job when none is active, and watches the
  active one: cancellation, deadline, runner exit. Every 5 seconds it refreshes its heartbeat and
  the job's 30-second lease.
- Recovery never resets the deadline or budgets, and happens at most twice per job.

Runner modes:
  local     the runner as a local process: DEVELOPMENT MODE, NOT SANDBOXED (decisions D14).
            It cannot satisfy the isolation or release gate.
  openshell the runner inside an OpenShell sandbox (Stage 3).

Run: PYTHONPATH=src uv run python -m incidentlens.supervisor [--runner openshell|local]
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import psycopg

from incidentlens import jobs, reports
from incidentlens.config import INTERNAL_PORT, LOG_DIR, PROJECT_ROOT, load_settings
from incidentlens.database import connect

log = logging.getLogger("incidentlens.supervisor")
LOCK_KEY = "incidentlens.supervisor"
TICK_SECONDS = 1.0
HEARTBEAT_SECONDS = 5.0


@dataclass
class ActiveRun:
    claim: jobs.Claim
    process: subprocess.Popen


class LocalRunnerLauncher:
    """DEVELOPMENT MODE, NOT SANDBOXED (D14): the runner as a local process."""

    label = "local (unsandboxed development mode)"

    def __init__(self, internal_url: str):
        self.internal_url = internal_url

    def start(self, claim: jobs.Claim) -> subprocess.Popen:
        env = {"PATH": os.environ.get("PATH", ""), "INCIDENTLENS_INTERNAL_URL": self.internal_url,
               "INCIDENTLENS_JOB_ID": str(claim.job_id)}
        log_path = LOG_DIR / "runner" / f"{claim.sandbox_name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = subprocess.Popen([sys.executable, str(PROJECT_ROOT / "src" / "incidentlens" / "runner.py")],
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, env=env,
                                       start_new_session=True)
        process.stdin.write((claim.capability + "\n").encode())  # never on the command line or in the environment
        process.stdin.close()
        (LOG_DIR / "run").mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "run" / "runner.pid").write_text(str(process.pid))
        return process

    def stop(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class OpenShellRunnerLauncher:
    """The runner inside an OpenShell sandbox (deploy/openshell/runner-policy.yaml): it can reach
    only the internal gateway port, as only /usr/bin/python3.12, with no host secrets or other egress
    (Stage 0 smoke test, 11/11). One wrapper process creates the sandbox and then runs the runner in
    it, so the supervisor keeps heartbeating while the sandbox starts. The capability travels on
    stdin to a shell built-in: never in an argument list or the environment."""

    label = "openshell sandbox"
    image = "incidentlens-runner:stage0"
    # Uploading to a directory keeps the file name. (An upload to "/sandbox/runner.py" with an
    # absolute source path created a directory of that name.) Sandbox names are at most 19 characters.
    policy = PROJECT_ROOT / "deploy" / "openshell" / "runner-policy.yaml"

    def start(self, claim: jobs.Claim) -> subprocess.Popen:
        name = claim.sandbox_name
        runner = PROJECT_ROOT / "src" / "incidentlens" / "runner.py"
        inner = (f"INCIDENTLENS_INTERNAL_URL=http://host.openshell.internal:{INTERNAL_PORT} "
                 f"INCIDENTLENS_JOB_ID={claim.job_id} exec python3 /sandbox/runner.py")
        script = (
            'read -r token\n'
            f'openshell sandbox create --name {name} --from {self.image} --policy "{self.policy}" '
            f'--upload "{runner}:/sandbox/" --detach --no-tty --no-auto-providers || exit 90\n'
            f'printf "%s\\n" "$token" | openshell sandbox exec -n {name} --no-tty --no-login-shell -- '
            f"sh -c '{inner}'\n")
        log_path = LOG_DIR / "runner" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = subprocess.Popen(["sh", "-c", script], stdin=subprocess.PIPE, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")})
        process.stdin.write((claim.capability + "\n").encode())
        process.stdin.close()
        process.sandbox_name = name
        return process

    def stop(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        subprocess.run(["openshell", "sandbox", "delete", process.sandbox_name], capture_output=True, timeout=60)


class Supervisor:
    def __init__(self, conninfo: str, launcher, instance: str | None = None):
        self.conninfo = conninfo
        self.launcher = launcher
        self.instance = instance or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.active: ActiveRun | None = None
        self.stopping = False

    def acquire_lock(self) -> psycopg.Connection | None:
        lock = connect(self.conninfo, autocommit=True)
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,)).fetchone()[0]:
            lock.close()
            return None
        return lock

    def run(self) -> int:
        lock = self.acquire_lock()
        if lock is None:
            log.error("another supervisor holds the lock; exiting without running jobs")
            return 1
        log.info("supervisor %s started; runner mode: %s", self.instance, self.launcher.label)
        next_heartbeat = 0.0
        try:
            with connect(self.conninfo, autocommit=True) as conn:
                while not self.stopping:
                    lock.execute("SELECT 1")  # raises if the lock connection is gone
                    if time.monotonic() >= next_heartbeat:
                        jobs.heartbeat(conn, self.instance)
                        if self.active:
                            jobs.refresh_lease(conn, self.active.claim.job_id, self.active.claim.generation)
                        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                    self.tick(conn)
                    time.sleep(TICK_SECONDS)
        except psycopg.OperationalError as error:
            log.error("database connection lost (%s); stopping the runner and exiting", type(error).__name__)
            return 2
        finally:
            if self.active:
                self.launcher.stop(self.active.process)  # the lease expires; the next supervisor recovers the job
            lock.close()
        return 0

    def tick(self, conn: psycopg.Connection) -> None:
        jobs.expire_queued(conn)
        if self.active:
            self.watch_active(conn)
        else:
            self.recover_orphans(conn)
            claim = jobs.claim_next(conn)
            if claim:
                log.info("claimed job %s generation %s", claim.job_id, claim.generation)
                self.active = ActiveRun(claim, self.launcher.start(claim))

    def watch_active(self, conn: psycopg.Connection) -> None:
        claim = self.active.claim
        status, generation, past_deadline = conn.execute(
            "SELECT status, generation, deadline <= now() FROM investigations WHERE id = %s",
            (claim.job_id,)).fetchone()
        runner_exited = self.active.process.poll() is not None
        if status == "CANCEL_REQUESTED":
            self.launcher.stop(self.active.process)
            jobs.finish_cancellation(conn, claim.job_id)
            self.active = None
        elif status in jobs.TERMINAL or generation != claim.generation:
            self.launcher.stop(self.active.process)
            self.active = None
        elif past_deadline:
            self.launcher.stop(self.active.process)
            reports.finalize_fallback(conn, claim.job_id, "the 180-second deadline passed")
            self.active = None
        elif runner_exited:
            self.launcher.stop(self.active.process)  # also deletes the sandbox
            outcome = jobs.requeue_for_recovery(conn, claim.job_id, "the runner exited before the job finished")
            log.warning("runner for %s exited early; job now %s", claim.job_id, outcome)
            self.active = None

    def recover_orphans(self, conn: psycopg.Connection) -> None:
        """Jobs left RUNNING or CANCEL_REQUESTED by a previous supervisor, once their lease expired."""
        rows = conn.execute("SELECT id, status, deadline <= now() FROM investigations "
                            "WHERE status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_until <= now()").fetchall()
        for job_id, status, past_deadline in rows:
            if status == "CANCEL_REQUESTED":
                jobs.finish_cancellation(conn, job_id)
            elif past_deadline:
                reports.finalize_fallback(conn, job_id, "the 180-second deadline passed during recovery")
            else:
                jobs.requeue_for_recovery(conn, job_id, "the lease expired (the previous supervisor stopped)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", choices=["openshell", "local"], default="openshell",
                        help="openshell (sandboxed) or local (DEVELOPMENT MODE, NOT SANDBOXED, D14)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from incidentlens.server import test_overrides
    _, conninfo = test_overrides(load_settings())
    launcher = (OpenShellRunnerLauncher() if args.runner == "openshell"
                else LocalRunnerLauncher(f"http://127.0.0.1:{INTERNAL_PORT}"))
    supervisor = Supervisor(conninfo, launcher)

    def request_stop(signum, frame):
        supervisor.stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    sys.exit(supervisor.run())


if __name__ == "__main__":
    main()
