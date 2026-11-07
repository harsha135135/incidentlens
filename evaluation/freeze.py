"""Freeze records (evaluation/protocol.md §8). A freeze is a SHA-256 manifest of every code file that
affects recording, replay or scoring, plus LedgerCore's working-tree revision, the model ID, the
prompt version and the investigation mode. The harness refuses to run when the current manifest
differs from the active freeze (decisions D46: code must not change during an experiment).

Usage: PYTHONPATH=src:. uv run python evaluation/freeze.py write development|held_out
       PYTHONPATH=src:. uv run python evaluation/freeze.py check
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = {".py", ".sql", ".yaml", ".yml", ".toml", ".html", ".css", ".js", ".sh", ".lock", ".json"}
INCLUDE = ["src", "evaluation", "migrations", "deploy", "scripts", "pyproject.toml", "uv.lock"]
EXCLUDE_PARTS = {"results", "runs", "bundles", "local", "__pycache__", ".pytest_cache"}
FREEZE_FILES = {"development": ROOT / "evaluation" / "freeze-dev.json",
                "held_out": ROOT / "evaluation" / "freeze-heldout.json"}


def code_files() -> list[Path]:
    files = []
    for name in INCLUDE:
        path = ROOT / name
        candidates = [path] if path.is_file() else path.rglob("*")
        for candidate in candidates:
            parts = set(candidate.relative_to(ROOT).parts)
            if candidate.is_file() and candidate.suffix in CODE_SUFFIXES and not EXCLUDE_PARTS & parts \
                    and not candidate.name.startswith("freeze-"):
                files.append(candidate)
    return sorted(files)


def current() -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    import stack  # noqa: E402
    from incidentlens import model_gateway  # noqa: E402
    return {
        "files": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in code_files()},
        "ledgercore_revision": stack.ledgercore_revision(),
        "model_id": model_gateway.MODEL_ID,
        "prompt_version": model_gateway.PROMPT_VERSION,
        "investigation_mode": model_gateway.INVESTIGATION_MODE,
        "protocol_sha256": hashlib.sha256((ROOT / "evaluation" / "protocol.md").read_bytes()).hexdigest(),
    }


def active() -> tuple[str, dict] | None:
    for name in ("held_out", "development"):
        if FREEZE_FILES[name].exists():
            return name, json.loads(FREEZE_FILES[name].read_text())
    return None


def differences(frozen: dict, now: dict) -> list[str]:
    found = []
    for key in ("ledgercore_revision", "model_id", "prompt_version", "investigation_mode", "protocol_sha256"):
        if frozen[key] != now[key]:
            found.append(f"{key}: {frozen[key]} -> {now[key]}")
    for path in sorted(set(frozen["files"]) | set(now["files"])):
        if frozen["files"].get(path) != now["files"].get(path):
            found.append(f"file changed: {path}")
    return found


def check() -> list[str]:
    """Differences between the tree now and the active freeze; empty means it matches."""
    freeze = active()
    if freeze is None:
        return ["no freeze has been written"]
    return differences(freeze[1], current())


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "write":
        name = sys.argv[2]
        record = current() | {"freeze": name, "written_at": datetime.now(timezone.utc).isoformat()}
        FREEZE_FILES[name].write_text(json.dumps(record, indent=2) + "\n")
        print(f"wrote {FREEZE_FILES[name].relative_to(ROOT)}: {len(record['files'])} files")
        return 0
    problems = check()
    print("\n".join(problems) if problems else "matches the active freeze")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
