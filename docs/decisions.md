# IncidentLens: decisions and discrepancies

This file records where the specification (`docs/TECHNICAL-DESIGN.md`, revision 1.0) meets the real LedgerCore repository, dependency APIs or this machine, and how each conflict is resolved.

None of these decisions weakens a specification gate. Each entry is marked **open** until its stage has evidence, then **verified**, citing that evidence.

## D1. Consumer faults use an external fixture, not LedgerCore code

- **Conflict:** the evaluation needs controllable consumer failures, but LedgerCore changes are limited to telemetry and diagnostic access.
- **Resolution:**
  - A test-only consumer stub lives in `evaluation/fixtures/consumer_stub/`. LedgerCore points to it through its existing `LEDGERCORE_CONSUMER_URL` setting, so no LedgerCore code changes.
  - The stub verifies the publisher credential with real BCrypt at a configurable cost.
  - It records authentication and handler timings, and can be made unavailable, slow, or rejecting.
  - Its control port is loopback-only and used only by the controller.
- **Labelling:** every result produced with the stub is labelled a **fixture result**, and stub timings are labelled "fixture consumer". The stub is not described as an independent deployed service (spec §5).
- **Extra check:** an end-to-end smoke test also runs against LedgerCore's original in-process consumer (`/demo-consumer/events`). It shows that deliveries succeed and that the tools observe its publications. Status: done 2026-11-06. `test_smoke_with_ledgercores_original_in_process_consumer` passed: 69 successful deliveries, 68 recorded publications, 60 requests on `/demo-consumer/events`. Delivery timings were reported as PARTIAL, saying the in-process consumer has no auth or handler timer.

## D2. Lock-contention and pool-saturation faults also avoid LedgerCore hooks, and must stay distinguishable

**Lock contention:**
- **How it is caused:** the controller holds row locks on busy accounts through its own privileged database connection while load runs.
- **What the blocker function must show:** the relationship between a blocked `ledger_app` session and its blocker, *even when the blocker is not `ledger_app`*. It filters on the blocked session being `ledger_app`, and reports any blocker with a coarse role label (`ledger_app` or `other`), its state and wait details. It never returns query text or the blocker's role name.

**Pool saturation:**
- **How it is caused:** the controller restarts LedgerCore with a smaller pool under load.
- **Which signal counts:** a saturation case counts only if its window shows **pending borrowers (`hikaricp_connections_pending` > 0) or acquisition timeouts**. A shrunken pool alone is not evidence.

**Status:** open (Stage 4). If either fault cannot produce its required signal, I stop and report it. Hooks are not added silently.

## D3. Timestamps are labelled for what they are

- **Arrivals:** `outbox_events.created_at` is `now()`, the time the inserting transaction *started*, not when it committed. Rows become visible only at commit, so a recent time bucket can still grow.
  - **Label:** "committed outbox records grouped by creation (transaction-start) timestamp".
  - **Not:** commit time.
- **Publications:** `published_at` is set by `markPublished` as `now()` inside its own single-statement transaction. That is also a transaction-start timestamp, although that transaction is short.
  - **Label:** "recorded publications grouped by publication-transaction timestamp".
- **Settle time:** a bucket is final only after the longest possible transaction has ended. `ledger_app` connections set `transaction_timeout=10s` (PostgreSQL 17), so a wait of ten seconds is enough *only if that bound is shown to apply*.
  - Stage 1 verifies it: a `ledger_app` transaction that runs past 10 s is terminated.
  - Until then, the tools mark any window ending less than 10 s before the query as `PARTIAL`.
  - Waiting fixes late visibility. It does not turn transaction-start timestamps into commit timestamps.
- **Status:** verified 2026-11-06.
  - LedgerCore's `DiagnosticDatabaseAccessTest.applicationTransactionsEndAtTheTenSecondTransactionTimeout` takes a connection from the application's own pool, which reports `transaction_timeout = 10s`. It runs a transaction past 10 s; PostgreSQL ends it at 10.03 s with "terminating connection due to transaction timeout".
  - `get_outbox_stats` marks durable counts `PARTIAL` with a "may still grow" limitation when the window ended less than 10 s ago (`TARGET_SETTLE_SECONDS`).

## D4. The diagnostic login carries its own settings, and the adapter enforces them too

- **The mistake to avoid:** setting `default_transaction_read_only` and `statement_timeout` on a NOLOGIN group role does **not** configure sessions of its members. Role-specific settings apply to the role that actually logs in; membership grants privileges, not settings.
- **Roles:**

  | Role | Kind | Holds |
  |---|---|---|
  | `ledger_diagnostics` | NOLOGIN | The grants: `SELECT` on the diagnostic views and `EXECUTE` on the blocker function |
  | `ledger_diag_owner` | NOLOGIN | Only `pg_read_all_stats`; owns the SECURITY DEFINER function, which would otherwise run as the superuser schema owner |
  | `ledger_diag_reader` | LOGIN, `INHERIT`, member of `ledger_diagnostics` | Its own settings, set on the login itself: `ALTER ROLE ledger_diag_reader SET default_transaction_read_only = on` and `... SET statement_timeout = '2s'` |

- **Adapter:** the IncidentLens adapter *also* opens every transaction read-only and applies the timeout itself (`BEGIN READ ONLY`, then `SET LOCAL statement_timeout`), so a misconfigured role cannot remove either protection.
- **Tests run through `ledger_diag_reader` itself:**
  - writes fail
  - payload columns are unreadable
  - an expensive query hits the timeout
  - only the approved function is callable
  - that function runs as `ledger_diag_owner`
- **Status:** implemented and tested 2026-11-06.
  - **LedgerCore:** `DiagnosticDatabaseAccessTest` has 9 tests, all run through `ledger_diag_reader` itself:
    - the login's own `default_transaction_read_only = on` and `statement_timeout = 2s`;
    - writes denied by grants even after the session turns read-only off;
    - payload and ledger tables unreadable;
    - exactly one executable application function, which is SECURITY DEFINER, owned by the non-superuser `ledger_diag_owner`, with a fixed `search_path`;
    - blocker edges for an `other` blocker and a `ledger_app` blocker;
    - only the approved columns returned.
  - **IncidentLens adapters:** they open every target transaction `READ ONLY` (`conn.read_only = True`) and run `SET LOCAL statement_timeout` (`tools/sources.py`, `collector.py`).
  - **Narrowed claim:** "only the approved function is callable" applies to application-schema functions. Built-in `pg_catalog` functions granted to PUBLIC (such as `pg_sleep`) stay callable, bounded by read-only transactions, grants and the 2 s timeout.

## D5. Existing LedgerCore databases can be upgraded, with a defined order

`docker/initdb` scripts run only when a database volume is first created. Creating the login only there would leave existing installations without it.

**Order:**
1. **Migration `V6__diagnostics.sql`** (Flyway, as the schema owner) creates the NOLOGIN roles `ledger_diagnostics` and `ledger_diag_owner` if missing. It also creates the views, indexes and function, and grants to `ledger_diagnostics`. It contains no password.
2. **A repeatable provisioning step**, `scripts/provision-diagnostics.sh`, runs as the owner through `psql` after V6.
   - It fails with an actionable message if V6 has not been applied.
   - It creates `ledger_diag_reader` if missing, sets its password from the environment, applies its session settings and grants membership.
   - It is safe to rerun and never drops or rewrites data.
3. **New volumes:** a new `docker/initdb` script may create the login early. The provisioning step stays the supported path for both new and existing databases.

**Tests:** Testcontainers runs the same provisioning SQL after migrating.

