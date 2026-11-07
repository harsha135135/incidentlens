# IncidentLens TODO

Stages and exit gates follow `docs/TECHNICAL-DESIGN.md` §19, and the acceptance tests follow §16–§17. Resolutions of conflicts with the real repository are in `docs/decisions.md` (D1–D17, and later entries as they are found).

**When an item may be ticked.** An item is ticked only with real evidence: the command that was run, its outcome and the raw output file. Mocks or a successful UI demonstration alone never tick an item.

**Two gates, reported separately:**
- **Gate A:** dependable diagnostic software.
- **Gate B:** measured diagnostic usefulness.

Passing A does not imply B.

## Stage 0: feasibility

**Exit gate:**
- allowed and denied access demonstrated
- token accounting verified
- tool schema obeyed
- missing signals listed

- [x] Skeleton: `pyproject.toml`, `uv.lock` (Python 3.12), `versions.json`, `.gitignore`, `docs/decisions.md`
- [x] OpenShell installed at a pinned release; CLI and gateway versions recorded and compatible (0.1.7 / 0.1.7)
- [x] Sandbox to internal gateway (8089): allowed request with a valid capability succeeds; invalid capability rejected
- [x] Denied: internet egress, host port 8088, database ports, host secret paths, plus a binary the policy does not list
- [x] Internal listener bind recorded and acceptable under D12: `127.0.0.1:8089` (loopback, the preferred option)
- [x] `python3` available in the sandbox image: custom image with a digest-pinned base (`deploy/openshell/runner.Dockerfile`)
- [x] Model: `claude-sonnet-5-5` listed as available to the account (D9); two real tool-calling calls made through LangChain with SDK retries off
- [x] Discriminated action schema obeyed by the real model (2/2 `call_tool`); malformed output rejected by validation. **Correction:** the first run was not actually strict (LangChain dropped the flag, D17); rerun with `strict` on each definition: 2/2 valid
- [x] Token accounting: count-endpoint estimate equals reported input (difference 0 in both calls) (D10); latency 2.57 s and 1.51 s
- [x] `.env.local` ignored by Git: `git check-ignore -v .env.local` reports `.gitignore:2`; file mode 600

- [x] A full `submit_report` fits the 1,500-token output limit: 5/5 valid, 958–1,183 tokens, after the D27 resolution (raw counts or no size guidance: 3/3 and 3/5 cut off)

**Stage 0 gate: PASSED on 2026-11-06.** Allowed and denied access demonstrated (11/11), token accounting verified, schema obeyed, missing signals listed.
- [x] Dated pricing file from the provider's pricing page (`evaluation/pricing/anthropic-2026-11-04.json`)
- [x] LedgerCore baseline suite run before any change: 91 tests, 0 failures, 0 errors, 0 skipped
- [x] LedgerCore exported metric names observed on a running instance; missing signals listed in `docs/telemetry-audit.md`

## Stage 1: observation

**Exit gate:**
- a known fixture yields correct observations with accurate gaps and timestamps
- existing LedgerCore tests still pass

**Stage 1 gate: PASSED on 2026-11-06.** Known fixture scenarios gave correct observations with accurate gaps and timestamps (5/5 live scenarios), and every existing LedgerCore test still passes (111 tests, 0 failures).

**LedgerCore (telemetry and diagnostic access only):**
- [x] Prometheus registry and `/actuator/prometheus`, reachable only with the new `DIAGNOSTICS_READER` credential; `/v1/**` returns 403 (`PrometheusExportTest`, 4 tests)
- [x] HTTP histogram buckets; fixed route templates; no IDs as labels (whole export scanned for UUIDs)
- [x] Structured JSON log file: ECS, UTC `@timestamp`, verified on the running jar at +05:30 (D18)
- [x] Delivery-attempt records (event ID, attempt, start/end, duration, result, status; no payload). First live run found them dropped by the encoder; fixed with a regression test through the real ECS encoder (D26)
- [x] Poll-batch records (start/end, selected, published, failed, configured delay)
- [x] Startup config event: allowlisted settings, revision, instance ID (D11)
- [x] `V6__diagnostics.sql`: view `diagnostics.outbox_event_times`, two indexes, `diagnostics.blocking_sessions()` owned by `ledger_diag_owner` (D2, D4)
- [x] `scripts/provision-diagnostics.sh`: repeatable, upgrades an existing database without data loss, refuses to run before V6 (D5); ran twice on the Compose database
- [x] Tests through the `ledger_diag_reader` login (`DiagnosticDatabaseAccessTest`, 9 tests):
  - writes fail
  - payloads unreadable
  - timeout enforced
  - only the approved function is callable
  - the function runs as `ledger_diag_owner`
  - a non-`ledger_app` blocker is reported
- [x] `transaction_timeout=10s` verified to terminate a long `ledger_app` transaction: ended at 10.03 s (D3)
- [x] Every existing LedgerCore test still passes: 111 tests, 0 failures (91 existing + 20 new)

**IncidentLens:**
- [x] `deploy/compose.yaml`: IncidentLens PostgreSQL 17 (fsync on) and Prometheus v3.15.1 pinned by digest (5 s scrape, 24 h retention) (D24)
- [x] Database migrations (`migrations/001_telemetry.sql`): `telemetry_samples`, `log_files`, `log_records` (D21)
- [x] Fixture consumer stub (D1), labelled as a fixture; its log never names the active mode (D23)
- [x] Minimal controller: seeded workload, the stub's unavailable and slow switches, and a LedgerCore pause for induced gaps; ground truth in `evaluation/runs/` (D25)
- [x] Collector (`tests/integration/test_collector.py`, 12 tests; live blocker spacing p50 2.000 s, p99 2.005 s):
  - blocker samples every 2 s, with gap records
  - log tailing with file identity and offset in the same transaction, handling rotation and truncation
  - config history
