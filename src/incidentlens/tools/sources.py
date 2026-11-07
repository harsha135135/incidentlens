"""Source adapters shared by the tools: Prometheus raw samples, read-only queries on LedgerCore's
database, and LedgerCore's configuration history from the collected startup records.

Tools pass only fixed query templates. The model never supplies PromQL, SQL, URLs or headers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from incidentlens.config import (
    SCRAPE_INTERVAL_SECONDS,
    SOURCE_DB_STATEMENT_TIMEOUT_MS,
    SOURCE_HTTP_CONNECT_TIMEOUT_S,
    SOURCE_HTTP_READ_TIMEOUT_S,
)
from incidentlens.tools.envelope import Deadline, Window, iso


class SourceError(Exception):
    """A source could not answer. The message is a short category, safe to show."""


@dataclass
class Series:
    labels: dict
    times: list[datetime]
    values: list[float]
    unreadable: int = 0  # samples the target exported as NaN or infinite, dropped


class PrometheusClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def raw_samples(self, selector: str, window: Window, deadline: Deadline,
                    include_previous_scrape: bool = False) -> list[Series]:
        """Every stored sample of `selector` inside the window, as Prometheus scraped it.
        With include_previous_scrape, the range starts two scrape intervals early so a counter
        has a baseline sample from just before the window."""
        start = window.start - timedelta(seconds=2 * SCRAPE_INTERVAL_SECONDS) if include_previous_scrape \
            else window.start
        range_ms = int((window.end - start).total_seconds() * 1000)
        # A range selector at time T covers (T - range, T]; ending a millisecond early keeps [start, end).
        at = (window.end - timedelta(milliseconds=1)).timestamp()
        payload = self.get("/api/v1/query", {"query": f"{selector}[{range_ms}ms]", "time": f"{at:.3f}"}, deadline)
        series = []
        for item in payload["data"]["result"]:
            # A gauge whose value could not be read (for example LedgerCore's outbox gauges when its
            # pool is exhausted) is exported as NaN. That is a missing sample, never a number.
            kept = [(t, float(v)) for t, v in item["values"] if math.isfinite(float(v))]
            times = [datetime.fromtimestamp(float(t), tz=timezone.utc) for t, _ in kept]
            values = [v for _, v in kept]
            series.append(Series(item["metric"], times, values, len(item["values"]) - len(kept)))
        return series

    def get(self, path: str, params: dict, deadline: Deadline) -> dict:
        if deadline.expired():
            raise SourceError("tool deadline reached before querying Prometheus")
        timeout = httpx.Timeout(min(SOURCE_HTTP_READ_TIMEOUT_S, deadline.remaining()),
                                connect=min(SOURCE_HTTP_CONNECT_TIMEOUT_S, deadline.remaining()))
        try:
            response = httpx.get(self.base_url + path, params=params, timeout=timeout)
        except httpx.TimeoutException as error:
            raise SourceError("Prometheus timed out") from error
        except httpx.HTTPError as error:
            raise SourceError("Prometheus unreachable") from error
        if response.status_code != 200:
            raise SourceError(f"Prometheus returned HTTP {response.status_code}")
        body = response.json()
        if body.get("status") != "success":
            raise SourceError("Prometheus query failed")
        return body


def target_query(conninfo: str, sql: str, params: tuple, deadline: Deadline) -> list[tuple]:
    """One read-only transaction on LedgerCore's database as ledger_diag_reader. The adapter sets
    READ ONLY and the statement timeout itself, in addition to the login's own settings (D4)."""
    if deadline.expired():
        raise SourceError("tool deadline reached before querying the target database")
    timeout_ms = int(min(SOURCE_DB_STATEMENT_TIMEOUT_MS, deadline.remaining() * 1000))
    try:
        with psycopg.connect(conninfo, autocommit=False) as conn:
            conn.read_only = True
            with conn.transaction():
                conn.execute(f"SET LOCAL statement_timeout = {max(timeout_ms, 1)}")
                return conn.execute(sql, params).fetchall()
    except psycopg.errors.QueryCanceled as error:
        raise SourceError("target database query timed out") from error
    except psycopg.OperationalError as error:
        raise SourceError("target database unreachable") from error
    except psycopg.Error as error:
        raise SourceError(f"target database error ({type(error).__name__})") from error


def target_config(store: psycopg.Connection, service: str, window: Window) -> dict | None:
    """The LedgerCore startup record in effect at the window end, plus any restarts inside the window.
    Configuration from after the window is never presented as applying to it."""
    rows = store.execute(
        "SELECT observed_at, fields FROM log_records WHERE service = %s AND record_type = 'ledgercore.config' "
        "AND observed_at < %s ORDER BY observed_at DESC LIMIT 20", (service, window.end)).fetchall()
    if not rows:
        return None
    latest_at, latest = rows[0]
    restarts = [iso(at) for at, _ in rows if at >= window.start]
    return {"revision": latest.get("revision"), "instance_id": latest.get("instanceId"),
            "effective_at": iso(latest_at), "fields": latest, "restarts_in_window": sorted(restarts)}
