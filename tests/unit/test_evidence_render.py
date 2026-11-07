"""Model-facing evidence rendering: the decisive numbers, coverage and limitations are always
present, shortening is explicit, and the newest observations keep their full form (spec §13)."""

from __future__ import annotations

import json
from pathlib import Path

from incidentlens.evidence_render import evidence_section, render_observation

SCENARIO = Path(__file__).resolve().parents[2] / "evaluation/results/stage1/scenarios-20261106T054219Z/slow_consumer.json"


def saved() -> list[tuple[str, str, dict, dict]]:
    observations = json.loads(SCENARIO.read_text())["observations"]
    return [(f"E{i + 1}", env["tool"], env.get("arguments") or {}, env) for i, env in enumerate(observations.values())]


def test_outbox_render_keeps_rates_coverage_and_a_link_to_the_saved_observation():
    handle, tool, arguments, envelope = next(item for item in saved() if item[1] == "get_outbox_stats")
    text = render_observation(handle, tool, arguments, envelope)
    counts = envelope["data"]["durable_counts"]
    assert f"({counts['arrivals_per_second']}/s)" in text and f"({counts['publications_per_second']}/s)" in text
    assert f"coverage {envelope['coverage']}" in text
    assert text.endswith(f"(full observation saved as {handle})")


def test_delivery_logs_render_is_short_even_for_a_large_observation():
    item = next(item for item in saved() if item[1] == "search_delivery_logs" and item[3]["data"]["records"])
    raw = len(json.dumps(item[3]["data"]))
    text = render_observation(*item)
    assert raw > 10_000 and len(text) < 1_500
    assert "matching records" in text and "record check" in text


def test_shortening_is_explicit_oldest_first_and_within_budget():
    items = saved() * 2  # twelve observations
    items = [(f"E{i + 1}",) + item[1:] for i, item in enumerate(items)]
    full = "\n".join(render_observation(*item) for item in items)
    section = evidence_section(items, 6_000)
    assert len(full) > 6_000 >= len(section)
    blocks = section.split("\n[")
    assert "shortened in this prompt" in blocks[0]  # the oldest is shortened first
    assert "full observation saved as E12" in blocks[-1]  # the newest keeps its full render
    for handle, *_ in items:  # every observation is still named
        assert f"[{handle}]" in section


def test_a_tiny_budget_reduces_old_observations_to_headers_but_never_drops_them():
    items = [(f"E{i + 1}",) + item[1:] for i, item in enumerate(saved() * 2)]
    section = evidence_section(items, 1_500)
    assert section.count("omitted from this prompt") >= 1
    assert all(f"[{handle}]" in section for handle, *_ in items)


def test_an_unavailable_observation_says_so():
    text = render_observation("E1", "get_outbox_stats", {}, {"coverage": "UNAVAILABLE", "truncated": False, "data": {},
                                                             "limitations": ["Prometheus could not be queried."],
                                                             "errors": ["Prometheus unreachable"]})
    assert "no data: the source was unavailable" in text and "Prometheus unreachable" in text