**Status:** implemented and tested 2026-11-06.
- **Existing databases:** `DiagnosticDatabaseAccessTest.provisioningUpgradesAnExistingDatabaseAndRefusesBeforeV6` migrates a new database to V5, inserts an outbox event, and shows provisioning refuses with the V6 message. It then migrates to V6, provisions twice, and confirms the event is intact and readable through the view.
- **The real Compose database:** `scripts/provision-diagnostics.sh` ran twice and reported success both times.
- **Migration requirement:** V6 needs a migration role that can create roles and change function ownership (a superuser, as in Compose and Testcontainers).
- **Upgrade cost:** V6's two `CREATE INDEX` statements block outbox inserts, and therefore transfers, while they build. This is documented in the migration.

## D6. Recorded-window evaluation uses frozen, immutable evidence bundles

Querying the same historical window twice does **not** guarantee identical observations: logs can arrive late, publication records can change, and retention can remove data.

**Per case-run:**
1. Record the live window.
2. Wait for the settle time (D3).
3. Export a **finalized evidence bundle**: the tool envelopes for every permitted argument combination, at maximum limits. Smaller limits are served by deterministic slicing.
4. Write the bundle's manifest with SHA-256 checksums, and make its files read-only.

**Investigating:**
- All three systems run against that bundle, in a randomized order, through the gateway's bundle mode.
- Checksums are verified before each system run.
- Results are labelled **frozen-bundle (replay) evaluation**, per spec §16.

**What this means:**
- `current` blocker mode is unavailable in bundle mode, and is recorded as such.
- The live tool path is still tested separately (live integration tests and the live browser demo).

**Status:** open (Stage 5).

## D7. Evaluation time is estimated honestly

- **Per window:** about 4 minutes of recording, plus three investigations of up to 3 minutes each, is **up to about 13 minutes**.
- **In total:** 140 windows (35 development plus 105 held-out) is **about 30 hours before overhead**. Actual runs may be faster.
- **Effect of bundles:** they separate recording from investigating, so the work can be split across sessions without the 24-hour retention limit. Recording and investigating are not overlapped, because elapsed time is a reported metric.

## D8. Healthy runs must actually be healthy

- **The risk:** LedgerCore's README measured delivery at about 6.8 events/s, limited by BCrypt plus the 1 s pause after each batch. Any faster workload grows a backlog and is not a healthy case.
- **What Stage 1 does:**
  - Measure delivery capacity with the fixture at its baseline authentication cost.
  - Fix the healthy workload rate well below it, and record its seed.
  - Show that pending stays bounded across at least 60 s of baseline.
- **Not fixed here:** the real LedgerCore backlog bottleneck, which is out of scope.
- **Status:** done 2026-11-06 (**fixture result**).
  - **Capacity:** `evaluation/calibrate.py` measured 8.03 publications/s while a 600-event backlog drained. The fixture consumer ran at BCrypt cost 10.
  - **Healthy rate:** 3.2 events/s, 40 % of capacity, seed 20261106.
  - **Baseline:** 80 s, 256 arrivals and 255 publications. Pending peaked at 4 and ended at 2, with no failed events, every transfer accepted (201) and complete scrape coverage.
  - **Records:** `evaluation/results/stage1/calibration-20261106T051810Z.json` and `evaluation/healthy_profile.json`.

## D9. The model is pinned by its API identifier, after checking it is available

- **Not enough:** a display name such as "Sonnet 5.5" is not a pin.
- **Stage 0:**
  - List the account's models through the provider API.
  - Record the exact model identifier that is available, with its creation date if reported.
  - Make one real call with it.
  - Write the identifier and the call's response model field into `versions.json`.
- **Prices:** taken from the provider's pricing page, with the date, into a versioned pricing file. They are never recalled from memory.
- **Status:** verified 2026-11-06.
  - `claude-sonnet-5-5` is listed by the Models API for this account: display name "Claude Sonnet 5.5", created 2026-09-28, 1,000,000 input and 128,000 output tokens maximum.
  - Both real calls returned `claude-sonnet-5-5` in the response model field.
  - Raw output: `evaluation/results/stage0/model-check-20261106T043728Z.json`.

## D10. Token accounting uses the provider's count endpoint

- **What the spec asks for:** "the selected model's tokenizer". Anthropic's tokenizer is not available locally, so the gateway estimates input with the provider's token-count endpoint.
- **Reservation:** that estimate plus the maximum output tokens.
- **Stage 0:** compare the estimate with the usage the provider reports and record the difference.
- **Status:** verified 2026-11-06. The count endpoint matched reported input exactly in both calls: 2,291 = 2,291 and 2,292 = 2,292, a difference of 0.
  - The estimate was made with the same system prompt, messages, tools and thinking setting as the call, through `ChatAnthropic.get_num_tokens_from_messages(..., tools=..., thinking=...)`.
  - Reservation per call is the estimate plus 1,500, for example 3,791.
  - Output used: 136 and 87 tokens.
  - **If the count request fails:** the gateway does not guess. It records the failure and does not dispatch the call.

## D11. Config history comes from a startup log event

- **What LedgerCore writes:** one structured JSON log event at startup, with allowlisted non-secret settings, revision and instance ID.
- **History:** the collector ingests it into versioned history, keyed by effective time.
- **Why:** this avoids a new table, a new grant and a database write at startup.
- **Status:** implemented 2026-11-06.
  - **The record:** `StartupConfigRecord` writes the allowlisted record when the application is ready; `DiagnosticRecordsTest` checks its keys and that no secret appears in it.
  - **History:** the collector stores each record as a `ledgercore.config` log record. One appears per start, so a restart adds history rather than overwriting it.
  - **Lookup:** `target_config()` returns the record in effect at the window end and lists any restarts inside the window.
  - **Revision:** passed in at launch as `<commit>+dirty.<hash of the uncommitted changes>` while the Stage 1 changes are uncommitted, never as the bare commit.

## D12. The internal listener bind is a gate

- **Preferred:** 8089 listens on loopback.
- **Acceptable:** the Docker/VM bridge address, or a host firewall rule shown to block other machines.
- **Failed gate:** binding to `0.0.0.0` exposes the listener to the local network, so it counts as a failed gate even though capability tokens still guard every request.

**Status: verified (Stage 0).**
- **Result:** 8089 bound to `127.0.0.1` is reachable from the sandbox. The listener saw the requests arrive from `127.0.0.1` through Docker Desktop's `host.docker.internal` forwarding. Raw output: `evaluation/results/stage0/openshell-smoke-20261104T153140Z/`.
- **Consequence:** any container on this Mac can reach that loopback port, not only sandboxes, so the capability check on every internal request remains essential.

## D15. OpenShell on Docker Desktop uses bridge supervisor networking

- **The problem:** OpenShell's Docker driver defaults to host networking for its supervisor container. On Docker Desktop for Mac, that container's `127.0.0.1` is inside Docker's VM, so the supervisor could not reach the gateway and the first sandbox failed to start. The gateway log shows `failed to connect to OpenShell server`.
- **The options:** OpenShell's documentation offers two fixes: enable Docker Desktop host networking, or set `supervisor_network_mode = "bridge"`.
- **Choice:** bridge mode, set in `/opt/homebrew/var/openshell/gateway.toml`. It changes only OpenShell's configuration, not Docker Desktop globally.
- **Isolation is unchanged:** the workload container still runs with `network=none`, and all of its egress goes through the supervisor's policy proxy.

**Status:** verified (Stage 0).

## D16. Refusals are recorded, not silently re-routed to another model

- **The skill default:** Anthropic's SDK guidance suggests enabling server-side `fallbacks` for Claude Sonnet 5.5, so a refused request is re-run on another model.
- **Why it is not enabled:** IncidentLens pins one model and freezes it for evaluation (spec §2, §16). A silent switch to another model would mix models within a run and break that pin.
- **What happens instead:** a `stop_reason: "refusal"` is recorded on the action as a model error. The job then takes the normal path: one bounded retry or repair if the budget allows, otherwise a deterministic inconclusive report.

