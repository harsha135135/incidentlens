"""Replay mode (decisions D51; protocol §1, §3): answer tool actions from a frozen evidence bundle.

A bundle is a directory with one envelope file per fixed-bundle tool and a manifest.json holding the
window, the case-run and a SHA-256 per file. A job is bound to the bundle whose window equals the
job's window exactly. Checksums are verified before every answer. Live sources are never read in
this mode: a job with no matching bundle, or a tool call with no recorded envelope, gets an
UNAVAILABLE observation.

Each replayed envelope keeps its recorded times, coverage and data, gets a fresh evidence ID (IDs
are primary keys) and records which bundle file it came from."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from incidentlens.tools.envelope import Window, build_envelope, parse_iso


class BundleError(Exception):
    """A bundle failed its checksum or manifest checks."""


def manifest(bundle: Path) -> dict:
    return json.loads((bundle / "manifest.json").read_text())


def verify(bundle: Path) -> dict:
    data = manifest(bundle)
    for entry in data["files"]:
        digest = hashlib.sha256((bundle / entry["file"]).read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise BundleError(f"{bundle.name}/{entry['file']} does not match its checksum")
    return data


def bundle_for_window(bundles: Path, window: Window) -> Path | None:
    for path in sorted(bundles.glob("*/manifest.json")):
        recorded = json.loads(path.read_text())["window"]
        if parse_iso(recorded["start"]) == window.start and parse_iso(recorded["end"]) == window.end:
            return path.parent
    return None


def replay_tool(bundles: Path, window: Window, tool: str, arguments: dict) -> dict:
    bundle = bundle_for_window(bundles, window)
    if bundle is None:
        return build_envelope(tool, arguments, window, "UNAVAILABLE", "replay", {},
                              ["No frozen evidence bundle matches this window; live sources are not read in replay mode."],
                              ["no bundle for window"])
    data = verify(bundle)
    entry = next((item for item in data["files"] if item["tool"] == tool and item["arguments"] == (arguments or {})), None)
    if entry is None:
        return build_envelope(tool, arguments, window, "UNAVAILABLE", "replay", {},
                              ["The frozen bundle has no observation for this tool and these arguments."],
                              ["not in bundle"])
    envelope = copy.deepcopy(json.loads((bundle / entry["file"]).read_text()))
    envelope["evidence_id"] = str(uuid.uuid4())
    envelope["source"] = dict(envelope.get("source") or {}, replayed_from=f"{bundle.name}/{entry['file']}",
                              bundle_sha256=entry["sha256"])
    return envelope
