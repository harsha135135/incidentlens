"""Baseline A's fixed rules on real saved observations from the Stage 1 fixture scenarios, whose
ground truth is known, and the pilot's provisional report classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.baselines import PILOT_BUNDLE, baseline_a, classify_report

SCENARIOS = Path(__file__).resolve().parents[2] / "evaluation/results/stage1/scenarios-20261106T054219Z"
KEYS = {"get_request_metrics": "requests_transfers", "get_outbox_stats": "outbox",
        "search_delivery_logs": "delivery_logs", "get_delivery_timings": "timings"}


def bundle(scenario: str) -> list[tuple]:
    observations = json.loads((SCENARIOS / f"{scenario}.json").read_text())["observations"]
    return [(f"E{i + 1}", tool, arguments, observations[KEYS[tool]]) for i, (tool, arguments) in enumerate(PILOT_BUNDLE)]


@pytest.mark.parametrize("scenario, cause", [("healthy", "none"), ("unavailable_consumer", "consumer_unavailable"),
                                             ("slow_consumer", "slow_consumer_handler")])
def test_rules_classify_the_known_fixture_scenarios(scenario, cause):
    result = baseline_a(bundle(scenario))
    assert result["primary_cause"] == cause, result


def test_an_unavailable_source_is_insufficient_evidence_not_healthy():
    items = bundle("healthy")
    handle, tool, arguments, envelope = items[1]
    items[1] = (handle, tool, arguments, envelope | {"coverage": "UNAVAILABLE"})
    assert baseline_a(items)["primary_cause"] == "insufficient_evidence"


def test_the_classifier_reads_outcomes_and_mechanisms():
    assert classify_report(None) == "no_report"
    assert classify_report({"outcome": "NO_FAULT_OBSERVED", "summary": "fine", "findings": []}) == "none"
    assert classify_report({"outcome": "SUPPORTED", "summary": "Deliveries failed: connection refused by the consumer.",
                            "findings": []}) == "consumer_unavailable"
    assert classify_report({"outcome": "SUPPORTED", "summary": "Each delivery spends 400 ms in the consumer handler.",
                            "findings": []}) == "slow_consumer_handler"
