"""Stage 3 check: does the worst-case model request fit the 6,000-token input limit?

Renders real saved observations from the Stage 1 scenario runs (12 of them: the most a job can
collect, since it has 12 tool actions), builds the same request the gateway sends (system prompt,
the tools offered in Stage 3, scope and evidence), and asks the provider's count endpoint. Counting
is free. Writes evaluation/results/stage3/prompt-budget-<timestamp>.json.

Usage: uv run python scripts/stage3_prompt_budget.py [scenarios-directory]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from incidentlens.actions import provider_tools  # noqa: E402
from incidentlens.config import MAX_INPUT_TOKENS_PER_CALL, PROJECT_ROOT  # noqa: E402
from incidentlens.evidence_render import evidence_section, render_observation  # noqa: E402
from incidentlens.model_gateway import (  # noqa: E402
    EVIDENCE_PROMPT_CHARS,
    OFFERED_TOOLS,
    SYSTEM_PROMPT,
    THINKING,
    chat_model,
    model_settings,
)


def saved_envelopes(directory: Path) -> list[tuple[str, dict, dict]]:
    found = []
    for path in sorted(directory.glob("*.json")):
        for envelope in json.loads(path.read_text())["observations"].values():
            found.append((envelope["tool"], envelope.get("arguments") or {}, envelope))
    return found


def main() -> int:
    default = sorted((PROJECT_ROOT / "evaluation" / "results" / "stage1").glob("scenarios-*"))[-1]
    directory = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default
    envelopes = saved_envelopes(directory)
    # Worst case: the largest renders first, 12 observations in all.
    items = sorted(((f"E{i + 1}", tool, args, env) for i, (tool, args, env) in enumerate(envelopes)),
                   key=lambda item: len(render_observation(*item)), reverse=True)[:12]
    items = [(f"E{i + 1}",) + item[1:] for i, item in enumerate(items)]
    rendered = [render_observation(*item) for item in items]
    scope = ("service: ledgercore-local\nsymptom (from the developer): Customers report transfer notifications "
             "arriving minutes late\nwindow: 2026-11-06T05:30:00+00:00 to 2026-11-06T05:45:00+00:00\n\nSaved observations:\n")
    section = evidence_section(items, EVIDENCE_PROMPT_CHARS)
    prompt = scope + section + "\n\nBudget used: 7 model actions, 12 tool actions.\nChoose the next action."
    settings = model_settings()
    llm = chat_model(settings)
    messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)]
    tools = provider_tools(available=OFFERED_TOOLS)
    worst = llm.get_num_tokens_from_messages(messages, tools=tools, thinking=THINKING)
    base = llm.get_num_tokens_from_messages([SystemMessage(SYSTEM_PROMPT), HumanMessage(scope)], tools=tools,
                                            thinking=THINKING)
    one = llm.get_num_tokens_from_messages([SystemMessage(SYSTEM_PROMPT), HumanMessage(scope + rendered[0])],
                                           tools=tools, thinking=THINKING)
    result = {"scenarios": str(directory.relative_to(PROJECT_ROOT)), "observations": len(rendered),
              "characters_per_observation": [len(text) for text in rendered],
              "tokens_without_evidence": base, "tokens_with_largest_observation": one,
              "tokens_worst_case": worst, "evidence_characters": len(section),
              "observations_shortened": section.count("shortened in this prompt"),
              "observations_reduced_to_header": section.count("omitted from this prompt"), "limit": MAX_INPUT_TOKENS_PER_CALL, "fits": worst <= MAX_INPUT_TOKENS_PER_CALL,
              "largest_rendered_observation": rendered[0]}
    out = PROJECT_ROOT / "evaluation" / "results" / "stage3"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"prompt-budget-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "largest_rendered_observation"}, indent=2))
    print(rendered[0])
    print(f"wrote {path.relative_to(PROJECT_ROOT)}")
    return 0 if result["fits"] else 1


if __name__ == "__main__":
    sys.exit(main())
