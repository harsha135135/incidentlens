"""Starts and stops the local IncidentLens stack used for development, tests and evaluation.

  up [--consumer fixture|in-process]   databases, Prometheus, fixture consumer, LedgerCore, collector
  down [--volumes]                     stops everything (--volumes also deletes the databases)
  status                               shows what is running
  restart-ledgercore [--consumer ...] [--pool-size N]
  gateway up|down [--runner openshell|local]   IncidentLens's API/gateway and supervisor

Order (spec §20): IncidentLens PostgreSQL and Prometheus -> LedgerCore's PostgreSQL -> fixture
consumer -> LedgerCore (Flyway applies V6 at startup) -> diagnostics login provisioning -> collector.

Every child process gets only the environment it needs. The model API key is never passed to
LedgerCore, the fixture consumer or the collector.

Usage: uv run python scripts/stack.py up
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from incidentlens.config import (  # noqa: E402
    INCIDENTLENS_ENV_FILE,
    LEDGERCORE_ENV_FILE,
    LEDGERCORE_PORT,
    LOG_DIR,
    PROJECT_ROOT,
    STUB_EVENTS_PORT,
)
from incidentlens.database import apply_migrations, provision_logins  # noqa: E402

LEDGERCORE_DIR = Path(os.environ.get("LEDGERCORE_DIR", PROJECT_ROOT.parent / "ledgercore")).resolve()
LEDGERCORE_JAR = LEDGERCORE_DIR / "target" / "ledgercore-0.0.1-SNAPSHOT.jar"
RUN_DIR = LOG_DIR / "run"
TARGET_PROJECT = "incidentlens-target"
BASE_ENV_NAMES = ("PATH", "HOME", "LANG", "JAVA_HOME", "TMPDIR")


def compose_incidentlens(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(PROJECT_ROOT / "deploy" / "compose.yaml"),
            "--env-file", str(INCIDENTLENS_ENV_FILE), *args]


def compose_target(*args: str) -> list[str]:
    return ["docker", "compose", "-p", TARGET_PROJECT, "-f", str(LEDGERCORE_DIR / "compose.yaml"),
            "--env-file", str(LEDGERCORE_ENV_FILE), *args]


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("'").strip('"')
    return values


def base_env() -> dict[str, str]:
    return {name: os.environ[name] for name in BASE_ENV_NAMES if name in os.environ}


def run(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, **kwargs)


def ledgercore_revision() -> str:
    """The commit plus, when the working tree has changes, a hash of those changes. Uncommitted
    Stage 1 instrumentation must not be reported as the plain commit."""
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(LEDGERCORE_DIR), *args], check=True, capture_output=True).stdout

    head = git("rev-parse", "--short=12", "HEAD").decode().strip()
    if not git("status", "--porcelain").strip():
        return head
    digest = hashlib.sha256(git("diff", "HEAD", "--binary"))
    for name in sorted(git("ls-files", "--others", "--exclude-standard").decode().splitlines()):
        digest.update(name.encode())
        digest.update((LEDGERCORE_DIR / name).read_bytes())
    return f"{head}+dirty.{digest.hexdigest()[:12]}"


def ensure_jar() -> None:
    """Builds LedgerCore's jar when any source file is newer than it."""
    sources = [LEDGERCORE_DIR / "pom.xml", *(LEDGERCORE_DIR / "src" / "main").rglob("*")]
    newest = max(path.stat().st_mtime for path in sources if path.is_file())
    if not LEDGERCORE_JAR.exists() or LEDGERCORE_JAR.stat().st_mtime < newest:
        print("building LedgerCore jar ...")
        run(["./mvnw", "-q", "-DskipTests", "package"], cwd=LEDGERCORE_DIR)
        LEDGERCORE_JAR.touch()  # the plugin writes a fixed 1980 timestamp; mark the build time


def pid_file(name: str) -> Path:
    return RUN_DIR / f"{name}.pid"