**Status:** decided (Stage 0); exercised in Stage 3.

## D17. Model actions are tool calls with strict schemas and automatic tool choice

- **Constraint:** Claude Sonnet 5.5 rejects forced tool choice (`any`/`tool`).
- **How the gateway offers actions:**
  - It offers one strict tool per permitted diagnostic tool, plus `submit_report` and `stop_inconclusive`.
  - It uses `tool_choice: auto` with parallel tool use disabled.
- **How replies are checked:** `src/incidentlens/actions.py` converts the reply into exactly one action. No action, more than one, an unknown or unavailable name, or invalid arguments become a `MalformedAction`, which gets at most one repair (spec §8).
- **Limits:** length and count limits are checked by Pydantic after the reply rather than sent to the provider, so a violation becomes a malformed action instead of a provider error.

**Status:** unit-tested (8 tests) and checked with the real model on 2026-11-06.
- **Both thinking settings obeyed the action schema.** Each returned one valid `call_tool` action with `stop_reason: tool_use`:
  - `between_tools`: `search_delivery_logs` (outcome any, limit 50), 2.57 s.
  - adaptive at low effort: `get_runtime_config`, 1.51 s.
- **Correction (2026-11-06, later the same day): the first check was not strict.** langchain-anthropic 1.7.9 passes a tool that is already in Anthropic format (`name`, `description`, `input_schema`) through unchanged and ignores `bind_tools(strict=True)` for it, so the request carried no `strict` flag. The report probe (D27) exposed this when `findings` arrived as a JSON string instead of an array.
  - **Fix:** `provider_tools()` sets `"strict": true` on every definition. A unit test checks the bound request LangChain would send.
  - **Rerun:** with the flag set, the model check returned a valid `call_tool` from both configurations (`evaluation/results/stage0/model-check-20261106T053603Z.json`); the estimate minus reported input was again 0.
- **Text before the tool call.** Both replies put a short text block before the tool call. The gateway stores that text as untrusted model commentary. It is never shown as a finding and never parsed as an action.
- **Provisional thinking setting: `between_tools`.**
  - **Why:** neither reply in this check included a thinking block, so the full 1,500-token output limit stays available for the action itself and a report is not cut off by thinking tokens.
  - **Before the freeze:** the Stage 3 pilot may compare it with adaptive at low effort under the same 1,500-token limit.
  - **After the freeze:** the chosen setting is frozen in `evaluation/protocol.md` before Stage 5. The limit is not raised to make room for thinking.
- **What one call shows:** that the contract works, not that the model is reliable. Malformed-action rates come from the Stage 3 pilot and the Stage 5 runs.

## D13. Stage 3 keeps the early stop

- **What the pilot must show:** more than telling two faults apart. It compares the agent with the rules baseline (A) and the single-call baseline (B), inspects every error, and reports measured cost.
- **If the agent offers no useful advantage:** that is investigated before building the full UI or the long evaluation (spec §19: "stop expansion and inspect the failure cases first").
- **Status:** open (Stage 3).

## D14. If the sandbox fails, development continues in a labelled mode

- **Decided by:** the project owner.
- **What happens:** if Stage 0 cannot establish the OpenShell network path, the blocker is recorded here and in `TODO.md`, and development continues in a clearly labelled unsandboxed development mode.
- **Gates:** the isolation gate and the release gate stay **FAILED** until the sandbox path is shown to work.

## D18. LedgerCore's diagnostic records are off unless switched on

- **What is added:**
  - One structured record per delivery attempt: event ID, attempt numbers, start and end, duration, result, HTTP status, and whether the PUBLISHED transition was recorded.
  - One record per polling pass that selected or failed events.
  - Two timers, `ledgercore.outbox.delivery` and `ledgercore.outbox.poll`.
- **No payload:** records never carry the payload, amounts or accounts. `DiagnosticRecordsTest` checks this.
- **Order unchanged:** the poller still sends, then updates the database. Records and timers come after the update and cannot throw into the loop.
- **The row count:** `markPublished` now returns its row count. An acknowledgement without a transition, because an operator changed the event meanwhile, is then visible and kept separate from a recorded publication (spec §5).
- **Off by default:**
  - The `ledgercore.diagnostics` logger is OFF unless `LEDGERCORE_DIAGNOSTICS_LOG_LEVEL=INFO`.
  - The JSON file exists only when `LEDGERCORE_LOG_FILE` is set. Its format is Spring Boot's ECS, with UTC `@timestamp`, verified on the running jar at +05:30.
  - So LedgerCore's console output and benchmark behaviour are unchanged unless diagnostics are switched on. `scripts/stack.py` switches both on.
- **If records are off:** log tools report the missing startup record as a limitation instead of "no failures".
- **Idle polls:** passes that selected nothing are timed but not logged. Whenever a backlog exists, every pass selects events, so the pause between batches is measured from consecutive batch records.

## D19. The Prometheus export is readable only by the diagnostics login

- **The rule:** `/actuator/prometheus` matches before `/actuator/**` and requires `DIAGNOSTICS_READER` only.
  - Ledger users, including the admin, get 403 on the new endpoint.
  - Existing `/actuator/metrics` access is unchanged; the observed limitation below still applies.
- **The login:** separate from `security.users`, because every entry there receives `LEDGER_USER`.
- **Buckets:** fixed boundaries, exported in seconds, rather than Micrometer's default of about 70 percentile buckets:
  - HTTP: 5 ms to 10 s.
  - Delivery: 5 ms to 5 s.
  - Poll: 10 ms to 60 s.
  - The `http.server.requests` setting also gives buckets to the long-task timer `http.server.requests.active`, which is harmless.
- **Tests:** Spring Boot 4.1 turns metrics export off in tests unless a test opts in with `@AutoConfigureMetrics`. Only `PrometheusExportTest` opts in, so it runs in its own application context.

## D20. Request counts come from raw samples, not extrapolation

- **How it counts:** `get_request_metrics` reads the raw Prometheus samples and computes counter increases in code, treating a counter drop as a restart.
  - **Baseline:** the last successful scrape before the window.
  - **New series:** a series first seen after scraping was already working counts from zero, because Micrometer creates meters on first use.
  - **Percentiles:** interpolated inside the fixed buckets, after summing buckets across series.
- **Why:** counts stay whole numbers, and restarts and gaps stay visible. PromQL `increase()` extrapolates to the window edges and can report fractional requests.
- **Limitation:** requests between the last scrape inside the window and its end are counted in the next window.

## D21. Log ingestion and log coverage

- **Tables:** `log_files` and `log_records` sit beside `telemetry_samples`. Spec §11 lists only `telemetry_samples`, but §20 requires deduplication on `(source_file_id, byte_offset)` and offsets saved in the same transaction.
- **Coverage:** a log tool reports `COMPLETE` only when both hold:
  - a collector heartbeat after the window end shows the log read to its end (lag 0);
  - no discontinuity (truncation, missing file, rotation lost while stopped) was recorded since the window start.
- **Collector downtime:** this alone does not reduce log coverage. The lines stay in the file and are read later from the saved offset. It does reduce blocker-sample coverage, which is handled in Stage 4.
- **Rotation:** LedgerCore's rolled files stay uncompressed, so the collector can finish a rotated file by its inode.
- **Known limitation:** a file truncated and regrown past the saved offset while the collector is stopped, with the same inode, cannot be detected from its size.
- **Known limitation:** a single line longer than the 1 MiB read limit stops that log's ingestion. This is visible, not silent: heartbeats keep showing a lag above zero, so log tools report `PARTIAL`. LedgerCore's lines, including ECS stack traces, are far shorter.

## D22. The evaluation workload stays at or below 50 arrivals per second

