"""Stage 5 scoring (evaluation/protocol.md §5, §9, §10) and the blinded review sheet. FIXTURE RESULTS.

Usage:
  PYTHONPATH=src:. uv run python evaluation/score.py sheet       writes the held-out review sheet
  PYTHONPATH=src:. uv run python evaluation/score.py score development|held_out
  PYTHONPATH=src:. uv run python evaluation/score.py score development --prompt-version V
      scores P's development replay under prompt version V (protocol §7) instead of its first run

The review sheet (evaluation/results/stage5/review/):
  sheet.html     what the owner reads: P's reports and a sample of findings, with no case, category,
                 ground truth or system shown
  picks.csv      the owner fills `pick` (1-8) for each report
  findings.csv   the owner fills `judgement` (supported / unsupported / unclear) for each finding
  key.json       maps sheet ids back to case-runs; the owner should not open it before marking
"""

from __future__ import annotations

import csv
import html
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evaluation import cases  # noqa: E402
from evaluation.stage5 import CASE_RUNS, RESULTS  # noqa: E402
from incidentlens.config import load_settings  # noqa: E402
from incidentlens.database import connect  # noqa: E402
from incidentlens.evidence_render import render_observation  # noqa: E402

FAULTS = ("consumer_unavailable", "slow_consumer", "credential_rejection", "lock_contention", "lock_pool_exhaustion")
REVIEW = RESULTS / "review"
SHEET_SEED = 20261106
FINDINGS_SAMPLE = 100
PICKS = {"1": "consumer_unavailable", "2": "slow_consumer_handler", "3": "consumer_credential_rejected",
         "4": "lock_contention", "5": "lock_contention", "6": "pool_saturation", "7": "other", "8": "none"}
PICK_TEXT = ["consumer refused connections", "slow consumer handler", "credential rejection (401)", "lock contention",
             "lock contention exhausting the connection pool",
             "the connection pool (its exhaustion, size or timeout), not attributed to lock contention",
             "other cause", "no cause named"]


# ---- loading -----------------------------------------------------------------------------------

def load(split: str, prompt_version: str | None = None) -> list[dict]:
    """Case-run states of a split. With prompt_version, P is taken from that development replay
    (a case-run without one has no P and is left out of P's rates)."""
    rows = []
    for path in sorted(CASE_RUNS.glob("*/state.json")):
        state = json.loads(path.read_text())
        if state["split"] == split:
            if prompt_version:
                state["P"] = (state.get("P_replays") or {}).get(prompt_version)
            rows.append(state)
    return rows


def read_csv(path: Path, column: str) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open() as handle:
        return {row["id"]: (row.get(column) or "").strip() for row in csv.DictReader(handle)}


def picks_for_case_runs() -> tuple[dict[str, str], str]:
    """Blind picks keyed by case-run, and where they came from. The owner's picks are used when every
    report has one; otherwise the author's (labelled unblinded, protocol §5.1)."""
    key = json.loads((REVIEW / "key.json").read_text()) if (REVIEW / "key.json").exists() else {"reports": {}}
    for source, file_name in (("owner, blind", "picks.csv"), ("author, unblinded", "author-picks.csv")):
        picks = read_csv(REVIEW / file_name, "pick")
        if picks and all(picks.get(sheet_id) in PICKS for sheet_id in key["reports"]):
            return {key["reports"][sheet_id]: pick for sheet_id, pick in picks.items()}, source
    return {}, "none"


# ---- per-run judgements ------------------------------------------------------------------------

def report_of(state: dict) -> dict:
    return ((state.get("P") or {}).get("report")) or {}


def p_cause(state: dict, picks: dict[str, str]) -> tuple[str, bool]:
    """(root-cause label, causal depth) for P: the blind pick when there is one. Development falls back
    to the keyword aid; held-out never does (protocol §5.1)."""
    pick = picks.get(state["case_run"])
    if pick:
        return PICKS[pick], pick == "5"
    if state["split"] == "held_out":
        return "not picked", False
    return (state.get("P") or {}).get("classified_dev_aid") or "no_report", False


def cause_correct(category: str, label: str | None, strict: bool = True) -> bool:
    expected = {"consumer_unavailable": {"consumer_unavailable"}, "slow_consumer": {"slow_consumer_handler"},
                "credential_rejection": {"consumer_credential_rejected"}, "lock_contention": {"lock_contention"},
                "lock_pool_exhaustion": {"lock_contention"} if strict else {"lock_contention", "pool_saturation"}}
    return label in expected[category]


