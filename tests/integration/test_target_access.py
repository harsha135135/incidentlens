"""Spec §17: the read-only credential cannot mutate target tables, and the timeout stops an
expensive permitted query, through IncidentLens's own adapter (tools/sources.py) against the live
LedgerCore database (scripts/stack.py up)."""

from __future__ import annotations

import time

import pytest

from incidentlens.tools.envelope import Deadline
from incidentlens.tools.sources import SourceError, target_query


def test_the_adapter_cannot_write_to_the_target(settings):
    for statement in ("UPDATE diagnostics.outbox_event_times SET state = 'FAILED'",
                      "DELETE FROM public.outbox_events",
                      "CREATE TABLE public.injected (x integer)"):
        with pytest.raises(SourceError) as error:
            target_query(settings.target_conninfo, statement, (), Deadline())
        assert "ReadOnlySqlTransaction" in str(error.value) or "InsufficientPrivilege" in str(error.value)


def test_the_adapter_timeout_stops_an_expensive_permitted_query(settings):
    started = time.monotonic()
    with pytest.raises(SourceError, match="timed out"):
        target_query(settings.target_conninfo, "SELECT pg_sleep(5)", (), Deadline())
    assert time.monotonic() - started < 3.5


def test_the_adapter_respects_the_tool_deadline(settings):
    deadline = Deadline(seconds=0.5)
    with pytest.raises(SourceError, match="timed out"):
        target_query(settings.target_conninfo, "SELECT pg_sleep(2)", (), deadline)