- **The cost test:** `DiagnosticDatabaseAccessTest.thirtyMinuteWindowCountsStayFastAtTheHighestEvaluationRate` loads 24 h of events at 2/s plus a 30-minute burst at 50/s. It requires both 30-minute bucket queries, arrivals and publications, to finish in under 1 s through the diagnostics login (the timeout is 2 s), and a narrow window to use `outbox_created_at_idx` (spec §10).
- **The ceiling:** the controller must not exceed 50 arrivals/s unless that test is rerun at the new rate.

## D23. The fixture consumer cannot leak the fault label, and faults stay distinguishable

- **No label in evidence:** the fixture's timing log records only measurements (receipt time, event ID, status, outcome, auth and handler milliseconds), never the active mode.
- **Unavailable:** closes the listener, so LedgerCore records `CONNECTION_FAILED` (network_error) and no consumer record exists.
- **Slow:** adds handler time while deliveries still succeed. The controller refuses a delay at or above LedgerCore's 3 s delivery timeout, which would turn slowness into `TIMEOUT` failures that look like unavailability.
- **Reject:** verifies against a different hash at the same cost, so the full BCrypt time is spent before the 401. It is used in Stage 4.

## D24. Prometheus is pinned to v3.15.1

- **Why not the newest:** v3.16.0 was published on 2026-11-05, one day before Stage 1. The previous patch release, v3.15.1 (2026-10-21), is pinned by digest instead.

## D25. Controller credentials are separate from IncidentLens's

- **Controller file:** `deploy/local/controller.env` holds the workload logins (customer A, admin) and the target database owner's password, which Stage 4's lock-contention fault needs. Only the evaluation controller loads it.
- **IncidentLens file:** `incidentlens.env` holds only IncidentLens's database password and the read-only diagnostics login.
- **Child processes:** LedgerCore, the fixture consumer and the collector receive an explicit minimal environment from `scripts/stack.py`, never the model API key.

## D26. Defect found by the first Stage 1 scenario run: delivery records were silently dropped

- **Symptom (2026-11-06):**
  - The healthy scenario showed 207 recorded publications in the window but **zero** delivery-attempt records.
  - `search_delivery_logs` still reported `COMPLETE`, which reads as "no deliveries".
  - Poll-batch and config records were present.
- **Cause:**
  - During a delivery the poller puts `eventId` in the logging MDC, and the delivery record also had an `eventId` key-value pair.
  - Spring Boot's ECS file format writes MDC entries and key-value pairs as top-level JSON fields, so the duplicate name made the encoder fail.
  - Logback reports appender failures only to its internal status, so each delivery record vanished without a trace.
- **Why LedgerCore's tests missed it:** they captured log events in memory and never ran the JSON encoder.
- **Fixes:**
  - **LedgerCore:**
    - The record names its event `outboxEventId`; the MDC's `eventId` is unchanged.
    - A new test, `DiagnosticRecordsTest.deliveryRecordsEncodeAsEcsJsonAlongsideTheMdcEventId`, runs the real ECS encoder while the MDC is set. It failed before the fix and passes after it.
  - **Collector:** reads `outboxEventId`, falling back to the MDC `eventId` for ordinary log lines written during a delivery.
  - **Tools (the deeper problem):** an empty result must not be trusted when another source contradicts it.
    - `search_delivery_logs` and `get_delivery_timings` now compare the stored attempt records with the attempts LedgerCore's poll-batch records report for the window.
    - Far fewer records (beyond one batch at the window edge) makes coverage `PARTIAL`, with the counts shown.
    - Integration test: `test_missing_delivery_records_are_detected_from_the_poll_batches`.
- **Results:**
  - LedgerCore verify: 111 tests, 0 failures.
  - IncidentLens: 45 tests pass.
  - The live scenarios were rerun after the fix (TODO log).

## D27. A full report must fit in one 1,500-token reply, while the spec's hard bounds stay

- **The conflict inside the spec:** §14 allows a summary of 1,000 characters, 8 findings, 10 alternatives, limitations and next checks, and strings up to 2,000 characters. §13 caps each model call at 1,500 output tokens. A report anywhere near §14's bounds cannot fit in one reply.
- **Measured** with `scripts/stage0_report_probe.py`: real `submit_report` calls on synthetic evidence, between_tools thinking, strict tools.
  - **Original schema, no size guidance:** 3 of 3 hit `max_tokens`; the tool input was cut off.
  - **Size guidance, raw counts in the evidence:** 2 to 3 of 5 still hit the limit. The model writes 600 to 1,450 characters of reasoning before the call, mostly re-deriving rates, and ignores "no text" instructions.
  - **Size guidance, evidence shown as the tools return it (rates already computed), and "do not re-derive":** **5 of 5 valid reports**, 958 to 1,183 output tokens, 3 findings each.
  - **Raw results:** `evaluation/results/stage0/report-probe-*.json`.
- **Resolution:**
  - **Validation keeps the spec's hard bounds unchanged.** An earlier tighter draft was reverted, because it would have narrowed §14.
  - **Compact targets in the descriptions** the model reads: usually 3 or fewer findings, 2 alternatives, 2 limitations and 2 next checks, each text under 160 characters, summary under 250.
  - **Short evidence handles:** the gateway shows evidence as E1, E2 and so on, and maps them back to the saved evidence IDs before validation and storage, so stored reports still cite real IDs. A UUID costs far more tokens per citation.
  - **Tool output carries computed rates and differences** (spec §9 already requires this), so the model need not do arithmetic in its reply.
  - **A report still cut off** (`stop_reason: max_tokens`) is a malformed action: one repair asking for a shorter report, then the deterministic inconclusive fallback (§8, §14). The 1,500-token limit is not raised.
- **Status:** confirmed in real investigations. In the Stage 3 pilot, 6 jobs made 24 real model calls, with **0 malformed replies, 0 reports cut off, 0 repairs** and no errored or unknown calls. The largest report used 1,165 of 1,500 output tokens. This is a small sample of the slow-consumer, unavailable and healthy cases.

## D28. Gap bounds follow Prometheus's timestamps

- **Observed (induced-gap scenario):** LedgerCore was paused from 05:33:47.2 to 05:34:07.2, and the reported scrape gap ran from 05:33:43.7 to 05:34:03.7.
- **Why the end looks early:** Prometheus timestamps a sample with its scrape's *start* time. The 05:34:03.7 scrape started during the pause, waited, and succeeded when LedgerCore resumed at 07.2, inside the 4 s scrape timeout.
- **What the tools say:** a gap can start up to one interval before an outage and end up to one scrape timeout before recovery, so each bound is accurate to about one scrape interval (5 s). Every scrape summary states this (`gap_bounds`).
- **The test:** asserts those bounds. The first version wrongly assumed a gap cannot end before recovery.

## D29. Model calls are counted, then reserved, then sent

- **The arithmetic problem:** a flat worst-case reservation (6,000 input + 1,500 output) allows only 5 calls inside the 40,000-token total, while the budget allows 8 model actions (§13).
- **The order:**
  1. Admission inserts a PENDING model action without tokens. It requires only that 1,500 output tokens could still fit.
  2. The executor counts the exact request with the provider's count endpoint, which is free.
  3. In one transaction, it reserves the counted input plus 1,500 and marks the action RUNNING.
  4. Only then does it send the call.
- **Refused without sending:** more than 6,000 input tokens, or a total that would exceed 40,000. A failed count also means no call (D10).
- **Unknown or failed calls:** a timeout or lost connection is `UNKNOWN` and keeps its whole reservation. A 429 or 5xx is `ERROR` and also keeps it; this is conservative, since the spec allows exactly-once billing to be unknowable.
- **Completed calls** reconcile the budget to the usage the provider reported.
- **After a gateway restart:** a PENDING model action was never sent, so it ends with an error and no charge. A RUNNING one becomes `UNKNOWN`.