def outcome(state: dict, system: str) -> str | None:
    if system == "A":
        return (state.get("A") or {}).get("outcome")
    return report_of(state).get("outcome")


# ---- rates and case-clustered bootstrap (protocol §9) ------------------------------------------

def rate(rows: list[dict], hit) -> dict:
    hits = [bool(hit(row)) for row in rows]
    by_case = defaultdict(list)
    for row, value in zip(rows, hits):
        by_case[row["template"]].append(value)
    groups = list(by_case.values())
    result = {"k": sum(hits), "n": len(hits), "rate": round(sum(hits) / len(hits), 3) if hits else None,
              "cases": len(groups)}  # ci95 below collapses to a point when every run agrees; read it with k and n
    if len(groups) >= 2:
        rng = random.Random(SHEET_SEED)
        draws = []
        for _ in range(10_000):
            sample = [value for _ in groups for value in rng.choice(groups)]
            draws.append(sum(sample) / len(sample))
        draws.sort()
        result["ci95"] = [round(draws[249], 3), round(draws[9_749], 3)]
    return result


def score(split: str, prompt_version: str | None = None) -> dict:
    rows = load(split, prompt_version)
    picks, pick_source = picks_for_case_runs() if split == "held_out" else ({}, "development: automatic keyword aid, not a reading (the author's reading is in TODO.md)")
    valid = [row for row in rows if row.get("status") == "valid" and row.get("A") and row.get("P")]
    faults = [row for row in valid if row["category"] in FAULTS]
    missing = [row for row in valid if row["category"] == "missing_evidence"]
    healthy = [row for row in valid if row["category"] == "healthy"]
    lock_pool = [row for row in faults if row["category"] == "lock_pool_exhaustion"]

    def a_cause(row):
        return (row.get("A") or {}).get("primary_cause")

    findings = [f for row in valid for f in report_of(row).get("findings", [])]
    checks = [((row.get("P") or {}).get("checks") or {}) for row in valid]
    summary = {
        "label": "FIXTURE RESULTS: Stage 5 " + split, "split": split, "primary_cause_source_for_P": pick_source,
        "case_runs": len(rows), "valid": len(valid),
        "no_valid_attempt": sum(1 for row in rows if row.get("status") == "no_valid_attempt"),
        "invalid_attempts": sum(1 for row in rows for a in row["attempts"] if a["status"] == "invalid"),
        "interrupted_attempts": sum(1 for row in rows for a in row["attempts"] if a["status"] == "interrupted"),
        "P": {
            "primary_cause": rate(faults, lambda r: cause_correct(r["category"], p_cause(r, picks)[0])),
            "primary_cause_lenient_secondary": rate(faults, lambda r: cause_correct(r["category"], p_cause(r, picks)[0], False)),
            "causal_depth_lock_pool": rate(lock_pool, lambda r: p_cause(r, picks)[1]) if picks
            else "assessed only from blind picks (held-out, pick 5)",
            "appropriate_abstention": rate(missing, lambda r: outcome(r, "P") == "INCONCLUSIVE"),
            "false_alarms": rate(healthy, lambda r: outcome(r, "P") in ("SUPPORTED", "TENTATIVE")),
            "healthy_inconclusive": rate(healthy, lambda r: outcome(r, "P") == "INCONCLUSIVE"),
            "fallback_reports": rate(valid, lambda r: (r.get("P") or {}).get("report_source") != "model"),
            "findings": len(findings),
            "findings_with_citations": sum(1 for f in findings if f.get("evidence_ids")),
            "numbers_checked": sum(c.get("numbers_checked", 0) for c in checks),
            "numbers_not_found_in_cited_evidence": sum(c.get("numbers_not_found_in_cited_evidence", 0) for c in checks),
            "model_calls": sum((r["P"].get("model_calls") or 0) for r in valid),
            "tool_calls": sum((r["P"].get("tool_calls") or 0) for r in valid),
            "cost_usd": round(sum((r["P"].get("cost_usd") or 0) for r in valid), 4),
            "replay_elapsed_seconds_median": median([r["P"].get("replay_elapsed_seconds") for r in valid]),
        },
        "A": {
            "primary_cause": rate(faults, lambda r: cause_correct(r["category"], a_cause(r))),
            "primary_cause_lenient_secondary": rate(faults, lambda r: cause_correct(r["category"], a_cause(r), False)),
            "appropriate_abstention": rate(missing, lambda r: outcome(r, "A") == "INCONCLUSIVE"),
            "false_alarms": rate(healthy, lambda r: outcome(r, "A") in ("SUPPORTED", "TENTATIVE")),
            "tool_calls": sum((r["A"].get("tool_calls") or 0) for r in valid),
        },
        "per_class": per_class(valid, picks),
        "invalid_reasons": [{"case_run": row["case_run"], "attempt": a["attempt"], "validity": a.get("validity")}
                            for row in rows for a in row["attempts"] if a["status"] == "invalid"],
    }
    if split == "held_out":
        if not picks:  # without a complete set of picks, P's primary cause is not scored at all
            reason = "NOT SCORED: picks.csv (or author-picks.csv) lacks a valid pick for every report"
            for metric in ("primary_cause", "primary_cause_lenient_secondary", "causal_depth_lock_pool"):
                summary["P"][metric] = reason
        summary["unsupported_findings"] = unsupported_rate()
        summary["gates"] = gates(summary)
    return summary