- [x] Shared evidence envelope: coverage, truncation, redaction, requested and observed windows, source revision
- [x] Tools: `get_request_metrics`, `get_outbox_stats`, `search_delivery_logs`, `get_delivery_timings` (unit and integration tests; D20, D26)
- [x] Healthy calibration (**fixture result**): capacity 8.03/s; healthy rate 3.2/s, seed 20261106; 80 s baseline, pending max 4 (D8)
- [x] Integration tests (**fixture results**): healthy, unavailable and slow scenarios, plus an induced scrape gap, give correct observations, gaps and timestamps against the controller's ground truth (`tests/stack`, 5/5 passed)
- [x] End-to-end smoke test with LedgerCore's original in-process consumer (D1): 69 successful deliveries, 68 publications observed; timings honestly PARTIAL

## Stage 2: dependable backend

**Exit gate:** real-database concurrency and crash tests pass without an LLM.

**Stage 2 gate: PASSED on 2026-11-06.** 115 tests pass: 26 unit, 80 integration on real PostgreSQL, and 9 process tests with real signals (`evaluation/results/stage2/pytest-20261106T062622Z.log`, plus the review fixes below).

**By design, these gate tests use:**
- a fake provider (loopback, test mode only);
- a fake Prometheus for slow-source cases;
- the runner in the labelled unsandboxed development mode (D14, D33).

**End-to-end with the real provider and the real sandbox (§17): done in the Stage 3 pilot.** 6 jobs completed with `claude-sonnet-5-5` and the runner in OpenShell; job IDs are in `evaluation/results/stage3/pilot-*/run-*.json`.

- [x] Public API (8088) and internal API (8089) with the §7 and §8 contracts and error format (`api.py`, `internal_api.py`). The `export` endpoint and HTML escaping belong with the UI in Stage 4 (D37)
- [x] Browser authentication: one-time secret (only its hash configured), hashed sessions, 8 h expiry, HttpOnly SameSite=Strict cookie, CSRF on mutations, Host and Origin checks, 5 logins per minute (`auth.py`; process tests)
- [x] Jobs: state machine (enforced by a trigger as well), idempotent create, admission under the singleton control-row lock (1 active, 10 queued) (`jobs.py`)
- [x] Actions: server-issued `next_action_id`, durable reservation before I/O, immutable terminal results (trigger), `UNKNOWN`/`INTERRUPTED` after restart, nothing redispatched (`evidence.py`)
- [x] Evidence and progress events persisted in one transaction; report finalization in one transaction (`evidence.py`, `reports.py`)
- [x] Supervisor (`supervisor.py`):
  - advisory-lock singleton
  - 30 s lease and 5 s heartbeat
  - generation fencing and capability tokens
  - at most two recoveries
  - deadline never reset
  - 10-minute queue expiry
  - takeover closes the old generation's unfinished actions (D32)
- [x] Budgets enforced (§13): 8 model actions, 12 tool actions, 40,000 tokens with count-then-reserve (D29), 6,000 input tokens per call, 1 MiB evidence, 32 KiB report, 180 s deadline
- [x] Non-superuser database roles for the application and the collector; owner for migrations only (D31)