## D30. Reports are submitted by reference, and a lost source is never healthy

- **By reference:** the runner names the model action whose recorded decision is the report, or asks for the fallback with `give_up`. It never sends report text, so a compromised runner cannot forge a report. This is stricter than §8's "submit typed report".
- **Validation:**
  - Handles (E1…) map to the job's own evidence IDs.
  - Unknown citations get one repair, shared with malformed actions (`repairs_used`, at most 1), then the deterministic fallback.
  - Limitations required by the code (incomplete, truncated or failed sources) are merged in and cannot be removed.
  - Usage comes from the action records.
- **The source-loss rule:** a `NO_FAULT_OBSERVED` outcome while any saved observation is `UNAVAILABLE` becomes `INCONCLUSIVE`, with a stated limitation (§17).
- **The fallback** is `INCONCLUSIVE`. It claims no cause and lists what was observed, by handle and coverage.

## D31. IncidentLens's own database uses separate, non-superuser roles

- **Owner** (`incidentlens`, the container superuser): runs migrations and provisioning only.
- **`incidentlens_app`** (gateway, supervisor, API):
  - No UPDATE or DELETE on evidence, progress events or reports.
  - UPDATE on sessions is limited to `revoked_at`.
  - Read-only access to telemetry.
- **`incidentlens_collector`:** only the telemetry tables, including deleting telemetry older than 24 h.
- **Triggers for everyone, the owner included:**
  - Terminal investigations and terminal actions cannot change.
  - Only §11's transitions are allowed.
- **Provisioning:** the roles are created NOLOGIN by migration 002 and given passwords by `provision_logins()`, called from `scripts/stack.py`; no password appears in a migration.
- **One clock:** every lease, deadline, expiry and heartbeat comparison uses the database clock, so the processes agree.

## D32. A takeover closes the old generation's unfinished actions

- **Found by:** the database-loss process test. An action left `RUNNING` by a dead generation made the new runner wait forever. The abandoned action's ID was also still `next_action_id`, so a new runner would have replayed it endlessly.
- **Fix:** the requeue transaction closes every unfinished action of the job, and issues a new `next_action_id`:
  - reads become `INTERRUPTED`;
  - dispatched model calls become `UNKNOWN`, keeping their reservation;
  - undispatched calls end with an error.
- **A late result** from the old executor then finds the action already terminal and changes nothing. Regression test: `test_a_takeover_closes_the_old_generations_unfinished_action_and_issues_a_new_id`.

## D33. Test mode is explicit; the standard profile cannot be redirected

- **Accepted only with `INCIDENTLENS_TEST_MODE=1` and a 127.0.0.1 address:**
  - the fake provider;
  - the fake Prometheus;
  - a test database reached through a proxy.
- **The standard profile always passes `https://api.anthropic.com` explicitly.** langchain-anthropic otherwise reads `ANTHROPIC_BASE_URL` or `ANTHROPIC_API_URL` from the environment, which could silently redirect model calls.
- **Process tests:**
  - **Real crashes:** the gateway and supervisor run as real processes, and crashes are real signals (SIGSTOP, SIGKILL), not exceptions.
  - **Labelled runner:** the runner runs in the development mode (D14). The real-provider and real-sandbox end-to-end runs are Stage 3 items.

## D34. The supervisor exits when it loses the database; restarting it is the operator's job

- **Spec §12:** "if that connection is lost, stop launching work and stop the active runner". The supervisor does that and exits with status 2.
- **The process test** restarts it after the database returns. The new supervisor then recovers the job once its lease expires (30 s).
- **Stage 6:** the runbook adds automatic restart (a launchd or process-manager entry). Until then this is a documented manual step.

## D35. Provider errors, refusals and retries

- **Error classes:**
  - A timeout or dropped connection is `UNKNOWN`: the request may have been processed and billed.
  - A 429 or 5xx is `ERROR`.
  - A refusal (`stop_reason: refusal`, D16) or a reply cut off at the output limit is a malformed decision. Either uses the job's single repair.
- **Retries:** the runner retries a failed model call at most once, with a new server-issued action ID and a fresh reservation (§12). SDK retries stay off.

## D36. The access secret is also kept in an owner-only file

- **Spec §7:** print the 32-byte secret once and do not commit it.
- **What setup does:** prints it once, and also writes it to `deploy/local/access-secret.txt` (mode 600, ignored by Git), so the browser demo and the process tests can sign in. Only its SHA-256 is in the configuration the server reads.

## D37. The export endpoint and HTML escaping arrive with the UI

- **Deferred to Stage 4:** `GET /investigations/{id}/export` and the escaping tests for report and log text. They are UI behaviour (§15) and are tested with it.
- **What Stage 2 does instead:** stores report and log text as data; never renders it.

## D38. The prompt carries rendered summaries, shortened explicitly to fit

- **The problem:** the first prompt builder cut each observation's raw JSON at 2,500 characters. For a 15-minute window that hides `get_outbox_stats`' arrival and publication rates behind its gauge series, and it keeps almost nothing of a delivery-log observation (about 20,000 characters).
- **The renderer** (`evidence_render.py`): one per tool. Each puts the decisive numbers first, always keeps coverage, gaps and up to two limitations, shows a few sample rows, and ends with "full observation saved as En". The same renderer feeds B and C.
- **Explicit shortening:**
  - **Older observations go first:** the oldest are reduced to a compact line (header plus decisive numbers, marked "shortened in this prompt"). In the extreme they become a header marked "omitted from this prompt". Nothing is dropped silently (spec §13).
  - **A second count:** if the provider's count still exceeds 6,000, the executor rebuilds with half the evidence budget and counts again.
- **Measured** (`scripts/stage3_prompt_budget.py`, 12 real saved observations):
  - **Before:** 8,020 tokens worst case.
  - **After:** 4,790 tokens, with 6 observations shortened and none reduced to a header. The system prompt, tools and scope alone are 2,301 tokens.
  - **Raw results:** `evaluation/results/stage3/prompt-budget-*.json`.
- **D27, rechecked on real rendered evidence** (`scripts/stage3_report_probe.py`, the slow-consumer scenario, **fixture result**): 5/5 valid reports, 939–1,148 output tokens. This was a size and schema probe, not a quality evaluation.

## D39. Only implemented tools are offered during Stage 3

- **The list:** `OFFERED_TOOLS` (request metrics, outbox stats, delivery logs, delivery timings) is the one list used for the model's tool definitions, action parsing, the runner's context and gateway admission (`TOOL_NOT_OFFERED`).
- **Why:** the three Stage 4 tools would return UNAVAILABLE every time, spending budget and skewing the A/B/C comparison. The list widens in Stage 4.

## D40. OpenShell runner launch details, found in Stage 3

- **The wrapper:** one process creates the sandbox, then runs the runner in it with `sandbox exec`, so the supervisor keeps heartbeating while the sandbox starts. The capability reaches it on stdin, through a shell built-in.
- **Facts about OpenShell 0.1.7, found by running it:**
  - **Uploads:** `--upload /absolute/path/runner.py:/sandbox/runner.py` created a *directory* named `runner.py`. Uploading to `/sandbox/` keeps the file name.
  - **Names:** sandbox names are limited to 19 characters. Job sandboxes use `il-<8 hex>-g<generation>`, 14 characters.
  - **Git filtering:** inside a Git work tree, uploads apply `.gitignore` rules. `runner.py` is not ignored.
- **Cleanup:** every runner exit, including an early one that leads to recovery, goes through the launcher's stop, which deletes the sandbox. The first sandboxed run left three sandboxes behind, because the early-exit path skipped that step.
- **Measured:** with the runner in OpenShell, claim to first admitted action took **2.09 s** of the 180 s deadline (`tests/process/test_sandboxed_runner.py`).

