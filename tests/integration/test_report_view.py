"""Spec §15: "budget exhausted, source missing and no fault observed must look different", and the
report screen never says the system is fixed. Checked on the view the Report screen and the HTML
export are built from, over records in real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from incidentlens.pages import TEMPLATES, report_view


def job_with_evidence(owner, coverages: list[str]) -> uuid.UUID:
    job = uuid.uuid4()
    owner.execute("INSERT INTO investigations (id, owner, service_id, symptom, window_start, window_end, profile, status, "
                  "queue_expires_at) VALUES (%s, 'local', 'ledgercore-local', 'x', now() - interval '10 min', "
                  "now() - interval '5 min', 'standard', 'QUEUED', now())", (job,))
    for ordinal, coverage in enumerate(coverages, 1):
        action = uuid.uuid4()
        owner.execute("INSERT INTO actions (id, investigation_id, generation, kind, tool, arguments, input_hash, status) "
                      "VALUES (%s, %s, 1, 'tool', 'get_outbox_stats', '{}', 'h', 'SUCCEEDED')", (action, job))
        envelope = {"coverage": coverage, "truncated": False, "data": {}, "limitations": [], "errors": [],
                    "collected_at": datetime.now(timezone.utc).isoformat(), "source": {"kind": "test"}}
        owner.execute("INSERT INTO evidence (id, action_id, investigation_id, ordinal, envelope, sha256, byte_count) "
                      "VALUES (%s, %s, %s, %s, %s, 'h', 2)", (uuid.uuid4(), action, job, ordinal, Jsonb(envelope)))
    return job


def report(outcome: str) -> dict:
    return {"outcome": outcome, "summary": "s", "findings": [], "alternatives": [], "limitations": [], "next_checks": [],
            "usage": {"model_actions": 1, "tool_actions": 1, "reported_input_tokens": 1, "reported_output_tokens": 1}}


def test_stopped_missing_source_and_no_fault_are_visibly_different(owner):
    stopped = report_view(owner, job_with_evidence(owner, ["COMPLETE"]), report("INCONCLUSIVE"), "fallback")
    missing = report_view(owner, job_with_evidence(owner, ["UNAVAILABLE", "COMPLETE"]), report("INCONCLUSIVE"), "model")
    no_fault = report_view(owner, job_with_evidence(owner, ["COMPLETE"]), report("NO_FAULT_OBSERVED"), "model")

    assert stopped["banner_text"] == "Stopped without a diagnosis" and stopped["banner_class"] == "stopped"
    assert [item["handle"] for item in missing["missing"]] == ["E1"]
    assert no_fault["banner_text"] == "No fault observed in the window" and no_fault["banner_class"] == "no-fault"
    assert len({stopped["banner_class"], missing["banner_class"], no_fault["banner_class"]}) >= 2

    rendered = {name: TEMPLATES.get_template("report_body.html").render(**view)
                for name, view in (("stopped", stopped), ("missing", missing), ("no_fault", no_fault))}
    assert "banner-stopped" in rendered["stopped"]
    assert "source unavailable" in rendered["missing"] and "unknown, not zero" in rendered["missing"]
    assert "banner-no-fault" in rendered["no_fault"]
    for html in rendered.values():
        assert "fixed" not in html.lower()  # "diagnosis in the observed window", never "system fixed"
