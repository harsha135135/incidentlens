"""Helpers for the live-stack scenario tests."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

from evaluation.controller import Controller, utc_now
from incidentlens.actions import DeliveryLogArguments, NoArguments, RequestMetricsArguments
from incidentlens.config import TARGET_SETTLE_SECONDS
from incidentlens.tools.envelope import Deadline, Window
from incidentlens.tools.registry import live_context, run_tool
from incidentlens.tools.sources import target_query


def wait_for_empty_outbox(settings, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while True:
        rows = target_query(settings.target_conninfo,
                            "SELECT count(*) FROM diagnostics.outbox_event_times WHERE state = 'PENDING'", (),
                            Deadline())
        if rows[0][0] == 0:
            return
        if time.monotonic() > deadline:
            raise AssertionError("outbox backlog did not drain before the scenario")
        time.sleep(1)


def observe(settings, store, window: Window, controller: Controller, results_dir: Path, name: str) -> dict:
    """Waits until the window has settled (D3) and the collector has read past it, runs the four
    Stage 1 tools over it, and saves the envelopes next to the controller's ground truth."""
    wait = TARGET_SETTLE_SECONDS + 6 - (utc_now() - window.end).total_seconds()
    if wait > 0:
        time.sleep(wait)
    context = live_context(window, store, settings)
    observations = {
        "requests_transfers": run_tool("get_request_metrics", {"route_group": "transfers"}, context),
        "requests_consumer": run_tool("get_request_metrics", RequestMetricsArguments(route_group="consumer")
                                      .model_dump(), context),
        "outbox": run_tool("get_outbox_stats", NoArguments().model_dump(), context),
        "delivery_logs": run_tool("search_delivery_logs",
                                  DeliveryLogArguments(outcome="any", limit=100).model_dump(), context),
        "network_errors": run_tool("search_delivery_logs",
                                   DeliveryLogArguments(outcome="network_error", limit=100).model_dump(), context),
        "timings": run_tool("get_delivery_timings", NoArguments().model_dump(), context),
    }
    record = {"label": "FIXTURE RESULT", "scenario": name, "window": window.as_json(),
              "ground_truth": [json.loads(line) for line in controller.ground_truth.read_text().splitlines()],
              "observations": observations}
    (results_dir / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    (controller.run_dir / "observations.json").write_text(json.dumps(record, indent=2) + "\n")
    return observations


def seconds_between(a, b) -> float:
    return (b - a).total_seconds()


def plus(moment, seconds: float):
    return moment + timedelta(seconds=seconds)