def running_pid(name: str) -> int | None:
    path = pid_file(name)
    if not path.exists():
        return None
    pid = int(path.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        path.unlink()
        return None
    return pid


def start_process(name: str, command: list[str], env: dict[str, str], log_path: Path) -> int:
    if running_pid(name):
        raise SystemExit(f"{name} is already running (pid {running_pid(name)})")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as output:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   cwd=PROJECT_ROOT, start_new_session=True)
    pid_file(name).write_text(str(process.pid))
    return process.pid


def stop_process(name: str, timeout: float = 20.0) -> None:
    pid = running_pid(name)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGCONT)  # in case a fault left it paused
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        os.kill(pid, signal.SIGKILL)
    pid_file(name).unlink(missing_ok=True)


def wait_until(check, what: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what}")


def start_stub() -> None:
    env = base_env() | {
        "STUB_PUBLISHER_HASH": read_env_file(LEDGERCORE_ENV_FILE)["LEDGERCORE_PUBLISHER_PASSWORD_HASH"],
        "STUB_TIMING_LOG": str(LOG_DIR / "fixture-consumer" / "consumer.jsonl"),
    }
    start_process("fixture-consumer", [sys.executable, "evaluation/fixtures/consumer_stub/stub.py"], env,
                  LOG_DIR / "fixture-consumer" / "stdout.log")
    wait_until(lambda: httpx.get("http://127.0.0.1:18091/mode", timeout=1).status_code == 200, "fixture consumer")


def start_ledgercore(consumer: str, pool_size: int | None = None, connection_timeout_ms: int | None = None) -> None:
    ensure_jar()
    env = base_env() | read_env_file(LEDGERCORE_ENV_FILE) | {
        "LEDGERCORE_PORT": str(LEDGERCORE_PORT),
        "LEDGERCORE_LOG_FILE": str(LOG_DIR / "ledgercore" / "ledgercore.json"),
        "LEDGERCORE_DIAGNOSTICS_LOG_LEVEL": "INFO",
        "LEDGERCORE_REVISION": ledgercore_revision(),
        # Empty means LedgerCore's own in-process consumer (its original behaviour).
        "LEDGERCORE_CONSUMER_URL": f"http://127.0.0.1:{STUB_EVENTS_PORT}/events" if consumer == "fixture" else "",
    }
    hikari = {}
    if pool_size is not None:
        hikari["maximum-pool-size"] = pool_size
    if connection_timeout_ms is not None:
        hikari["connection-timeout"] = connection_timeout_ms
    if hikari:
        # Exact property names through SPRING_APPLICATION_JSON. The environment-variable form
        # SPRING_DATASOURCE_HIKARI_CONNECTION_TIMEOUT can bind as "connection.timeout", which calls
        # HikariDataSource.getConnection() during binding and seals the pool before it is configured.
        env["SPRING_APPLICATION_JSON"] = json.dumps({"spring": {"datasource": {"hikari": hikari}}})
    start_process("ledgercore", ["java", "-jar", str(LEDGERCORE_JAR)], env, LOG_DIR / "ledgercore" / "stdout.log")
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{LEDGERCORE_PORT}/actuator/health/readiness",
                                 timeout=2).status_code == 200, "LedgerCore readiness")


def provision_diagnostics_login() -> None:
    env = base_env() | {"LEDGERCORE_ENV_FILE": str(LEDGERCORE_ENV_FILE), "LEDGERCORE_COMPOSE_PROJECT": TARGET_PROJECT}
    run(["sh", str(LEDGERCORE_DIR / "scripts" / "provision-diagnostics.sh")], env=env)


def start_collector() -> None:
    env = base_env() | {"PYTHONPATH": str(PROJECT_ROOT / "src")}
    start_process("collector", [sys.executable, "-m", "incidentlens.collector"], env,
                  LOG_DIR / "collector" / "collector.log")


