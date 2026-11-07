"""The shared evidence envelope (spec §9) and the small, deterministic arithmetic every tool uses.
Tools compute rates, percentiles and gaps here, in code; the model only reads the results."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from incidentlens.config import SERVICE_ID, TOOL_RESULT_MAX_BYTES
from incidentlens.redaction import redact

ADAPTER_VERSION = "1.0"
TOOL_DEADLINE_SECONDS = 8.0
COVERAGE_ORDER = ["COMPLETE", "PARTIAL", "UNAVAILABLE"]


@dataclass(frozen=True)
class Window:
    """A half-open time window [start, end) in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window times must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("window end must be after its start")

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def as_json(self) -> dict:
        return {"start": iso(self.start), "end": iso(self.end)}


class Deadline:
    """The 8-second total budget for one tool call (spec §9)."""

    def __init__(self, seconds: float = TOOL_DEADLINE_SECONDS):
        self.expires = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.expires - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(text) -> datetime | None:
    """An RFC 3339 timestamp with an offset, or None. Naive times are rejected, not guessed."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def worst_coverage(*coverages: str) -> str:
    return max(coverages, key=COVERAGE_ORDER.index)


def build_envelope(tool: str, arguments: dict, window: Window, coverage: str, source_kind: str, data: dict,
                   limitations: list[str], errors: list[str], observed_window: dict | None = None,
                   target: dict | None = None) -> dict:
    """Wraps tool data in the envelope, redacts it, and enforces the 64 KiB result limit."""
    envelope = {
        "evidence_id": str(uuid.uuid4()),
        "tool": tool,
        "arguments": arguments,
        "requested_window": window.as_json(),
        "observed_window": observed_window or {"first": None, "last": None},
        "collected_at": iso(utc_now()),
        "coverage": coverage,
        "truncated": False,
        "source": {"kind": source_kind, "instance": SERVICE_ID, "adapter_version": ADAPTER_VERSION,
                   "target_revision": (target or {}).get("revision"),
                   "target_instance_id": (target or {}).get("instance_id")},
        "data": finite(redact(data)),
        "limitations": redact(limitations),
        "errors": redact(errors),
    }
    return fit_to_limit(envelope)


def finite(value):
    """JSON has no NaN or infinity, and PostgreSQL rejects them: a non-finite number becomes null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [finite(item) for item in value]
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    return value


def fit_to_limit(envelope: dict, max_bytes: int = TOOL_RESULT_MAX_BYTES) -> dict:
    """Halves the longest list in `data` (keeping its first items, so source order is kept) until
    the envelope fits, and says what was cut."""
    while len(json.dumps(envelope, allow_nan=False).encode()) > max_bytes:
        path, items = longest_list(envelope["data"])
        if items is None or len(items) <= 1:
            envelope["data"] = {"omitted": "result exceeded the size limit and could not be reduced"}
            envelope["truncated"] = True
            envelope["limitations"].append("The result was too large to keep; data omitted.")
            break
        keep = len(items) // 2
        envelope["limitations"].append(f"{path} truncated from {len(items)} to {keep} items (size limit).")
        del items[keep:]
        envelope["truncated"] = True
    return envelope


def longest_list(value, path: str = "data"):
    best_path, best = None, None
    if isinstance(value, list):
        best_path, best = path, value
        children = enumerate(value)
    elif isinstance(value, dict):
        children = value.items()
    else:
        return None, None
    for key, child in children:
        child_path, child_list = longest_list(child, f"{path}.{key}")
        if child_list is not None and (best is None or len(child_list) > len(best)):
            best_path, best = child_path, child_list
    return best_path, best


def stats(values: list[float]) -> dict:
    """Count, min, nearest-rank p50/p95, max and mean of measured values, in their own unit."""
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    ordered = sorted(values)

    def nearest_rank(q: float) -> float:
        return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

    return {"count": len(ordered), "min": round(ordered[0], 3), "p50": round(nearest_rank(0.5), 3),
            "p95": round(nearest_rank(0.95), 3), "max": round(ordered[-1], 3),
            "mean": round(sum(ordered) / len(ordered), 3)}


def scrape_gaps(sample_times: list[datetime], values: list[float], window: Window,
                interval_seconds: float) -> dict:
    """Finds the parts of the window without a successful scrape. A scrape counts when Prometheus
    recorded up == 1. A gap is any stretch longer than 1.5 scrape intervals between successful
    scrapes, or at either end of the window.

    Prometheus timestamps a sample with its scrape's start time. A scrape that starts during an
    outage can still succeed once the target recovers (within the scrape timeout), so a gap can
    appear to end up to one scrape timeout before the recovery, and to start up to one interval
    before the outage. Bounds are therefore accurate to about one scrape interval either way."""
    tolerance = timedelta(seconds=interval_seconds * 1.5)
    good = [t for t, v in zip(sample_times, values) if v == 1]
    failed = sum(1 for v in values if v != 1)
    gaps = []
    previous = window.start
    for moment in good + [window.end]:
        if moment - previous > tolerance:
            gaps.append({"start": iso(previous), "end": iso(moment),
                         "seconds": round((moment - previous).total_seconds(), 1)})
        previous = moment
    return {
        "expected_scrapes": round(window.seconds / interval_seconds),
        "successful_scrapes": len(good),
        "failed_scrapes": failed,
        "gaps": gaps,
        "gap_bounds": f"scrape start times; each bound accurate to about one scrape interval ({interval_seconds:g} s)",
    }


def bucket_quantile(q: float, buckets: list[tuple[float, float]]) -> float | None:
    """Quantile from cumulative histogram buckets [(upper_bound, count), ...] including +Inf,
    by linear interpolation inside the bucket that holds the rank (the method Prometheus's
    histogram_quantile uses). Buckets must already be summed across series: per-instance
    percentiles are never averaged (spec §5). None when there are no observations."""
    ordered = sorted(buckets)
    if not ordered or ordered[-1][1] <= 0:
        return None
    rank = q * ordered[-1][1]
    lower_bound, lower_count = 0.0, 0.0
    for upper_bound, count in ordered:
        if count >= rank:
            if math.isinf(upper_bound):
                return lower_bound  # above the highest finite bucket: report that bound
            if count == lower_count:
                return upper_bound
            return lower_bound + (upper_bound - lower_bound) * (rank - lower_count) / (count - lower_count)
        lower_bound, lower_count = upper_bound, count
    return None


def counter_increase(values: list[float]) -> tuple[float, int]:
    """Increase of a counter across consecutive samples, treating a drop as a restart from zero.
    Returns (increase, resets)."""
    increase, resets = 0.0, 0
    for previous, current in zip(values, values[1:]):
        if current >= previous:
            increase += current - previous
        else:
            increase += current
            resets += 1
    return increase, resets
