# LedgerCore telemetry audit (Stage 0)

**Instance audited:** unmodified LedgerCore at commit `86e4bf1`.

**Command:** `scripts/stage0_ledgercore_audit.sh`. It starts LedgerCore with its own `.env.example` demo settings, drives `scripts/demo.sh`, then records what the running instance exports.

**Raw output:** `evaluation/results/stage0/ledgercore-audit-20261104T153653Z/`.

**Baseline test suite before any change:**
- Command: `./mvnw -q verify`.
- Result: **91 tests, 0 failures, 0 errors, 0 skipped**.
- Log: `evaluation/results/stage0/ledgercore-baseline-verify.log`.

## What the running instance exports

- **103 metric names under `/actuator/metrics`.** The ones relevant to IncidentLens:
  - `http.server.requests`, with tags `uri` (route template), `method`, `status`, `outcome`, `exception` and `error`. No account or request IDs appear as tags.
  - `http.server.requests.active`.
  - `hikaricp.connections`, `.active`, `.idle`, `.max`, `.min`, `.pending`, `.timeout`, `.acquire`, `.creation` and `.usage`, each tagged `pool`.
  - `ledgercore.outbox.pending.events`, `ledgercore.outbox.failed.events` and `ledgercore.outbox.oldest.undelivered.age`.
  - `ledgercore.business.rejections`, `ledgercore.idempotency.replays`, `ledgercore.reconciliation.runs` and `ledgercore.reconciliation.failures`.
- **`/actuator/prometheus` returns 404.** No Prometheus registry is on the classpath.
- **`http.server.requests` has no histogram buckets.** It reports only a count, total time and maximum, so percentiles cannot be computed from it.
- **Outbox deliveries leave no log trace.** The demo delivered 6 events, and the log contains no line for any of them. Successful deliveries are not logged, and failures are logged only as plain-text warnings.
- **Log lines are plain text with a local time offset** (for example `2026-11-04T21:07:08.129+05:30`). They are not UTC JSON.
- **A customer login can read `/actuator/metrics`** (HTTP 200 for `customer-a`). This is recorded as an observed limitation and left unchanged (see `decisions.md`).

## Missing signals against spec §5

| Spec signal | Status in the unmodified instance | Stage 1 change |
|---|---|---|
| HTTP rate, errors, duration (histogram, seconds) | Partial: timer present, no buckets, no Prometheus export | Prometheus registry, histogram buckets, a scrape credential limited to `/actuator/prometheus` |
| Outbox pending, failed, oldest age | Present as gauges; Prometheus export not available | Confirm the exported names through Prometheus |
| Committed arrivals by time | Not exported. Durable `outbox_events.created_at` exists but is a transaction-start timestamp (D3) | Read-only view and index, plus a settle-time rule |
| Recorded publications by time | Not exported. Durable `published_at`, set once on `PENDING → PUBLISHED` | Read-only view and index |
| Delivery attempt log | **Missing**: success not logged, no duration or status, not JSON | Structured delivery-attempt records without payload |
| Delivery timing (auth vs handler) | **Missing** | Fixture consumer timings (D1); LedgerCore's in-process consumer has no timer |
| Polling behaviour | **Missing** | Structured poll-batch records and timer |
| Pool | Present (`hikaricp.*`); Prometheus export not available | Confirm the exported names through Prometheus |
| Blocking history | **Missing** | `diag_blocking_sessions()` plus IncidentLens collector samples every 2 s (D2, D4, D5) |
| Config history | **Missing** | Startup config log event (D11) |
| UTC structured logs | **Missing** (local offset, plain text) | JSON file logs in UTC |

## After Stage 1 (instrumented LedgerCore)

**Observed on 2026-11-06** from the running instance, scraped by Prometheus v3.15.1 through the diagnostics login.

### Prometheus series

| Signal | Prometheus series | Labels |
|---|---|---|
| HTTP rate, errors, duration | `http_server_requests_seconds_bucket`, `_count`, `_sum`, `_max` | Buckets `le` = 0.005 to 10 s, `uri` (route template), `method`, `status`, `outcome`, `exception`, `error` |
| Outbox gauges | `ledgercore_outbox_pending_events`, `ledgercore_outbox_failed_events`, `ledgercore_outbox_oldest_undelivered_age_seconds` | none |
| Delivery attempts | `ledgercore_outbox_delivery_seconds_bucket`, `_count`, `_sum`, `_max` | `result` = success, auth_rejected, http_4xx, http_5xx, http_other, timeout, connection_failed, other |
| Polling | `ledgercore_outbox_poll_seconds_bucket`, `_count`, `_sum`, `_max` | none |
| Pool | `hikaricp_connections_active`, `_idle`, `_max`, `_min`, `_pending`, `hikaricp_connections_timeout_total`, `hikaricp_connections_acquire_seconds_*` | `pool` |

- **Delivery timer:** appears after the first delivery.
- **No identifiers in labels:** `PrometheusExportTest.metricLabelsCarryNoIdentifiers` scans the whole export for UUIDs.

### Other sources

| Signal | Source after Stage 1 |
|---|---|
| Committed arrivals by time | `diagnostics.outbox_event_times.created_at`, indexed. Labelled as transaction-start time; a bucket is final 10 s after it ends (D3) |
| Recorded publications by time | `diagnostics.outbox_event_times.published_at`, indexed (partial index on published rows) |
| Delivery attempt log | `outbox.delivery_attempt` records in the JSON log (D18) |
| Delivery timing (auth vs handler) | Fixture consumer timing log (**fixture result**, D1/D23). LedgerCore's in-process consumer still has no timer, so for it only total attempt time is measured |
| Polling behaviour | `outbox.poll_batch` records, plus the poll timer |
| Blocking history | Collector samples of `diagnostics.blocking_sessions()` every 2 s, with explicit UNAVAILABLE samples on failure |
| Config history | `ledgercore.config` startup records (D11) |
| UTC structured logs | ECS JSON file with UTC `@timestamp` when `LEDGERCORE_LOG_FILE` is set |

**Still missing, by design:** `/actuator/**` access for customer logins is unchanged (observed limitation).
