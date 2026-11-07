"""The diagnostic tool registry: binds tool names to adapters and fixed sources. Window, service
and target come from the job (here, the context), never from model arguments (spec §9)."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from incidentlens.actions import DIAGNOSTIC_TOOLS
from incidentlens.config import SERVICE_ID, Settings
from incidentlens.tools.envelope import Window, build_envelope
from incidentlens.tools.sources import PrometheusClient


@dataclass
class ToolContext:
    window: Window
    settings: Settings
    store: psycopg.Connection  # IncidentLens's database (collected telemetry), autocommit
    prometheus: PrometheusClient
    service: str = SERVICE_ID
    frozen: bool = False  # answering from a frozen evidence bundle (D6): no live-state reads


def adapters() -> dict:
    # Imported here so each tool module can import ToolContext without a cycle.
    from incidentlens.tools import blockers, config_snapshot, logs, metrics, outbox
    return {
        "get_request_metrics": metrics.get_request_metrics,
        "get_outbox_stats": outbox.get_outbox_stats,
        "search_delivery_logs": logs.search_delivery_logs,
        "get_delivery_timings": logs.get_delivery_timings,
        "get_pool_metrics": metrics.get_pool_metrics,
        "get_database_blockers": blockers.get_database_blockers,
        "get_runtime_config": config_snapshot.get_runtime_config,
    }


def run_tool(name: str, arguments: dict, context: ToolContext) -> dict:
    """Validates arguments against the tool's schema and runs its adapter. A tool without an
    adapter returns UNAVAILABLE, never fabricated zeros."""
    _, argument_model = DIAGNOSTIC_TOOLS[name]
    validated = argument_model.model_validate(arguments)
    adapter = adapters().get(name)
    if adapter is None:
        return build_envelope(name, validated.model_dump(), context.window, "UNAVAILABLE", "none", {},
                              ["This diagnostic source is not implemented yet."], ["adapter unavailable"])
    return adapter(context, validated)


def live_context(window: Window, store: psycopg.Connection, settings: Settings) -> ToolContext:
    """A context over the live sources (as opposed to a frozen evidence bundle, decisions D6)."""
    return ToolContext(window=window, settings=settings, store=store,
                       prometheus=PrometheusClient(settings.prometheus_url))