**§17 tests (real PostgreSQL):**
- [x] Concurrent repeated create gives one job; changed-body reuse returns 409 (12 parallel connections)
- [x] Queue admission never exceeds its cap under concurrency (25 parallel connections: exactly 10 admitted)
- [x] Old generation cannot append evidence or complete a report after takeover
- [x] Runner killed after evidence commit and before response receipt (SIGSTOP, then SIGKILL): restart reuses the record; generation 2, no repeated read, deadline kept
- [x] Gateway killed during a model action (SIGKILL): the unknown charge keeps its reservation; 2 model actions in total (bounded)
- [x] Cancel while a tool is in flight: no model request reached the provider; no evidence or report afterwards
- [x] IncidentLens database failure (proxy cut): zero source or model requests during the outage; recovery after it returns
- [x] One telemetry source fails: the report shows the loss, and NO_FAULT_OBSERVED becomes INCONCLUSIVE (D30)
- [ ] Historical query never substitutes current blockers without explicit mode: **moved to Stage 4.** It needs `get_database_blockers`, a Stage 4 tool
- [x] Read-only credential cannot mutate target tables; timeout stops an expensive permitted query (through IncidentLens's adapter, against the live target)
- [x] Instructions embedded in logs cannot cause forbidden tool names, destinations or writes (fixed schemas; unknown tools and arguments rejected and never stored)
- [x] Login, CSRF and ownership checks prevent cross-job reads. Escaping of report content is tested with the Stage 4 UI and HTML export (D37)

## Stage 3: agent pilot

**Two separate results:**
- **The gate is met** for its pilot part: C told the two faults apart, with evidence, compared against A and B, with errors inspected and cost measured.
- **The early stop has triggered (D13, D43):** on these faults C showed no advantage over B. Stage 4 has not started; the owner decides how to continue.
- **Still open in this stage:** the report-validation arithmetic check.

**Exit gate:**
- the pilot distinguishes two faults with evidence
- it is compared with the rules and one-call baselines
- errors inspected and cost reported (D13)

- [x] `runner.py` (standard library only) running in OpenShell: sandboxed process test, and 6 real pilot jobs (claim to first action 2.09 s) (D40)
- [x] Model gateway:
  - versioned system prompt (`PROMPT_VERSION`)
  - one typed action per step, strict tools
  - one repair attempt
  - count, reserve and reconcile tokens (D29)
  - SDK retries off
  - rendered evidence within the input limit (D38)
- [x] Report validation: citations, bounded lengths, required limitations, the source-loss rule, and the deterministic inconclusive fallback (D30). Numbers in claims are checked against the cited evidence and **flagged, not rejected**: on 15 real reports, 8 of 127 numbers (6 %) were not found literally, mostly correct derived values (D50). Weaker than "ensure"; the flagged rate is reported in Stage 5
- [x] Baseline A (rules over a fixed bundle) and baseline B (one model call), defined once before the pilot (D41)
- [x] Pilot runs (**fixture results**): 6 windows, 2 per scenario, across A, B and C. Read manually: A 4/6, B 6/6, C 6/6 (`evaluation/results/stage3/pilot-report.md`)
- [x] Errors inspected; measured cost per run: B $0.017, C $0.040 on average. **Owner to be asked before Stage 5 spending**
- [x] C shows **no advantage** on the pilot faults (equal accuracy, more tool calls, about 2.3 times the cost of B). Failure cases investigated (pilot report). **Stopped for the owner's decision before expanding (D43)**

## Stage 4: product

**Stage 4 gate: PASSED on 2026-11-06** for the product (D48). A developer completed an investigation through the UI in real Chrome and audited all 6 cited observations (`evaluation/results/stage4/browser-demo-20261106T113341Z/`). **Still open:** missing-evidence cases, which are designed with the Stage 5 case set.

**Exit gate:** a developer completes an investigation and audits its citations through the UI.

- [x] Start, Progress and Report screens; keyboard navigation (Tab to a citation, Enter, Escape); visible loading and error states (notice area, 10 s backoff); phone layout (390 px, no horizontal overflow)
- [x] Budget exhausted ("Stopped without a diagnosis"), source missing (a red note naming the unavailable sources) and no fault observed (its own banner) look different (`test_report_view.py`)
- [x] JSON and escaped-HTML exports (no scripts, no external resources): checked in the browser demo and in `test_browser.py`
- [x] Tools: `get_pool_metrics`, `get_database_blockers` (historical or current), `get_runtime_config`, with renderers; integration tests for blockers and config (7 tests). Built early for the extended pilot (D44)
- [ ] Controller faults (validated with `evaluation/validate_faults.py`, D45):
  - [x] credential rejection (303/303 failures were auth_error)
  - [x] lock contention, with a non-`ledger_app` blocker visible (6 `other` edges, 19 samples with blocking)
  - [ ] pool saturation on its own: **not producible without hooks.** At 1 connection and 3.2 or 12 transfers/s, there were 0 timeouts. What can be produced is pool exhaustion caused by lock waits: 68 acquisition timeouts, a causal chain whose root is lock contention
  - [x] missing-evidence cases (for example a source stopped during the window): built with the Stage 5 case set; 5 of 5 development and 15 of 15 held-out windows valid under protocol §4 (deciding source not COMPLETE, symptom visible in a remaining source)
- [x] Historical blocker query never substitutes current blockers without explicit mode (§17; moved from Stage 2): integration tests. A historical query with no samples is UNAVAILABLE; current mode is labelled outside the window and refused from a frozen bundle
- [x] Escaping: report and log text rendered escaped; HTML export has no scripts or external resources (§17; moved from Stage 2). Script and HTML payloads in the symptom and the model's report show as text, with no dialog, in real Chrome
- [x] Browser smoke check (`tests/process/test_browser.py`, real headless Chrome), plus a real-browser investigation with the real provider and the OpenShell runner, every citation audited (`evaluation/browser_demo.py`)

## Stage 5: evaluation

**Exit gate:** reliability gate and usefulness gate reported separately, with raw results.

- [x] `evaluation/protocol.md` written before the first development window: categories, validity, scoring, gates, uncertainty (D52). The freeze record (`evaluation/freeze.py`) covers code, LedgerCore revision, model ID, prompt version and the protocol's checksum; `freeze-dev.json` written 2026-11-06; **held-out freeze written 2026-11-06T17:04:19Z** (`freeze-heldout.json`), before the first held-out window
- [x] 35 development and 35 held-out templates (5 × 7 categories) in `evaluation/cases.py`; held-out parameters and distractors unseen in development (`check_templates`)
- [ ] Frozen evidence bundles with manifest and checksums for every case-run (D6). Built: `freeze_bundle` (read-only files, SHA-256 manifest) and replay mode (D51), unit-tested; **real runs pending**
- [ ] **Rescoped (D48):** P (the product) and A on each bundle in seeded random order; held-out: 3 recorded runs per case. C (the agent) is not re-run; its pilot result stands (D47)
- [ ] Metrics:
  - primary cause
  - causal depth
  - unsupported claims
  - citation relevance
  - false alarms
  - abstention
  - elapsed time
  - calls
  - cost
- [ ] Invalid cases counted and rerun, never dropped. Built: validity from the frozen bundle alone, at most 3 attempts per case-run (`evaluation/validity.py`, unit-tested)
- [ ] Blinded review sheet and rubric; owner marks; unblind and compute
- [ ] Usefulness gate (§16) reported pass or fail, with per-class results and case-clustered uncertainty

## Stage 6: release

**Exit gate:**
- a fresh checkout reproduces the demo and representative tests
- limits documented

- [ ] Runbook and setup scripts (§20), including automatic supervisor restart (D34)
- [ ] Retention (§11): jobs, evidence, reports and submission keys kept 7 days, as a separate privileged operation that never deletes active jobs or evidence they reference (telemetry's 24 h deletion already exists in the collector)
- [ ] Backup and restore to a fresh instance: citations retained; pending jobs recover without budget reset
- [ ] Secrets scan clean; dependency and license report
- [ ] README with separate reliability and usefulness results, each with commands and raw files
- [ ] Fresh-checkout reproduction

## Log of commands and outcomes

Entries are added as each stage runs: date, command, outcome and raw-output path.

### 2026-11-04: Stage 0

| Command | Outcome | Raw output |
|---|---|---|
| `OPENSHELL_VERSION=v0.1.7 sh install.sh` (installer reviewed first) | OpenShell CLI and gateway 0.1.7 installed; gateway connected over mTLS | `evaluation/results/stage0/openshell-install.log` |
| `openshell sandbox create … -- sh -c '…'` (first probe) | **Failed**: the supervisor could not reach the gateway in Docker host-network mode on Docker Desktop | `evaluation/results/stage0/sandbox-probe.log` (overwritten by the retry), gateway log |
| Set `supervisor_network_mode = "bridge"` in `gateway.toml`; restart the gateway | Sandbox starts. Default image has no `python3` or `curl`; home directory empty | `evaluation/results/stage0/sandbox-probe.log` |
| `docker build -f deploy/openshell/runner.Dockerfile` | Runner image with the full Python 3.12 standard library | `versions.json` |
| `scripts/stage0_openshell_smoke.sh 127.0.0.1` | **11/11 checks passed**: allowed with a valid capability, 401 with an invalid one; 8088, internet, ports 5432/55432/55433/17670 denied; another binary denied; no host paths or secret variables | `evaluation/results/stage0/openshell-smoke-20261104T153140Z/` |
| Separate `bash /dev/tcp` probe | Denied by the supervisor: `binary '/usr/bin/bash' not allowed in policy` | `evaluation/results/stage0/bash-denial-*.txt` |
| `./mvnw -q verify` (LedgerCore, before any change) | 91 tests, 0 failures, 0 errors, 0 skipped | `evaluation/results/stage0/ledgercore-baseline-verify.log` |
| `scripts/stage0_ledgercore_audit.sh` | 103 metric names; `/actuator/prometheus` 404; no histogram buckets; 6 deliveries left no log lines; logs are local-offset plain text | `evaluation/results/stage0/ledgercore-audit-20261104T153653Z/` |
| `uv run pytest -q tests/unit` | 8 passed (action parser: one valid action accepted; none, two, unknown, out-of-range, extra or server-only fields rejected) | console |
| Model check (`scripts/stage0_model_check.py`) | Not run on 2026-11-04: no API key yet | n/a |

### 2026-11-06: Stage 0 (model)

| Command | Outcome | Raw output |
|---|---|---|
| `git init -b main` (local only: no commit, no remote) and `git check-ignore -v .env.local` | `.gitignore:2:.env.local`; `git status` does not list the file | console |
| `uv run python scripts/stage0_model_check.py` | `claude-sonnet-5-5` listed for the account (created 2026-09-28). 2/2 calls returned a valid `call_tool` with `stop_reason: tool_use`. Estimate − reported input = 0 and 0. Output 136 and 87 tokens; latency 2.57 s and 1.51 s | `evaluation/results/stage0/model-check-20261106T043728Z.json` |

**Unresolved limitations found in Stage 0:**
- **Child processes inherit network permission.** OpenShell grants a binary's permission to its child processes too. A process started by the allowed `python3` can reach 8089. The runner must therefore never start subprocesses, and the model has no code-execution tool.
- **Other containers can reach 8089.** On Docker Desktop, a listener bound to the Mac's `127.0.0.1` is reachable through `host.docker.internal` from any container on this machine, not only from sandboxes. This is why every internal request must carry a valid capability (spec §4); network position alone is never trusted.
- **Policy and gateway changes:**
  - **OpenShell is version 0.1.x.** Its policy behaviour must be rechecked with this smoke test whenever the pinned version changes.
  - **The gateway config was changed.** Bridge mode is a change to OpenShell's gateway configuration, not to Docker Desktop. The original file is kept in the scratchpad.

### 2026-11-06: Stage 0 follow-up (report size and strict tools)

| Command | Outcome | Raw output |
|---|---|---|
| `uv run python scripts/stage0_report_probe.py` (original schema) | 3/3 `max_tokens`: report cut off | `evaluation/results/stage0/report-probe-20261106T053128Z.json` |
| Probe with size targets, short handles | Completes, but `findings` sometimes arrived as a JSON string: revealed that LangChain had dropped `strict` (D17) | `report-probe-20261106T053436Z.json` |
| `uv run python scripts/stage0_model_check.py` (strict set on each definition) | 2/2 valid `call_tool`; estimate minus reported input 0 | `evaluation/results/stage0/model-check-20261106T053603Z.json` |
| Probe, strict, raw counts in the evidence | 2–3/5 cut off by long reasoning text before the call | `report-probe-20261106T053840Z.json`, `…054005Z.json` |
| Probe, strict, tool-computed rates, "do not re-derive" | **5/5 valid reports**, 958–1,183 output tokens (D27) | `report-probe-20261106T054136Z.json` |

### 2026-11-06: Stage 1

| Command | Outcome | Raw output |
|---|---|---|
| `./mvnw -q test -Dtest='PrometheusExportTest,…'` (first run) | 2 failures: `/actuator/prometheus` returned 404, because Spring Boot disables metrics export in tests. Fixed with `@AutoConfigureMetrics` on that class only (D19) | console |
| `./mvnw verify` (LedgerCore) | 110 tests, 0 failures, 0 errors, 0 skipped | `evaluation/results/stage1/ledgercore-verify-20261106T045708Z.log` |
| Manual start of the jar with `LEDGERCORE_LOG_FILE` | ECS JSON lines with UTC `@timestamp`; key-value pairs written as top-level fields | `logs/ledgercore/ledgercore.json` (ignored) |
| `scripts/provision-diagnostics.sh` (twice, Compose database) | Both runs succeed; the login connects with `default_transaction_read_only=on`, `statement_timeout=2s` | console |
| `uv run python scripts/setup_local.py` | 4 credential files, mode 600, ignored by Git | `deploy/local/` (ignored) |
| `uv run python scripts/stack.py up` | All services up; LedgerCore revision `86e4bf1c38fb+dirty.f75b45ec8819`; Prometheus target `up` | console |
| `uv run pytest -q tests/integration/test_collector.py` | 12 passed (real PostgreSQL) | console |
| `PYTHONPATH=src uv run python evaluation/calibrate.py` | **Fixture result.** Capacity 8.03/s; healthy 3.2/s; 80 s baseline passed all 7 checks | `evaluation/results/stage1/calibration-20261106T051810Z.json` |
| `uv run pytest tests/stack -v` (run 1) | **2 failed, then stopped.** The healthy scenario had 207 publications but 0 delivery records, and the log tool still said COMPLETE. Cause: duplicate `eventId` (MDC plus key-value pair) made the ECS encoder drop delivery records (D26) | `evaluation/results/stage1/stack-tests-20261106T052004Z.log`, `scenarios-20261106T052010Z/` |
| `./mvnw verify` (after the D26 fix) | 111 tests, 0 failures (91 existing + 20 new, including the real-encoder regression test) | `evaluation/results/stage1/ledgercore-verify-20261106T052458Z.log` |
| `uv run pytest` (after the D26 fix) | 45 passed, including the lost-record detection test | console |
| `uv run pytest tests/stack -v` (run 2) | **Stopped after the healthy scenario failed.** 208 requests returned 201, but only 1 outbox event was created. Cause: a harness bug. The controller derived idempotency keys from the seed, so the rerun replayed run 1's stored responses. The tools reported this correctly. Keys are now unique per run | `evaluation/results/stage1/stack-tests-20261106T052630Z.log`, `scenarios-20261106T052637Z/` |
| `uv run pytest tests/stack -v` (run 3, run-unique keys) | 4 passed, 1 failed: the induced-gap test expected the gap to end at or after the resume. Prometheus timestamps samples at scrape start, so a gap can end up to one scrape timeout early (D28); the tool was right and the test was corrected | `evaluation/results/stage1/stack-tests-20261106T052901Z.log` |
| `uv run pytest tests/stack -v` (run 4, final code) | **5 passed** (6 min 54 s). Healthy: 208 requests, 209 arrivals, 207 publications. Unavailable: first refusal 0.55 s after the fault. Slow: handler p50 407.6 ms, no errors. Gap: both bounds within 0.25 s here. In-process consumer: 69 deliveries | `evaluation/results/stage1/stack-tests-20261106T054213Z.log`, `scenarios-20261106T054219Z/` |
| `uv run pytest` | 46 passed (26 unit, 20 integration on real PostgreSQL) | console |

**Unresolved limitations found in Stage 1:**
- **Fixture timings.** Consumer authentication and handler timings come from the test fixture. LedgerCore's in-process consumer has no such timer, so with it only total attempt time is measured (D1, D23).
- **Gap resolution.** Gap bounds are accurate to about one scrape interval (5 s); shorter interruptions can be missed (D28). Blocker samples every 2 s can miss shorter lock waits (spec §5).
- **Regrown files.** A log file truncated and regrown past the saved offset while the collector is stopped cannot be detected; a single line over 1 MiB stops that log's ingestion, which is visible as a lag (D21).
- **Report size.** Reports must be compact to fit 1,500 output tokens. Measured only on synthetic evidence so far (5/5); the Stage 3 pilot measures real repair rates (D27).
- **Uncommitted LedgerCore changes.** They are reported as revision `86e4bf1c38fb+dirty.ed5247a36278`.
- **Customer access to metrics.** Customer logins can still read `/actuator/metrics`, an observed limitation left unchanged.

### 2026-11-06: Stage 2

| Command | Outcome | Raw output |
|---|---|---|
| `uv run python scripts/setup_local.py --add-missing` | Added the app and collector database passwords and the access-secret hash; the secret was printed once and kept in `deploy/local/access-secret.txt` (mode 600) | console (secret masked) |
| `uv run pytest -q tests/integration/test_jobs.py` (first run) | 3 failed: appending a progress event after a terminal transition tripped the immutability trigger. Fixed by appending before the transition, in the same transaction | console |
| `uv run pytest -q tests/process` (first full run) | 4 failed. Three came from a test-proxy bug (a 5 s socket timeout closed idle database connections); one was a test sending duplicate Origin headers. Code unchanged; tests fixed | `/private/var/…/pytest-16/` (temporary) |
| `uv run pytest -q tests/process` (second run) | 1 failed: **a real recovery deadlock.** After a takeover, the new runner waited forever on an action the dead generation left RUNNING. Fixed (D32), with a regression test | console |
| `uv run pytest -v` | **113 passed** (2 min 2 s) | `evaluation/results/stage2/pytest-20261106T062622Z.log` |
| Advisor review of Stage 2 | **Three fixes before the gate.** (1) The ownership test was vacuous: the fixture empties the tables, so no foreign job existed. It now inserts another developer's job with evidence and a report and probes 6 endpoints, all of which return the missing-job 404; a real evidence ID requested under the wrong job is also 404. (2) A blocking database call ran on the event loop in `create`. (3) Capabilities now expire at the deadline (§8); new tests cover that and old-generation `/report` | console |
| `uv run pytest -q tests/unit tests/integration` then `tests/process` | 106 passed, then 9 passed | console |
| `uv run pytest tests/stack -v` (after the Stage 2 role changes) | **5 passed** (6 min 54 s): the collector writes as `incidentlens_collector`, the tools read as `incidentlens_app`, and the Stage 1 gate still holds | `evaluation/results/stage1/stack-tests-20261106T063502Z-after-stage2.log` |

### 2026-11-06: Stage 3

| Command | Outcome | Raw output |
|---|---|---|
| `uv run python scripts/stage3_prompt_budget.py …` | Worst case with 12 real observations: 8,020 tokens with the first prompt builder, then 4,790 with the renderer and explicit shortening (limit 6,000) | `evaluation/results/stage3/prompt-budget-*.json` |
| `uv run python scripts/stage3_report_probe.py` | 5/5 valid reports on real rendered evidence, 939–1,148 output tokens | `evaluation/results/stage3/report-probe-20261106T063909Z.json` |
| `uv run pytest tests/process/test_sandboxed_runner.py` (first run) | Failed: the upload created a directory named `runner.py`, and early exits left 3 sandboxes behind. Both fixed (D40) | `logs/runner/il-02644bf8-*.log` |
| Same, after the fix | Passed: full investigation with the runner in OpenShell; sandbox deleted; claim to first action 2.09 s | console |
| `uv run python scripts/stack.py gateway up` (first) | Readiness 503: the app login could not read `schema_migrations`. Migration 004 (D42) | `logs/gateway/gateway.log` |
| `PYTHONPATH=src:. uv run python evaluation/pilot.py 1` (two passes, seeds 20261107 and 20261108) | 6 windows. A 4/6, B 6/6, C 6/6 (manual reading). C: 18–30 s, 4–6 tools, $0.031–0.050. B about $0.017. **Early stop triggered: no C advantage** | `evaluation/results/stage3/pilot-20261106T093932Z/`, `…095030Z/`, `pilot-report.md` |
| `uv run pytest -v` (after the Stage 3 changes) | **126 passed** (2 min 18 s), including the sandboxed-runner process test | `evaluation/results/stage3/pytest-20261106T100203Z.log` |

### 2026-11-06: extended pilot preparation (D44, D45)

| Command | Outcome | Raw output |
|---|---|---|
| `uv run pytest -q tests/integration/test_blocker_and_config_tools.py` | 7 passed | console |
| `evaluation/validate_faults.py credential_rejection lock_contention` | Both signals present | `evaluation/results/stage4/fault-validation-20261106T103415Z.json` |
| `POOL_SIZE=1 POOL_TIMEOUT_MS=250 evaluation/validate_faults.py pool_saturation` (first) | LedgerCore failed to start: "the configuration of the pool is sealed once started". An environment-variable binding quirk; settings now go through `SPRING_APPLICATION_JSON` (D45) | `logs/ledgercore/stdout.log` |
| Same, then `POOL_RATE=12` | 1 pending scrape and 0 timeouts at 3.2/s; 0 and 0 at 12/s. **Not credible as a standalone fault** | `fault-validation-20261106T103825Z.json`, `…104031Z.json` |
| `POOL_SIZE=2 evaluation/validate_faults.py pool_exhaustion_by_lock_waits` | 68 acquisition timeouts, pending in 3 scrapes, plus blocker edges | `fault-validation-20261106T104224Z.json` |
| `scripts/stage3_prompt_budget.py` (7 tools offered) | Worst case 5,110 tokens (limit 6,000) | `evaluation/results/stage3/prompt-budget-20261106T104348Z.json` |
| `PILOT_HEALTHY_WINDOWS=1 PILOT_SEED=20261109 evaluation/pilot.py 2 extended` (first attempt) | **Discarded:** crashed before any run, because of code edited mid-run (D46) | `evaluation/results/stage3/pilot-extended-*.log` |
| Same, rerun with the fixed gateway | 7 valid windows. Manual reading: A 7/7, B 7/7, C 7/7. C: 7.0 tool calls and 6.0 model calls on average, $0.064 per run against B's $0.018. **The D44 criterion (5.6 or fewer calls) was not met** (D47) | `evaluation/results/stage3/pilot-20261106T104730Z/`, `pilot-report.md` |

### 2026-11-06: rescope to the fixed-bundle product (D48)

| Command | Outcome | Raw output |
|---|---|---|
| Owner's decision after D47 | **Rescope: B is the product.** The adaptive agent is kept only as a non-default mode | `docs/decisions.md` D48 |
| `uv run pytest -q tests/unit tests/integration` | 125 passed (bundle phase; terminal-only decisions; agent mode still tested) | console |
| `uv run pytest -q tests/process` | 10 passed, including the sandboxed runner, on the product flow | console |
| `PILOT_SEED=20261110 evaluation/pilot.py 1 product_smoke` | **The product end to end (real provider, OpenShell runner):** a slow-consumer window, 7 bundle tools then 1 model decision, 16.1 s, $0.019. Correct (TENTATIVE); A missed it again | `evaluation/results/stage3/pilot-20261106T112213Z/` |

### 2026-11-06: Stage 4

| Command | Outcome | Raw output |
|---|---|---|
| Claude in Chrome (`tabs_context_mcp`) | Not available: the browser extension is not connected. Used Playwright driving the installed Google Chrome 156 instead (D49) | console |
| `evaluation/browser_demo.py 2026-11-06T11:28:33 2026-11-06T11:31:03 "…late."` (first) | Failed: Playwright video needs an extra ffmpeg download, which I did not run. Video dropped; screenshots kept | console |
| Same, rerun | **PASSED.** Real provider and OpenShell runner. "Diagnosis supported in the observed window": consumer refused connections for about 100 s (controller: 90 s, 11:29:03–11:30:33). All 6 citations opened by mouse and 1 by keyboard. HTML export has no scripts or external references; no dialogs; no phone overflow. One console 404 (favicon), fixed | `evaluation/results/stage4/browser-demo-20261106T113341Z/` |
| `uv run pytest -v` | **138 passed** (2 min 54 s) | `evaluation/results/stage4/pytest-20261106T113522Z.log` |
| Arithmetic check measured on 15 real reports | 8 of 127 numbers (6 %) not found literally in cited evidence, mostly correct derived values; flagged, not rejected (D50) | console |
| `uv run pytest -v` | **141 passed** (2 min 55 s) | `evaluation/results/stage4/pytest-20261106T114100Z.log` |

### 2026-11-06: Stage 5 build

| Command | Outcome | Raw output |
|---|---|---|
| AskUserQuestion: Stage 5 spending | **Approved: full evaluation** (about $2.70, about 12 h unattended) | conversation |
| `uv run pytest -q tests/unit` | 49 passed (adds replay, validity, number-check tests) | console |
| Fixture auth time from telemetry (14,864 deliveries) | p50 70 ms, p99 79 ms, max 157 ms: the 2,400 ms held-out delay stays under the 3 s timeout | console |
| `stack.py gateway up --replay …` (first) | Refused: the bundle directory did not exist yet (server check working as designed). Harness now creates it first | `logs/gateway/gateway.log` |
| `evaluation/freeze.py write development` | 72 files; `check` matches | `evaluation/freeze-dev.json` |
| `evaluation/stage5.py run development --only cu-d1,sc-d1,cr-d1,lc-d1,lp-d1,he-d1,me-d1` (smoke, 1 per category) | **7 of 7 valid on the first attempt.** Replay verified: all 49 evidence rows of the 7 P jobs came from their own frozen bundle (`source.replayed_from`). P: 0 fallbacks, $0.017–0.021 per run, about 16–18 s replay elapsed. P's primary cause right on 5 of 5 fault runs (keyword aid, development only); no false alarm on healthy; **TENTATIVE on the missing-evidence run** (its summary says "the cause is not determined", but TENTATIVE counts as inappropriate, protocol §5.3). The keyword aid misread that report as lock contention (it matched "blocker"). A: right on 4 of 5 fault runs (INCONCLUSIVE on slow consumer), TENTATIVE "publisher throughput" on missing evidence | `evaluation/results/stage5/smoke-20261106T143747Z.log`, `case-runs/*/state.json` |
| `evaluation/freeze.py write development` (rewrite) | Only `evaluation/score.py` added (scoring, produces no bundles); every other file and key unchanged. Smoke freeze kept | `evaluation/results/stage5/freeze-dev-1-smoke.json` |

| `evaluation/stage5.py run development` (full batch; resumes past the 7 smoke case-runs) | **35 of 35 case-runs valid on the first attempt; 0 invalid, 0 interrupted.** 28 windows recorded in 1 h 34 min (15:00–16:34 UTC). Two harness faults: the background-traffic threads (§6) crashed at the LedgerCore restart in lp-d2 (overspend ran about 20 s of 120 s) and lp-d3 (replays ran about 10 s of 120 s). Both windows are still valid under §4 (the distractor is not a validity condition). Development is not gated, so they were not re-recorded | `evaluation/results/stage5/development-20261106T150007Z.log` |
| `evaluation/score.py score development` (prompt `2026-11-06.1`) | **P:** keyword aid 24 of 25 fault runs; **0 of 5 appropriate abstentions** (all 5 missing-evidence reports TENTATIVE, each saying the cause is not determined); 0 of 5 false alarms; 0 fallbacks; 122 findings, all cited; 24 of 297 numbers flagged (D50); $0.64 for 35 runs; replay elapsed median 16.1 s. **A:** 20 of 25 (missed all 5 slow-consumer runs: INCONCLUSIVE 4, "publisher throughput" 1); 3 of 5 abstentions; 0 of 5 false alarms | `evaluation/results/stage5/summary-development.json` |
| Author's reading of P's 25 fault reports (unblinded, development only) | **The keyword aid overcounts.** It read sc-d2 as lock contention: the report names the slow handler as the cause and mentions locking only as healthy. Reading: consumer-unavailable 5/5, credential 5/5, slow consumer 5/5, lock contention 5/5. **Lock-pool (strict D45): 0 or 1 of 5.** Four lead with the pool change and list the blocking session beside it (lp-d1, d2, d3, d5); lp-d4 puts the restart first, then says the blocking session caused the 500/503s, a borderline call. **P's strict total: 20 or 21 of 25** | `case-runs/*/state.json` |
| D50 flags reviewed (24) | **8 were checker false positives:** a percentage restating a ratio (8.1 % for 0.0808). The other 16 are correctly flagged: derived counts ("about 7 buckets (~70 s)"), derived rates ("~8/s"), a derived sum, "1.5 minutes", and "HTTP 401" where the cited text says only auth_error | console |
| Edits after development (protocol §8, revision 1) | Prompt `2026-11-06.2`: outcome definitions and how combined causes are explained. Number check accepts a percentage restating a ratio (+1 unit test). Distractor threads survive a LedgerCore restart. `score.py --prompt-version`. `stage5.py replay` now checks the freeze (it did not before, although D52 said it did) | `evaluation/protocol.md` §8, D53 |
| `uv run pytest -q tests/unit tests/integration` | 139 passed | console |
| `evaluation/freeze.py write development` (rewrite) | Differences from the batch freeze: prompt version, protocol checksum, `controller.py`, `score.py`, `stage5.py`, `model_gateway.py`, `reports.py`. Batch freeze kept | `evaluation/results/stage5/freeze-dev-2-batch.json` |
| `evaluation/stage5.py replay development` (prompt `2026-11-06.2`, all 35 bundles) | **Acceptance check met:** missing evidence 5 of 5 INCONCLUSIVE; healthy 5 of 5 NO_FAULT_OBSERVED; no fault run turned INCONCLUSIVE. Verified on the server: all 35 reports stored with prompt `2026-11-06.2`, source `model`; all 245 evidence rows carry `replayed_from`. One malformed reply was repaired once (sc-d3). $0.68; replay elapsed median 22.1 s | `evaluation/results/stage5/replay-development-2026-11-06.2-20261106T164055Z.log` |
| `evaluation/score.py score development --prompt-version 2026-11-06.2` | Keyword aid 22 of 25 (it misread lp-d2 and lp-d5 as pool, and sc-d4 as lock). **Author's reading (unblinded): 24 of 25.** Consumer-unavailable, credential, lock contention 5/5 each; slow consumer 5/5 (sc-d2 borderline: it names the slow handler but says the cause of the dip is not confirmed); **lock-pool 4 of 5 under strict D45** (d2–d5 attribute the pool exhaustion to the blocking session; lp-d1 still leads with the pool and calls the locks a "likely contributor"). Abstention 5/5; false alarms 0/5; 0 fallbacks; 114 findings, all cited; **D50: 18 of 304 numbers flagged (5.9 %)**, all derived values (for example "30 s" from 30000 ms, a summed 111). **Side effect:** SUPPORTED fell from 15 to 3 of 25 fault runs. The product is now more cautious than the evidence warrants on clear cases (for example 1,392 refused connections reported TENTATIVE). Not gated, not tuned further (protocol §8 limit) | `evaluation/results/stage5/summary-development-2026-11-06.2.json` |
| Harness check (not evaluation data): each distractor kind for 8 s against live LedgerCore | All 4 kinds started and stopped cleanly, 0 request errors; `distractor_stopped` now records the error count. `load_bump` had never run live | `evaluation/runs/harness-check-distractors-1793984149/controller.jsonl` |
| Held-out scoring path dry-run on development data (scratch script; P = the `.2` replays; my picks, labelled unblinded; mechanics-only finding judgements) | Found 2 problems before the freeze and fixed them: (1) with incomplete picks, held-out primary cause fell back to the keyword aid; it is now reported "NOT SCORED" and its gates are left unset; (2) pick 6's wording ("no other cause given") did not match §5.2; reworded, and the sheet now says to pick the root cause. After the fix: strict 24/25, lenient 25/25, lock-pool causal depth 4/5, gates computed; the sheet shows no case, category or system and has no scripts; `key.json` round-trips; the sample is 100 of 114 findings with seed 20261106 | scratchpad script `dryrun_heldout_scoring.py` |
| Mechanics check (not evaluation data): LedgerCore with a pool of 1 (lp-h1, lp-h4 use it; development used 2 and 3) | Ready after 3.3 s; 22 of 22 transfers returned 201 at 2.2/s; default pool restored after 3.2 s | `evaluation/runs/harness-check-pool1-*/controller.jsonl` |
| LedgerCore revision in the freeze | `86e4bf1c38fb+dirty.ed5247a36278`: the commit plus a hash of the uncommitted diff and untracked files, so the telemetry changes are pinned | `evaluation/freeze-heldout.json` |
| `uv run pytest -q tests/unit tests/integration`, then `tests/process` (gateway stopped to free 8088/8089) | 139 passed; 12 passed (2 min 46 s), including the OpenShell sandboxed runner | console |
| `evaluation/freeze.py write held_out` | 73 files, written 2026-11-06T17:04:19Z. Differs from the development freeze only in `score.py` and the protocol checksum (the scoring fixes above); prompt `2026-11-06.2`, model `claude-sonnet-5-5`, mode `fixed_bundle`. **From here on, nothing in the manifest may change** | `evaluation/freeze-heldout.json` |
| `evaluation/stage5.py run held_out` (started 17:04 UTC, 105 case-runs, background) | **Stopped by host sleep after 40 case-runs.** The Mac (on battery, lid open) entered idle sleep at 19:41:36 UTC once the session went idle. Case-runs 1–39: valid on the first attempt, no sleep overlap. **sc-h2-r2:** window recorded before the sleep, bundle frozen in a maintenance wake (all sources COMPLETE); **P FAILED**: the OpenShell runner exited three times in about 5 s while the Docker VM clock lagged the host by about 15.5 min. **cr-h3-r2:** recording stretched across the sleep; harness stopped at about 21:45 UTC; attempt 1 becomes `interrupted` (not a validity failure). Handling is pre-declared in **D54**: sc-h2-r2 counts as a P miss in the primary result, plus a labelled host-sleep sensitivity. Original state archived (`state-as-recorded.json`). **Correction:** I reported 44 finished case-runs to the owner; the log holds 40 | `evaluation/results/stage5/held_out-20261106T170426Z.log`, `pmset -g log`, `evaluation/runs/stage5-cr-h3-r2-a1/controller.jsonl` |
| Checks before resuming (2026-11-07 about 02:55 UTC) | Database and Prometheus clocks within 5 ms of the host; collector heartbeat 4 s; outbox empty; LedgerCore ready; OpenShell connected; `freeze.py check` matches; on mains power (the owner plugged in) | console |
| `caffeinate -i env PYTHONPATH=src:. uv run python evaluation/stage5.py run held_out` (resume, background), plus a battery guard and a watchdog | **Finished 07:17 UTC, exit 0.** 65 case-runs in 4 h 25 min. **All 105 held-out case-runs valid**: 104 on the first attempt; cr-h3-r2 on attempt 2 after the interrupted attempt 1. 0 invalid attempts, 0 no-valid-attempt. **No host sleep during the resume**, checked by timestamp in `pmset -g log`. The watchdog's count-based sleep check could not have fired, because the power log rotated (810 entries became 793); it is replaced by the timestamp check. Partly on battery; the battery guard never triggered. **P:** 104 COMPLETED (all reports from the model, prompt `2026-11-06.2`) and 1 FAILED (sc-h2-r2, D54). Outcomes: TENTATIVE 73, INCONCLUSIVE 16, SUPPORTED 8, NO_FAULT_OBSERVED 7. Held-out P cost **$2.02**, so Stage 5 total is about $3.34, against about $2.70 approved (overrun reported to the owner earlier). `freeze.py check` still matches | `evaluation/results/stage5/held_out-20261107T025239Z-resume.log`, `case-runs/*-h*-r*/state.json` |
| `PYTHONPATH=src:. uv run python evaluation/score.py sheet` | 104 reports (sc-h2-r2 has no report) and 100 of 355 findings (seed 20261106). Checked: no case IDs, ground-truth labels, system names or scripts in `sheet.html`. **Waiting for the owner's blind marks** in `picks.csv` and `findings.csv` | `evaluation/results/stage5/review/` |
