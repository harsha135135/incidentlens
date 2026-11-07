"""The runner inside a real OpenShell sandbox (deploy/openshell/runner-policy.yaml), with the
gateway and supervisor as real processes. The provider is still the loopback fake here; the
real-provider end-to-end run is in the Stage 3 pilot. Needs the OpenShell gateway running."""

from __future__ import annotations

import subprocess
import time

from tests.process.conftest import Process, create_job, job_row, report_step, wait_until


def sandboxes() -> str:
    return subprocess.run(["openshell", "sandbox", "list"], capture_output=True, text=True, timeout=30).stdout


def test_a_full_investigation_with_the_runner_in_an_openshell_sandbox(owner, client, gateway, env, tmp_path, fake):
    supervisor = Process("supervisor", env, tmp_path, ["--runner", "openshell"]).start()
    try:
        wait_until(lambda: owner.execute("SELECT supervisor_heartbeat_at IS NOT NULL FROM control").fetchone()[0],
                   "the supervisor's first heartbeat", 20)
        fake.set_script([report_step("E1", "E3")])
        job_id = create_job(client)
        claimed = time.monotonic()
        wait_until(lambda: owner.execute("SELECT 1 FROM actions WHERE investigation_id = %s", (job_id,)).fetchone(),
                   "the sandboxed runner's first action", 120, 0.1)
        startup = time.monotonic() - claimed
        wait_until(lambda: job_row(owner, job_id)[0] == "COMPLETED", "the sandboxed job to complete", 120)
        sandbox = owner.execute("SELECT sandbox_name FROM investigations WHERE id = %s", (job_id,)).fetchone()[0]
        (tmp_path / "sandbox-startup-seconds.txt").write_text(f"{startup:.2f}\n")
        print(f"sandbox {sandbox}: claim to first action {startup:.2f} s")
        assert startup < 60, f"sandbox start took {startup:.1f} s of the 180 s deadline"
        report = client.get(f"/api/v1/investigations/{job_id}/report").json()
        assert report["source"] == "model" and report["report"]["findings"][0]["evidence_handles"] == ["E1", "E3"]
        wait_until(lambda: sandbox not in sandboxes(), "the sandbox to be deleted", 60, 1.0)
    finally:
        supervisor.stop()
