"""The model's action surface: exactly one action per model turn (spec §8).

The model is offered one tool per diagnostic tool, plus `submit_report` and
`stop_inconclusive`. Whatever it returns is converted here into exactly one of three
actions (call a diagnostic tool, submit a report, or stop as inconclusive). Anything else
(no action, two actions, an unknown name or invalid arguments) is a MalformedAction,
which the gateway may repair once and otherwise turns into an inconclusive report.

Schemas sent to the provider and the Pydantic models used to validate replies are kept
side by side so they cannot drift apart unnoticed; tests compare them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ---- Diagnostic tool arguments (spec §9). Job scope (service, window) is never an argument.


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestMetricsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route_group: Literal["transfers", "consumer"]


class DeliveryLogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["any", "success", "auth_error", "network_error", "server_error"]
    limit: int = Field(ge=1, le=100)


class DatabaseBlockerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["historical", "current"]
    limit: int = Field(ge=1, le=50)


DIAGNOSTIC_TOOLS: dict[str, tuple[str, type[BaseModel]]] = {
    "get_request_metrics": (
        "HTTP request rate, status counts and latency histograms for one route group in the job window.",
        RequestMetricsArguments,
    ),
    "get_outbox_stats": (
        "Outbox pending/failed gauges plus durable committed-arrival and recorded-publication counts.",
        NoArguments,
    ),
    "search_delivery_logs": (
        "Structured outbox delivery-attempt log rows in the job window, filtered by outcome.",
        DeliveryLogArguments,
    ),
    "get_pool_metrics": (
        "Connection pool active/idle/max, pending borrowers, acquisition timeouts and scrape gaps.",
        NoArguments,
    ),
    "get_database_blockers": (
        "Sampled database blocking relationships; historical (within the window) or current.",
        DatabaseBlockerArguments,
    ),
    "get_delivery_timings": (
        "Correlated total, authentication and handler durations for consumer deliveries.",
        NoArguments,
    ),
    "get_runtime_config": (
        "Allowlisted runtime settings (poll interval, batch size, auth mode) and their effective time.",
        NoArguments,
    ),
}

# ---- Report payload (spec §14). Lengths are enforced here, after the provider returns.


# Report limits. Hard bounds are the spec's (§14): summary 1,000 characters, 8 findings,
# 10 citations per finding, 10 alternatives, limitations and next checks, strings 2,000 characters.
# A report that large cannot fit in one call's 1,500 output tokens (§13), and real Stage 0 reports
# hit that output limit (decisions D27). So the descriptions, which the model sees, ask for a compact
# report; the hard bounds stay as validated. A report cut off at the output limit is a malformed
# action: one repair ("resubmit shorter"), then the deterministic inconclusive fallback.
SUMMARY_CHARS, SUMMARY_TARGET = 1_000, 250
FINDINGS_MAX, FINDINGS_TARGET = 8, 3
CITATIONS_MAX = 10
ENTRIES_MAX = 10  # alternatives, limitations, next checks
ALTERNATIVES_TARGET = 2
NOTES_TARGET = 2
TEXT_CHARS, TEXT_TARGET = 2_000, 160
EVIDENCE_HANDLE = "an evidence handle shown with a saved observation, such as E2"


def target(text: str) -> str:
    return f"{text} Keep the whole report compact: it must fit in one 1,500-token reply."


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=1, max_length=TEXT_CHARS,
                       description=f"One claim, ideally under {TEXT_TARGET} characters.")
    support: Literal["observed", "inferred"] = Field(
        description="observed: a saved observation shows it; inferred: reasoned from observations.")
    evidence_ids: list[str] = Field(min_length=1, max_length=CITATIONS_MAX,
                                    description=f"At least one citation, each {EVIDENCE_HANDLE}.")
    reason: str = Field(min_length=1, max_length=TEXT_CHARS,
                        description=f"Why the cited evidence supports the claim, ideally under {TEXT_TARGET} characters.")


class Alternative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=TEXT_CHARS,
                            description=f"Another possible cause, ideally under {TEXT_TARGET} characters.")
    assessment: Literal["supported", "weakened", "unassessed"]
    evidence_ids: list[str] = Field(max_length=CITATIONS_MAX, description=f"Citations, each {EVIDENCE_HANDLE}.")
    reason: str = Field(min_length=1, max_length=TEXT_CHARS,
                        description=f"Ideally under {TEXT_TARGET} characters.")


class ReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["SUPPORTED", "TENTATIVE", "INCONCLUSIVE", "NO_FAULT_OBSERVED"]
    summary: str = Field(min_length=1, max_length=SUMMARY_CHARS,
                         description=target(f"Ideally under {SUMMARY_TARGET} characters."))
    findings: list[Finding] = Field(max_length=FINDINGS_MAX,
                                    description=target(f"Usually {FINDINGS_TARGET} or fewer findings."))
    alternatives: list[Alternative] = Field(max_length=ENTRIES_MAX,
                                            description=f"Usually {ALTERNATIVES_TARGET} or fewer.")
    limitations: list[str] = Field(max_length=ENTRIES_MAX,
                                   description=f"Usually {NOTES_TARGET} or fewer, each under {TEXT_TARGET} characters.")
    next_checks: list[str] = Field(max_length=ENTRIES_MAX,
                                   description=f"Usually {NOTES_TARGET} or fewer, each under {TEXT_TARGET} characters.")

    @field_validator("limitations", "next_checks")
    @classmethod
    def bounded_notes(cls, notes: list[str]) -> list[str]:
        if any(not note or len(note) > TEXT_CHARS for note in notes):
            raise ValueError(f"each item must be 1 to {TEXT_CHARS} characters")
        return notes


class StopArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=TEXT_CHARS,
                        description=f"Ideally under {SUMMARY_TARGET} characters.")
    missing_evidence: list[str] = Field(max_length=ENTRIES_MAX,
                                        description=f"Usually {NOTES_TARGET} or fewer items.")


# ---- The three action kinds the rest of the system sees.


@dataclass(frozen=True)
class CallTool:
    tool: str
    arguments: dict


@dataclass(frozen=True)
class SubmitReport:
    report: ReportPayload


@dataclass(frozen=True)
class StopInconclusive:
    reason: str
    missing_evidence: list[str]


@dataclass(frozen=True)
class MalformedAction:
    problem: str


Action = CallTool | SubmitReport | StopInconclusive | MalformedAction


def _strict_schema(model: type[BaseModel]) -> dict:
    """A provider schema for strict tool use: every field required, no extra fields.

    Numeric and length limits are not sent; they are enforced by the Pydantic models when
    the reply is validated, so a violation becomes a MalformedAction instead of a 400.
    """
    schema = model.model_json_schema()
    return _strip_limits(schema)


def _strip_limits(node):
    if isinstance(node, dict):
        cleaned = {key: _strip_limits(value) for key, value in node.items()
                   if key not in ("minimum", "maximum", "minLength", "maxLength", "minItems",
                                  "maxItems", "title", "default")}
        if cleaned.get("type") == "object":
            cleaned["additionalProperties"] = False
            cleaned.setdefault("properties", {})
            cleaned["required"] = list(cleaned["properties"])
        return cleaned
    if isinstance(node, list):
        return [_strip_limits(item) for item in node]
    return node


def provider_tools(available: list[str] | None = None) -> list[dict]:
    """Tool definitions offered to the model: the permitted diagnostic tools plus the two
    terminal actions. `available` narrows the diagnostic tools to the job's capabilities.

    "strict" is set on each definition: langchain-anthropic passes tools that are already in
    Anthropic format through unchanged and ignores bind_tools(strict=True) for them (decisions D17)."""
    names = available if available is not None else list(DIAGNOSTIC_TOOLS)
    tools = []
    for name in names:
        description, arguments = DIAGNOSTIC_TOOLS[name]
        tools.append({"name": name, "description": description, "input_schema": _strict_schema(arguments),
                      "strict": True})
    tools.append({
        "name": "submit_report",
        "description": ("Finish with a diagnosis report. The whole reply must fit in 1,500 output tokens, so be "
                        f"compact: usually {FINDINGS_TARGET} or fewer findings, {ALTERNATIVES_TARGET} alternatives, "
                        f"{NOTES_TARGET} limitations and {NOTES_TARGET} next checks, each text under {TEXT_TARGET} "
                        "characters. Every finding cites at least one evidence handle (such as E2)."),
        "input_schema": _strict_schema(ReportPayload),
        "strict": True,
    })
    tools.append({
        "name": "stop_inconclusive",
        "description": ("Stop without a diagnosis and name the evidence that would be needed. Keep it short: "
                        f"reason under {SUMMARY_TARGET} characters, {NOTES_TARGET} or fewer missing items."),
        "input_schema": _strict_schema(StopArguments),
        "strict": True,
    })
    return tools


def parse_action(tool_calls: list[dict], allowed_tools: list[str] | None = None) -> Action:
    """Turns the provider's tool calls into exactly one action, or a MalformedAction.

    `tool_calls` uses LangChain's shape: [{"name": ..., "args": {...}, "id": ...}].
    """
    if not tool_calls:
        return MalformedAction("the reply contained no action")
    if len(tool_calls) > 1:
        return MalformedAction(f"the reply proposed {len(tool_calls)} actions; exactly one is allowed")
    name = tool_calls[0].get("name")
    arguments = tool_calls[0].get("args") or {}
    permitted = allowed_tools if allowed_tools is not None else list(DIAGNOSTIC_TOOLS)
    try:
        if name in permitted:
            validated = DIAGNOSTIC_TOOLS[name][1].model_validate(arguments)
            return CallTool(tool=name, arguments=validated.model_dump())
        if name == "submit_report":
            return SubmitReport(report=ReportPayload.model_validate(arguments))
        if name == "stop_inconclusive":
            stop = StopArguments.model_validate(arguments)
            return StopInconclusive(reason=stop.reason, missing_evidence=stop.missing_evidence)
    except ValidationError as error:
        return MalformedAction(f"invalid arguments for {name}: {error.error_count()} validation error(s)")
    return MalformedAction(f"unknown or unavailable action {name!r}")
