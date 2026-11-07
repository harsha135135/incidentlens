"""The fixed collection bundle (decisions D48): every implemented diagnostic tool, in a fixed
order. The product collects exactly these, then makes one model decision. Evaluation baselines use
the same list, so every system sees the same sources."""

from __future__ import annotations

FIXED_BUNDLE: list[tuple[str, dict]] = [
    ("get_request_metrics", {"route_group": "transfers"}),
    ("get_outbox_stats", {}),
    ("search_delivery_logs", {"outcome": "any", "limit": 100}),
    ("get_delivery_timings", {}),
    ("get_pool_metrics", {}),
    ("get_database_blockers", {"mode": "historical", "limit": 50}),
    ("get_runtime_config", {}),
]

# The adaptive agent (non-default mode, kept to reproduce the pilot) starts from the first two.
AGENT_INITIAL_BUNDLE = FIXED_BUNDLE[:2]
