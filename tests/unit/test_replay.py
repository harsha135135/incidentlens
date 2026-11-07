"""Replay mode (D51): exact window binding, checksum verification, fresh evidence IDs, and no live
fallback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from incidentlens.tools.envelope import Window
from incidentlens.tools.replay import BundleError, replay_tool

START = datetime(2026, 11, 6, 12, 0, 0, 123000, tzinfo=timezone.utc)
WINDOW = Window(START, START + timedelta(seconds=150))


def make_bundle(root, envelope):
    bundle = root / "case-1"
    bundle.mkdir()
    text = json.dumps(envelope)
    (bundle / "E2-get_outbox_stats.json").write_text(text)
    (bundle / "manifest.json").write_text(json.dumps({
        "window": WINDOW.as_json(), "files": [{"file": "E2-get_outbox_stats.json", "tool": "get_outbox_stats",
                                               "arguments": {}, "sha256": hashlib.sha256(text.encode()).hexdigest()}]}))
    return bundle


def envelope():
    return {"evidence_id": "11111111-1111-4111-8111-111111111111", "tool": "get_outbox_stats", "coverage": "COMPLETE",
            "collected_at": "2026-11-06T12:03:00.000Z", "truncated": False, "data": {"n": 1}, "limitations": [],
            "errors": [], "source": {"kind": "prometheus+postgres_aggregate"}}


def test_a_matching_window_replays_the_recorded_envelope_with_a_fresh_id(tmp_path):
    make_bundle(tmp_path, envelope())
    replayed = replay_tool(tmp_path, WINDOW, "get_outbox_stats", {})
    assert replayed["evidence_id"] != envelope()["evidence_id"]
    assert replayed["collected_at"] == "2026-11-06T12:03:00.000Z" and replayed["data"] == {"n": 1}
    assert replayed["source"]["replayed_from"] == "case-1/E2-get_outbox_stats.json"


def test_a_tampered_file_is_refused(tmp_path):
    bundle = make_bundle(tmp_path, envelope())
    (bundle / "E2-get_outbox_stats.json").write_text(json.dumps(envelope() | {"data": {"n": 2}}))
    with pytest.raises(BundleError):
        replay_tool(tmp_path, WINDOW, "get_outbox_stats", {})


def test_no_bundle_or_no_recorded_tool_is_unavailable_never_live(tmp_path):
    make_bundle(tmp_path, envelope())
    other = Window(START + timedelta(seconds=1), START + timedelta(seconds=151))
    assert replay_tool(tmp_path, other, "get_outbox_stats", {})["coverage"] == "UNAVAILABLE"
    assert replay_tool(tmp_path, WINDOW, "get_pool_metrics", {})["coverage"] == "UNAVAILABLE"
