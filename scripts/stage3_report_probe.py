"""Stage 3 contract probe: with evidence rendered exactly as the gateway renders it (real saved
observations from a Stage 1 scenario, FIXTURE RESULT), does a full submit_report fit in one
1,500-token reply? Only the terminal actions are offered, so the model must report or stop.

This checks size and schema only. It is not an evaluation of diagnostic quality.

Usage: uv run python scripts/stage3_report_probe.py [scenario.json] [calls]
Writes evaluation/results/stage3/report-probe-<timestamp>.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from incidentlens.actions import MalformedAction, SubmitReport, parse_action, provider_tools  # noqa: E402
from incidentlens.config import PROJECT_ROOT  # noqa: E402
from incidentlens.evidence_render import evidence_section  # noqa: E402
from incidentlens.model_gateway import (  # noqa: E402
    EVIDENCE_PROMPT_CHARS,
    SYSTEM_PROMPT,
    THINKING,
    chat_model,
    model_settings,
)

DEFAULT = PROJECT_ROOT / "evaluation/results/stage1/scenarios-20261106T054219Z/slow_consumer.json"


def main() -> int:
    scenario = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT
    calls = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    record = json.loads(scenario.read_text())
    items = [(f"E{i + 1}", env["tool"], env.get("arguments") or {}, env)
             for i, env in enumerate(record["observations"].values())]
    window = record["window"]
    prompt = (f"service: ledgercore-local\nsymptom (from the developer): Transfers succeed but notifications "
              f"are delayed\nwindow: {window['start']} to {window['end']}\n\nSaved observations:\n"
              + evidence_section(items, EVIDENCE_PROMPT_CHARS)
              + "\n\nThe diagnostic budget is used up: submit your report now, or stop as inconclusive.")
    llm = chat_model(model_settings())
    tools = provider_tools(available=[])
    bound = llm.bind_tools(tools, strict=True, tool_choice="auto", parallel_tool_calls=False)
    messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
    estimate = llm.get_num_tokens_from_messages(messages, tools=tools, thinking=THINKING)
    results = []
    for _ in range(calls):
        started = time.perf_counter()
        reply = bound.invoke(messages)
        action = parse_action(reply.tool_calls, allowed_tools=[])
        meta, usage = reply.response_metadata or {}, reply.usage_metadata or {}
        results.append({
            "stop_reason": meta.get("stop_reason"), "output_tokens": usage.get("output_tokens"),
            "input_tokens": usage.get("input_tokens"), "latency_seconds": round(time.perf_counter() - started, 2),
            "action": type(action).__name__,
            "problem": action.problem if isinstance(action, MalformedAction) else None,
            "report": action.report.model_dump() if isinstance(action, SubmitReport) else None,
        })
    out = PROJECT_ROOT / "evaluation" / "results" / "stage3"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"report-probe-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"label": "FIXTURE RESULT: size and schema probe, not a quality evaluation",
                                "scenario": str(scenario.relative_to(PROJECT_ROOT)), "estimated_input_tokens": estimate,
                                "prompt": prompt, "calls": results}, indent=2) + "\n")
    for item in results:
        report = item["report"] or {}
        print({k: item[k] for k in ("stop_reason", "output_tokens", "action", "problem")},
              "| outcome:", report.get("outcome"), "| summary:", (report.get("summary") or "")[:140])
    print(f"estimated input tokens {estimate}; wrote {path.relative_to(PROJECT_ROOT)}")
    return 0 if all(item["action"] in ("SubmitReport", "StopInconclusive") for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