def per_class(valid: list[dict], picks) -> dict:
    table = {}
    for category in cases.TRUTH:
        rows = [row for row in valid if row["category"] == category]
        table[category] = {
            "runs": len(rows),
            "A": [f"{(r['A'] or {}).get('outcome')}:{(r['A'] or {}).get('primary_cause')}" for r in rows],
            "P": [f"{outcome(r, 'P')}:{p_cause(r, picks)[0]}" for r in rows],
        }
    return table


def median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def unsupported_rate() -> dict:
    judgements = read_csv(REVIEW / "findings.csv", "judgement")
    counts = defaultdict(int)
    for value in judgements.values():
        counts[value.lower() or "unmarked"] += 1
    judged = counts["supported"] + counts["unsupported"]
    return dict(counts) | {"rate": round(counts["unsupported"] / judged, 3) if judged else None}


def gates(summary: dict) -> dict:
    p, a = summary["P"], summary["A"]

    def at_least(metric, threshold):
        return None if not isinstance(metric, dict) or metric["rate"] is None else metric["rate"] >= threshold

    unsupported = summary["unsupported_findings"]["rate"]
    return {
        "primary_cause >= 0.80": at_least(p["primary_cause"], 0.80),
        "appropriate_abstention >= 0.80": at_least(p["appropriate_abstention"], 0.80),
        "false_alarms <= 0.10": None if p["false_alarms"]["rate"] is None else p["false_alarms"]["rate"] <= 0.10,
        "every finding cites evidence": p["findings_with_citations"] == p["findings"],
        "unsupported findings <= 0.05 (blinded sample)": None if unsupported is None else unsupported <= 0.05,
        "P at least 10 points above A on primary cause":
            None if not isinstance(p["primary_cause"], dict) or None in (p["primary_cause"]["rate"], a["primary_cause"]["rate"])
            else p["primary_cause"]["rate"] - a["primary_cause"]["rate"] >= 0.10,
    }


# ---- the blinded review sheet (protocol §5.1, §5.5) --------------------------------------------

def rendered_evidence(store, job_id: str) -> dict[str, str]:
    return {f"E{ordinal}": render_observation(f"E{ordinal}", tool, arguments, envelope)
            for ordinal, tool, arguments, envelope in store.execute(
                "SELECT e.ordinal, a.tool, a.arguments, e.envelope FROM evidence e JOIN actions a ON a.id = e.action_id "
                "WHERE e.investigation_id = %s ORDER BY e.ordinal", (job_id,)).fetchall()}