## D41. Baselines and the pilot's classification aid

- **Defined once, before any pilot run** (`evaluation/baselines.py`):
  - **The fixed bundle:** request metrics (transfers), outbox stats, delivery logs (any, 100), delivery timings.
  - **A:** fixed rules with thresholds in code.
  - **B:** one model call over the bundle, with the same renderer and only the terminal actions.
  - **C:** the job itself; it starts from the same first two tools.
- **Scoring free-text reports:** in the pilot, B's and C's primary cause is read with a keyword classifier that is labelled *provisional*, and every report is also read in full. The evaluation instead uses the blinded manual review with a written rubric (spec §16); an automatic classifier alone is not accepted there.
- **Why this is a pilot:** each system queries the live recorded window (A and B share one bundle). The frozen-bundle replay is Stage 5 (D6).

## D42. Readiness checks that the schema is current

- **The check:** `/health/ready` compares the newest migration file with the applied migrations, using the application login. Migration 004 grants that login read access to `schema_migrations`.
- **Found:** the first live start reported "not ready" because the login could not read the table.

## D43. Stage 3 early stop triggered: the agent showed no advantage on the pilot faults

- **Result:** 6 recorded windows, 2 each for healthy, unavailable consumer and slow consumer (**fixture results**).
  - **Accuracy, read manually:** A 4/6, B 6/6, C 6/6.
  - **Cost and calls:** C cost $0.040 per run against B's $0.017, and used 5 tool calls on average against B's fixed 4.
  - **Full analysis:** `evaluation/results/stage3/pilot-report.md`.
- **What D13 requires:** "if the agent offers no useful advantage, investigate before building the full UI or the long evaluation". The investigation found:
  1. The fixed bundle already contains every source these two faults need, so adaptive selection cannot win on them.
  2. C wastes calls on the in-process consumer route in 4 of 6 runs; only the Stage 4 runtime-config tool would let it know that route is idle.
  3. C's cost is mostly re-reading the evidence on each call.
- **What the pilot cannot test:** whether C gains on faults whose decisive source differs per class, namely pool saturation, lock contention and credential rejection. That is the comparison spec §16 is built on.
- **Status:** stopped for the owner's decision. No Stage 4 work has started, and no evaluation spending has happened.

## D44. Extended pilot before any UI (the owner's decision after D43)

- **Decided 2026-11-06, by the owner:** build the three remaining tools and the three remaining fault injectors, then rerun A, B and C on faults whose decisive source differs by class. The UI is built only if C shows an advantage.
- **Frozen before any extended run:**
  - **B's bundle is every implemented tool (7):** request metrics (transfers), outbox stats, delivery logs (any, 100), delivery timings, pool metrics, database blockers (historical, 50), runtime config. This makes B as strong as it can be.
  - **C is offered the same 7 tools,** and keeps the 2-tool initial bundle.
  - **A's rules for the new faults** (pool saturation, lock contention, credential rejection) and the classifier's new keywords are written in `evaluation/baselines.py` before the extended runs, and are not changed after seeing them.