def start_gateway(runner: str = "openshell", replay: str | None = None) -> None:
    """IncidentLens's API/gateway (8088, 8089) and supervisor against the live stack. The gateway
    reads the model key itself from .env.local; it is not passed in the environment. With `replay`,
    the gateway runs in evaluation replay mode and answers tools from frozen bundles (D51)."""
    env = base_env() | {"PYTHONPATH": str(PROJECT_ROOT / "src")}
    if replay:
        env |= {"INCIDENTLENS_EVALUATION_MODE": "1", "INCIDENTLENS_REPLAY_BUNDLES": str(Path(replay).resolve())}
    if not running_pid("gateway"):
        start_process("gateway", [sys.executable, "-m", "incidentlens.server"], env, LOG_DIR / "gateway" / "gateway.log")
        wait_until(lambda: httpx.get("http://127.0.0.1:8088/health/ready", headers={"Host": "127.0.0.1:8088"},
                                     timeout=2).status_code == 200, "the IncidentLens gateway")
    if not running_pid("supervisor"):
        start_process("supervisor", [sys.executable, "-m", "incidentlens.supervisor", "--runner", runner], env,
                      LOG_DIR / "supervisor" / "supervisor.log")


def stop_gateway() -> None:
    for name in ("supervisor", "gateway"):
        stop_process(name)


def up(consumer: str) -> None:
    run(compose_incidentlens("up", "-d", "--wait"))
    run(compose_target("up", "-d", "--wait"))
    from incidentlens.config import load_settings
    settings = load_settings()
    applied = apply_migrations(settings.owner_conninfo)
    print(f"IncidentLens migrations applied now: {applied or 'none'}")
    secrets = read_env_file(INCIDENTLENS_ENV_FILE)
    provision_logins(settings.owner_conninfo, {
        "incidentlens_app": secrets["INCIDENTLENS_APP_DB_PASSWORD"],
        "incidentlens_collector": secrets["INCIDENTLENS_COLLECTOR_DB_PASSWORD"]})
    if consumer == "fixture" and not running_pid("fixture-consumer"):
        start_stub()
    if not running_pid("ledgercore"):
        start_ledgercore(consumer)
    provision_diagnostics_login()
    if not running_pid("collector"):
        start_collector()
    status()


def down(volumes: bool) -> None:
    stop_gateway()
    for name in ("collector", "ledgercore", "fixture-consumer"):
        stop_process(name)
    extra = ["-v"] if volumes else []
    run(compose_target("down", *extra))
    run(compose_incidentlens("down", *extra))


def restart_ledgercore(consumer: str, pool_size: int | None, connection_timeout_ms: int | None = None) -> None:
    stop_process("ledgercore")
    if consumer == "fixture" and not running_pid("fixture-consumer"):
        start_stub()
    start_ledgercore(consumer, pool_size, connection_timeout_ms)


def status() -> None:
    for name in ("fixture-consumer", "ledgercore", "collector", "gateway", "supervisor"):
        pid = running_pid(name)
        print(f"{name:17} {'running, pid ' + str(pid) if pid else 'stopped'}")
    print(f"ledgercore revision {ledgercore_revision()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    up_parser = commands.add_parser("up")
    up_parser.add_argument("--consumer", choices=["fixture", "in-process"], default="fixture")
    down_parser = commands.add_parser("down")
    down_parser.add_argument("--volumes", action="store_true")
    commands.add_parser("status")
    gateway = commands.add_parser("gateway", help="start or stop the IncidentLens gateway and supervisor")
    gateway.add_argument("action", choices=["up", "down"])
    gateway.add_argument("--runner", choices=["openshell", "local"], default="openshell")
    gateway.add_argument("--replay", help="evaluation replay mode: directory of frozen bundles (D51)")
    restart = commands.add_parser("restart-ledgercore")
    restart.add_argument("--consumer", choices=["fixture", "in-process"], default="fixture")
    restart.add_argument("--pool-size", type=int)
    restart.add_argument("--connection-timeout-ms", type=int)
    args = parser.parse_args()
    if args.command == "up":
        up(args.consumer)
    elif args.command == "down":
        down(args.volumes)
    elif args.command == "status":
        status()
    elif args.command == "gateway":
        start_gateway(args.runner, args.replay) if args.action == "up" else stop_gateway()
    else:
        restart_ledgercore(args.consumer, args.pool_size, args.connection_timeout_ms)


if __name__ == "__main__":
    main()