def write_sheet() -> None:
    rows = [row for row in load("held_out") if row.get("status") == "valid" and report_of(row)]
    rng = random.Random(SHEET_SEED)
    rng.shuffle(rows)
    REVIEW.mkdir(parents=True, exist_ok=True)
    key = {"seed": SHEET_SEED, "reports": {}, "findings": {}}
    all_findings = []
    parts = [PAGE_HEAD, "<h1>IncidentLens review sheet</h1>", INSTRUCTIONS, "<h2>Part 1: which root cause does each report name?</h2>"]
    with connect(load_settings().app_conninfo, autocommit=True) as store:
        for index, row in enumerate(rows, 1):
            sheet_id = f"R{index:03d}"
            key["reports"][sheet_id] = row["case_run"]
            report = report_of(row)
            parts.append(f"<section><h3>{sheet_id}</h3><p><b>Outcome:</b> {html.escape(report.get('outcome', ''))}</p>"
                         f"<p><b>Summary:</b> {html.escape(report.get('summary', ''))}</p><ol>"
                         + "".join(f"<li>{html.escape(f.get('claim', ''))}</li>" for f in report.get("findings", []))
                         + "</ol></section>")
            texts = rendered_evidence(store, row["P"]["job_id"])
            for number, finding in enumerate(report.get("findings", []), 1):
                all_findings.append((row["case_run"], number, finding,
                                     {h: texts.get(h, "(missing)") for h in finding.get("evidence_handles", [])}))
    sample = random.Random(SHEET_SEED + 1).sample(all_findings, min(FINDINGS_SAMPLE, len(all_findings)))
    parts.append("<h2>Part 2: is each finding supported by the observations it cites?</h2>" + RUBRIC)
    for index, (case_run, number, finding, cited) in enumerate(sample, 1):
        sheet_id = f"F{index:03d}"
        key["findings"][sheet_id] = {"case_run": case_run, "finding": number}
        parts.append(f"<section><h3>{sheet_id}</h3><p><b>Claim ({html.escape(finding.get('support', ''))}):</b> "
                     f"{html.escape(finding.get('claim', ''))}</p><p><b>Stated reason:</b> {html.escape(finding.get('reason', ''))}</p>"
                     + "".join(f"<details><summary>{html.escape(h)}</summary><pre>{html.escape(t)}</pre></details>"
                               for h, t in cited.items()) + "</section>")
    parts.append("</body></html>")
    (REVIEW / "sheet.html").write_text("\n".join(parts))
    with (REVIEW / "picks.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "pick"])
        writer.writerows([[sheet_id, ""] for sheet_id in key["reports"]])
    with (REVIEW / "findings.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "judgement"])
        writer.writerows([[sheet_id, ""] for sheet_id in key["findings"]])
    key["findings_population"] = len(all_findings)
    (REVIEW / "key.json").write_text(json.dumps(key, indent=1) + "\n")
    print(f"wrote {len(key['reports'])} reports and {len(key['findings'])} of {len(all_findings)} findings to {REVIEW}")


PAGE_HEAD = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
             "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'\">"
             "<meta name=viewport content='width=device-width, initial-scale=1'><title>Review sheet</title>"
             "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:1rem auto;padding:0 1rem}"
             "section{border-top:1px solid #ccc;padding:.4rem 0}pre{white-space:pre-wrap;background:#f4f4f4;"
             "padding:.5rem;font-size:13px}</style></head><body>")
INSTRUCTIONS = ("<p>Each report below came from one investigation. The case, the injected fault and the system are "
                "hidden. Mark your answers in <code>picks.csv</code> and <code>findings.csv</code>, next to this page. "
                "Please do not open <code>key.json</code> until you have finished.</p>"
                "<p><b>Part 1:</b> pick the cause the report names as the <b>root</b> cause, not every cause it "
                "mentions. A condition the report calls only a possible or likely contributor is not its root cause. "
                "Write the number in <code>picks.csv</code>:</p><ol>"
                + "".join(f"<li>{html.escape(text)}</li>" for text in PICK_TEXT) + "</ol>")
RUBRIC = ("<p>Write <code>supported</code>, <code>unsupported</code> or <code>unclear</code> in <code>findings.csv</code>. "
          "A finding is <b>supported</b> when the cited observations show what it claims, at the claimed strength: an "
          "<i>observed</i> finding needs the fact itself in the cited text; an <i>inferred</i> finding needs its premises "
          "there, with the inference stated as such. It is <b>unsupported</b> when the cited observations do not show "
          "it, contradict it, or show only a weaker claim.</p>")


def main() -> int:
    if sys.argv[1:2] == ["sheet"]:
        write_sheet()
        return 0
    split = sys.argv[2] if len(sys.argv) > 2 else "development"
    version = sys.argv[4] if sys.argv[3:4] == ["--prompt-version"] else None
    if version and split != "development":
        raise SystemExit("--prompt-version scores development replays only")
    summary = score(split, version) | ({"P_prompt_version": version} if version else {})
    name = f"summary-{split}-{version}.json" if version else f"summary-{split}.json"
    (RESULTS / name).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_class"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