- **Windows:** 2 per new fault (credential rejection, lock contention with a non-`ledger_app` blocker, pool saturation) plus 1 healthy, 7 in all. Each new fault must first be shown to produce its required signal (D2); a fault that cannot is reported, not forced with hooks.
- **What would count as an advantage, as an indication only** (the sample is small; spec §16's real gate is Stage 5):
  - C at least as accurate as B with **5.6 or fewer** tool calls on average (20 % fewer than B's 7), or
  - C at least 10 points more accurate.
- **What can and cannot be concluded:** the earlier pilot had only 2 windows per scenario. It showed no *observed* advantage, which does not prove the two systems are equal.

## D45. A standalone pool-saturation fault cannot be produced without hooks; it only arises downstream of lock waits

- **Fault validation** (`evaluation/validate_faults.py`, **fixture results**, `evaluation/results/stage4/fault-validation-*.json`):
  - **Credential rejection:** signal present; 303 of 303 failed deliveries were `auth_error`.
  - **Lock contention:** a separate owner session held row locks on 30 % of the workload's source accounts for 40 s. Signal present: blocking in 19 samples, 6 edges with blocker role `other` (the controller's session) and 7 `ledger_app` edges (transfers queued behind each other on the same accounts).
  - **Pool saturation on its own:** LedgerCore was restarted with a 1-connection pool and a 250 ms acquisition timeout.
    - At 3.2 transfers/s: pending above zero in 1 scrape, maximum 1, 0 timeouts, acquisition mean 0.09 ms.
    - At 12/s: 0 scrapes with pending, 0 timeouts, mean 0.06 ms.
    - **Not credible.** LedgerCore's transactions take about a millisecond, so one connection does not saturate at any load within D22's ceiling. Per D2, this is reported, not forced with LedgerCore hooks.
  - **Pool exhaustion caused by lock waits** (2-connection pool, 250 ms timeout, plus held locks): **68 acquisition timeouts**, acquisition mean 17.8 ms, pending in 3 scrapes, plus the blocker edges. Transactions hold their connection while waiting on a lock, so the pool runs out. This is the realistic, hook-free way LedgerCore's pool is exhausted.
- **Found along the way:** passing `SPRING_DATASOURCE_HIKARI_CONNECTION_TIMEOUT` as an environment variable made LedgerCore fail to start ("the configuration of the pool is sealed once started"). Relaxed binding can read it as `connection.timeout`, and binding a nested `connection` property calls `HikariDataSource.getConnection()`, sealing the pool too early. `scripts/stack.py` now passes Hikari settings with exact names through `SPRING_APPLICATION_JSON`.
- **Extended pilot fault set** (replaces "pool saturation" in D44), 7 windows: credential rejection ×2, lock contention ×2, lock contention with pool exhaustion ×2, healthy ×1.
  - **Ground truth for the chain:** primary cause *lock contention*, with the expected mechanism *pool exhausted by connections waiting on locks* (causal depth).
- **Symptoms, frozen before the runs.** They describe what customers see, never the cause:
  - Credential rejection and healthy: "Customers say that transfer notifications are arriving late."
  - Both lock scenarios: "Some customers say their transfers are failing or taking several seconds."
  - The two lock scenarios therefore share a symptom and must be told apart by evidence.
- **A's new rules, frozen in `evaluation/baselines.py` before the runs,** in order:
  1. Delivery-failure categories, as before.
  2. Blocker edges with role `other` in at least 2 samples: lock contention, with any pool timeouts noted as the mechanism.
  3. Pool timeouts or pending borrowers without blocking: pool saturation.
  4. Throughput rules, as before.
  5. Healthy only if there is also no blocking, no pool timeouts and no transfer 5xx.

## D46. NaN samples are missing data, and evidence is always valid JSON

- **Found during the extended pilot (first attempt):**
  - During the pool-exhaustion window, LedgerCore's outbox gauges, which query its database, could not get a connection, and Micrometer exported them as `NaN`.
  - Python's JSON encoder writes `NaN`, PostgreSQL rejects it, so committing the evidence failed and the action ended as an internal error. An integration test running at the same moment hit exactly this.
- **Fixes:**
  - `PrometheusClient.raw_samples` drops non-finite samples and counts them (`Series.unreadable`).
  - `get_outbox_stats` reports them as `unreadable_samples` with a stated limitation, treated as missing, never as a number.
  - `build_envelope` turns any remaining non-finite number into null, and its size check uses `allow_nan=False`.
  - Regression test: `test_non_finite_numbers_never_reach_storage`.
- **The first extended-pilot attempt was discarded, not counted.** The fix was edited into the working tree while that pilot ran. The tool modules load lazily, so the pilot then mixed old and new code and crashed collecting its first bundle. It recorded no runs and created no jobs, and the fault state was clean afterwards. The pilot was rerun from the start with the fixed gateway.
- **Process rule:** no code edits while a long experiment is running.

## D47. Extended pilot: still no advantage for the agent; the UI waits for the owner's decision

- **Result** (`evaluation/results/stage3/pilot-report.md`, **fixture results**): 7 valid windows.
  - **Accuracy, read manually:** A 7/7, B 7/7, C 7/7. Five windows were unambiguous for all three; in the 2 lock-plus-small-pool windows, B and C found both parts but led with the pool. A's new rules were written knowing the injectors' signals, though frozen before the runs.
  - **Calls and cost:** C averaged 7.0 tool calls (B 7) and cost $0.064 against B's $0.018.
  - **Accuracy hit the ceiling for every system.** Abstention, false alarms beyond 3 healthy windows, and §16's variations are untested, not passed.
  - **The frozen criterion was not met:** at most 5.6 tool calls at matched accuracy, or 10 points more accuracy.
- **Both pilots together:** A 11/13, B 13/13, C 13/13. C never used fewer calls than B and cost 2.3 to 3.6 times as much.
- **Why:** C keeps checking every remaining source after a cause is supported. Changing that (a stopping rule in the prompt, a different initial bundle) is tuning on development cases. It is allowed only if written down and frozen before anything held out, and it must not be presented as rescuing the result.
- **Status:** per D44, the UI is built only if C shows an advantage, and it did not. Stopped for the owner's decision.

## D48. Rescope: the product is the fixed-bundle, one-decision investigation (B)

- **Decided 2026-11-06, by the owner,** after D43 and D47. This is a **deliberate departure from the specification**, which chose an adaptive agent (§1, §2, §8, §16).
- **Why:** across 13 pilot windows, B was as accurate as the agent (C) at a third to two fifths of the cost, with no more tool calls. C showed no observed advantage. Spec §16 says to report a failed gate honestly rather than disguise it.
- **What the product does:**
  - **Same pipeline:** one investigation job through the Stage 2 pipeline (sessions, admission, durable action reservations, generation fencing, recovery, budgets), with the runner in the OpenShell sandbox.
  - **Collection:** the runner collects the fixed bundle of every implemented tool as ordinary tool actions (7, in the order of `incidentlens/bundle.py`). Each becomes saved, citable evidence.
  - **Decision:** then one model decision, offered only `submit_report` and `stop_inconclusive`. A malformed decision gets the one repair, then the deterministic fallback, as before.
  - **Unchanged:** the report checks (citations, required limitations, the source-loss rule, bounded lengths).
- **The agent is kept as a non-default mode** (`INVESTIGATION_MODE = "agent"` in `model_gateway.py`), so the pilot can be reproduced. It is not the product, and Stage 5 does not need it.
- **How Stage 5 judges the product (stated here so nothing reads as the original gate passing):**
  - **Unchanged absolute thresholds from §16:**
    - at least 80 % correct primary causes on valid fault runs;
    - at least 80 % appropriate abstention on insufficient-evidence runs;
    - at most 10 % false alarms on healthy runs;
    - every factual finding has valid references;
    - at most 5 % unsupported factual findings by the blinded manual rubric.
  - **The trade-off clause** now compares the product with the rules baseline A, not with an adaptive agent: at least 10 points more accurate than A, or at least 20 % fewer tool calls at similar accuracy. Both use the same 7 sources, so in practice this is the accuracy route.
  - **The original §16 comparison** (the adaptive agent against the stronger baseline) is reported as **not met in the pilot: no advantage observed in 13 windows**. It is not re-run and not claimed.
- **What stays the same:** permissions, recovery guarantees, budgets and the remaining evaluation criteria (abstention, false alarms, unsupported findings, invalid-case reruns).

## D49. The UI and how it is tested in a browser

- **Rendering:**
  - Pages are server-rendered with Jinja2 autoescaping.
  - One script file (`/static/app.js`) writes every dynamic value with `textContent`; there are no inline scripts.
  - Every response carries a strict policy: `Content-Security-Policy` (`script-src 'self'`, `frame-ancestors 'none'`, …), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`.
- **The HTML export** is a standalone file with escaped content, inline CSS only, and its own `default-src 'none'` policy. No scripts, nothing loaded from the network.
- **Wording:** the Report screen says "diagnosis supported in the observed window" and never "fixed". Stopped-without-diagnosis, missing sources and no fault observed have distinct banners (§15).
- **Browser automation:**
  - The Claude in Chrome extension was not connected in this session.
  - The real-browser checks therefore use **Playwright driving the installed Google Chrome** (`channel="chrome"`), with no browser download.
  - Playwright's video recording needs its own ffmpeg download, so it was not used; screenshots record each step.

## D50. Numbers in findings are checked against the cited evidence and flagged, not rejected

- **What spec §14 asks:** validation must ensure "accurate tool-calculated arithmetic".
- **The check** (`reports.check_numbers`):
  - Every number in a finding's claim is looked up in the cited observations, rendered exactly as the model saw them, allowing for the precision it is stated with. Evidence handles and clock times are ignored.
  - Numbers not found are listed on the finding (`unverified_numbers`) and shown under it on the Report screen and in the export.
  - Each report carries `checks.numbers_checked` and `checks.numbers_not_found_in_cited_evidence`.
- **Why flag and not reject.** Measured on the 15 real model reports from the pilots: 127 numbers, 8 (6 %) not found literally. These were mostly correct derived values:
  - durations ("about 90 s", read from 10 s buckets);
  - percentages computed from counts (86 of 464 is 18.5 %);
  - a maximum age from a neighbouring sample (115 against 116).
  - One was a parser false positive ("5xx"), since fixed.
  - Rejecting these would trigger repairs and fallbacks for correct statements.
- **What this does not achieve:** it is weaker than "ensure". The flags are for the reviewer, and **Stage 5 reports the flagged rate as a metric.** Derived arithmetic (ratios, durations) is not recomputed.

## D51. Evaluation replay mode: the product answers from frozen evidence bundles

- **The problem:** spec §16 compares systems on the same observations, and D6 requires frozen evidence. Re-querying a historical window later is not freezing: log ingestion, late commits and retention can change what a later query returns.
- **The bundle:** after a window settles, the harness runs the 7 fixed-bundle tools once and writes each envelope to a read-only file, with a `manifest.json` of SHA-256 checksums (`evaluation/stage5.py:freeze_bundle`). Directory names are opaque hashes, so no category is visible in them.
- **Replay** (`src/incidentlens/tools/replay.py`):
  - Turned on only with `INCIDENTLENS_EVALUATION_MODE=1` plus `INCIDENTLENS_REPLAY_BUNDLES` (`scripts/stack.py gateway up --replay <dir>`). The server refuses the setting without the mode flag, and refuses a missing directory.
  - The executor answers each tool action from the bundle whose window equals the job's window exactly. Checksums are verified before every answer.
  - **No live fallback:** a job with no matching bundle, or a tool call not in the bundle, gets an UNAVAILABLE observation.
  - The replayed envelope keeps its recorded times, coverage, `settled` flag and data. It gets a fresh evidence ID (IDs are primary keys) and records the bundle file and checksum in `source`. The prompt renderer never shows `source`.
- **Everything else is the real pipeline:** public API, supervisor, the runner in OpenShell, the real model and report validation. Only the tool answers change.
- **What it does not measure:** elapsed time in replay excludes source collection, so it is labelled *replay elapsed*, not latency (protocol §5.6).
- **Windows are aligned to whole seconds** when recorded, so the job's window and the manifest's millisecond ISO strings match exactly.

## D52. Stage 5 design: protocol, freeze, validity and scoring fixed before the first window

- **`evaluation/protocol.md`** fixes the categories, validity rules, scoring, the gates and the uncertainty method before any development window is recorded.
- **Case set** (`evaluation/cases.py`): 7 categories × 10 templates (5 development, 5 held-out).
  - Held-out templates use durations, intensities, onsets, window lengths, distractors (`load_bump`, `overspend_and_replays`) and handler delays that no development template uses; `check_templates` enforces this.
  - Slow-consumer delays stay below the 3 s delivery timeout: the fixture's auth measured p50 70 ms, p99 79 ms, max 157 ms over 14,864 deliveries, so 2,400 ms leaves room.
  - **Missing evidence:** a real fault, with the source that decides its cause removed for the whole window by stopping the collector (logs and blocker samples) and, for consumer faults only, Prometheus. A lock fault's symptom (transfer errors) comes from Prometheus, so lock templates never stop it.
- **Validity** (`evaluation/validity.py`) is decided from the frozen bundle before any system runs.
  - A consumer-fault stall is a durable backlog (accumulated arrivals minus publications) of at least 40. A plain "some bucket had fewer publications than arrivals" fires on ordinary batch timing in healthy windows, so it was rejected.
  - Lock-and-pool windows need COMPLETE blockers, because the collector keeps sampling through the LedgerCore restart.
  - At most 3 attempts per case-run; failures are counted, never dropped.
- **Scoring keeps D45's ground truth.** For lock contention exhausting the pool, the correct primary cause is lock contention, named directly or as the cause of the pool exhaustion. The extended pilot accepted 2 pool-led reports as correct ("not clean", unblinded); Stage 5 does not. The lenient count is reported beside the strict one, labelled secondary.
- **The product's primary cause on held-out runs** comes from the owner's blind pick of the root cause each report names (8 options), not from the keyword aid that misread a pilot report. The report schema gets no `primary_cause` field, which would hand the model the label set.
- **Abstention:** only INCONCLUSIVE counts as appropriate on missing-evidence runs. TENTATIVE still asserts a diagnosis.
- **Freeze** (`evaluation/freeze.py`):
  - The record holds a SHA-256 manifest of 72 code and config files, plus LedgerCore's working-tree revision, the model ID, the prompt version, the investigation mode and the protocol's checksum.
  - The harness refuses to record or replay on any difference: D46 showed what a mid-run edit does.
  - Development may change P's prompt, gateway and report checks (each recorded). Nothing that produces bundles, scores or validates may change after the held-out freeze.
- **Not done:** the adaptive agent (C) is not re-run. Its pilot result (no advantage, D47) stands.

## D53. Development results led to one recorded prompt revision before the held-out freeze

- **What development showed** (35 of 35 valid on the first attempt; prompt `2026-11-06.1`):
  - **Abstention failed: 0 of 5.** Each missing-evidence report said the cause was not determined, yet was labelled TENTATIVE. The prompt and schema never defined the outcome labels, so the model graded the symptom, not the cause.
  - **Lock-pool reports were pool-led.** Under strict D45, the author's reading was 0 or 1 of 5.
  - **The keyword aid overcounts.** It matches negated mentions ("locking looks healthy"), so the author read all 25 fault reports and records that count separately. The aid is not used on held-out runs.
- **Revision 1 (protocol §8):**
  - The prompt defines the four outcomes by what the evidence shows about the cause, and asks how contributing conditions combine. No category, source or fault is named.
  - The number check accepts a percentage that restates a ratio. Development flags were reviewed one by one: 8 of 24 were this false positive, and the other 16 were correct flags of derived values.
  - Harness repairs:
    - The distractor threads now survive LedgerCore restarts. They had died in lp-d2 and lp-d3.
    - The replay command now checks the freeze, as D52 said it did.
- **The acceptance check was set before the replay, with a limit of two revisions. It was met on the first:**
  - 5 of 5 missing-evidence runs INCONCLUSIVE;
  - 0 false alarms;
  - no fault run turned INCONCLUSIVE.
  - The author's strict count is 24 of 25, with lock-pool at 4 of 5.
- **Known side effect, accepted rather than tuned:**
  - Fault-run labels moved from 15 SUPPORTED out of 25 to 3. The product now calls clear cases TENTATIVE, typically because the reason behind the cause (why the consumer refused, or why it returned 401) is not shown.
  - The protocol does not score SUPPORTED against TENTATIVE.
  - Tuning further on 5 development cases per class would be fitting to development. This is listed as a usefulness limitation.
- **Development windows not re-recorded:** lp-d2 and lp-d3 ran with a shortened distractor. Development is not gated, and §4 validity does not depend on the distractor.

## D54. The held-out run was interrupted by host sleep; how the two affected case-runs are handled

Written 2026-11-06 at about 21:50 UTC, before the run resumed and before any held-out scoring.

- **What happened** (from `pmset -g log`, the controller logs and the IncidentLens database):
  - The Mac was on battery with the lid open. Once the session went idle, nothing held it awake, so it entered idle sleep at **19:41:36 UTC**.
  - It then only woke for short maintenance windows (2–45 s) until about 21:40 UTC.
  - There was no sleep between the run's start (17:04 UTC) and 19:41:36 UTC, so the first 39 case-runs are not affected.
- **sc-h2-r2** (case-run 40):
  - **Its window** (19:38:30–19:41:30) was recorded before the sleep, which began 6 s after the window ended.
  - **The bundle** was frozen at 20:12:59, in a maintenance wake. Its coverage matches the other four slow-consumer bundles: all seven sources COMPLETE.
  - **P failed:** "the runner exited before the job finished; no recovery left after 2". It made 0 model calls and 0 tool calls.
    - The OpenShell runner exited within about 1 s on each of its three starts.
    - The Docker VM's clock was about 15.5 min behind the host's at that point: the job's database timestamps read 19:57, against 20:13 on the host. The VM was still recovering from sleep.
    - The clocks agreed again (within 5 ms) when checked at about 21:45 UTC.
- **cr-h3-r2** (case-run 41): its recording was stretched across about an hour of sleep. The harness was stopped while that attempt was still recording. On resume, the harness marks it `interrupted`, which is not a validity failure, and records attempt 2 with a new seed. No bundle had been frozen for it.
- **Scoring:** protocol §7 forbids changing the scoring rules after the first held-out window.
  - **Primary result:** sc-h2-r2 stays exactly as recorded. P produced no report, so the frozen `score.py` counts it as a miss for slow consumer and for the fault total.
  - **Pre-declared sensitivity:** a second figure, labelled "host-sleep sensitivity", leaves sc-h2-r2 out of P's denominators. That makes slow consumer out of 14 and fault runs out of 74.
  - **Any later case-run** whose span from workload start to P's finish overlaps an `Entering Sleep` entry in `pmset -g log` gets the same treatment.
  - The original state is archived at `evaluation/results/stage5/case-runs/sc-h2-r2/state-as-recorded.json`. It is never edited.
- **Prevention:**
  - The resumed run runs under `caffeinate -i`, which works on battery with the lid open.
  - A guard stops the harness between case-runs, never while P runs, if the battery falls to 15 % or below. The interrupted recording is then re-recorded on the next resume.
  - Conditions are unchanged: most of the first 40 case-runs already ran on battery (unplugged at 17:27 UTC), with Low Power Mode set in both power profiles.
- **Reliability observation for Stage 6** (not changed, because `src/` is frozen): the supervisor used both recoveries within about 5 s because it does not wait between them. A short outage in the environment can use up the whole recovery allowance.

## Observed limitation, not changed

LedgerCore lets any `LEDGER_USER`, including customer logins, read `/actuator/**`. Changing existing access rules is outside the telemetry-only scope, so IncidentLens uses a separate, narrower credential that can reach only `/actuator/prometheus`.
