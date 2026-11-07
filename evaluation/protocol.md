# IncidentLens evaluation protocol (Stage 5)

**Status:** written 2026-11-06 and revised the same day, before the first development window was recorded (the revision tightened §4 and §5.1–5.2; nothing had been run against it). The scoring, validity and freeze rules below are fixed from the first recorded window. The few items marked *frozen at the held-out freeze* are filled in only at that freeze, and once filled they do not change.

**FIXTURE RESULTS throughout:** every case runs against the local LedgerCore, its fixture consumer (D1) and the controller's fault injectors.

## 1. What is evaluated

**Two systems, one recorded evidence bundle per case-run:**

- **P (the product, D48):** the full IncidentLens job pipeline.
  - **Path:** public API, then the supervisor, the runner in an OpenShell sandbox, the gateway and executor, and finally report validation.
  - **What it does:** collects the fixed 7-tool bundle, then makes one model decision. The model is `claude-sonnet-5-5` with the system prompt version recorded in §8.
  - **Answering from the bundle:** in replay mode (D51) the gateway answers every tool action from the case-run's frozen bundle. It never reads live state.
- **A (the rules baseline, D41, D45):** `evaluation/baselines.py:baseline_a`, applied to the same frozen bundle.
  - **Frozen as it stands on 2026-11-06.** A's rules may not be changed for this evaluation, even though P's prompt may be tuned on development cases (rule in §7).

**What is not re-run:**
- The adaptive agent (C) is not part of this evaluation. Its pilot result stands as reported: no advantage observed in 13 windows (D43, D47).
- The original §16 comparison (the agent against the stronger baseline) is reported as not met in the pilot.

## 2. Categories, cases and runs

- **Seven categories** (spec §16, with D45's substitution):

  | Category | Ground-truth primary cause | Injected by the controller |
  |---|---|---|
  | `consumer_unavailable` | consumer refuses connections | fixture consumer closes its listener |
  | `slow_consumer` | slow consumer handler | fixture handler delay (below LedgerCore's 3 s timeout) |
  | `credential_rejection` | consumer rejects the publisher's credential (401) | fixture verifies against a different hash |
  | `lock_contention` | row locks held by a non-`ledger_app` session | owner session holds `FOR UPDATE` locks |
  | `lock_pool_exhaustion` | locks held while the pool is small: joint cause (see §5.2) | LedgerCore restarted with a smaller pool, plus held locks |
  | `healthy` | none | no fault |
  | `missing_evidence` | (a fault occurs, but its deciding source is missing) | a consumer or lock fault, with the collector and/or Prometheus stopped |

- **Templates:** each category has 10, defined in `evaluation/cases.py`: 5 development and 5 held-out.
  - **Held-out templates differ in substance, not only in seed:** fault durations, intensities, onset times, window lengths and distractors that no development template uses (§6).
- **Runs:**
  - **Development:** 35 cases, 1 run each.
  - **Held-out:** 35 cases, 3 runs each, each run in a new recorded window with a new seed. That is 105 held-out case-runs.
- **Neutral symptoms** describe what customers see, never the cause:
  - Consumer and credential categories, plus some healthy and missing-evidence cases: "Customers say that transfer notifications are arriving late."
  - Lock categories, plus some healthy and missing-evidence cases: "Some customers say their transfers are failing or taking several seconds."
  - Each case's symptom is fixed in its template.

## 3. Recording a case-run

1. **Preflight**, which must pass or the window is not recorded:
   - Docker and the OpenShell gateway are connected.
   - LedgerCore is ready on its default pool (10 connections, 30 s timeout).
   - The fixture consumer is in normal mode.
   - The outbox is empty.
   - The collector is running, with a heartbeat under 15 s old.
   - Prometheus is ready.
   - The freeze manifest (§8) matches.
2. **Record:** the controller runs the template (workload, distractor, fault). Ground truth is written only to `evaluation/runs/<case-run>/controller.jsonl`.
3. **Settle:**
   - **Normal cases:** wait until at least 10 s after the window end (D3) *and* a collector heartbeat after the window end shows zero lag for both logs.
   - **Missing-evidence cases:** freeze while the stopped component is still stopped, then restart it.
4. **Freeze the bundle:**
   - Run the 7 fixed-bundle tools once over the window.
   - Write each envelope to `evaluation/results/stage5/bundles/<case-run>/` with `manifest.json` (window, case, template, SHA-256 per file, freeze time).
   - Make the files read-only.
5. **Validity** (§4), decided from the bundle alone, before any system runs.
6. **Run A and P** on the bundle, in an order randomised per case-run. Checksums are verified before each.

## 4. Validity (decided from the frozen bundle, never from a system's answer)

A case-run is **valid** only when its category's rule holds. "Logs" means the `search_delivery_logs` envelope; "blockers" means the historical `get_database_blockers` envelope.

| Category | Valid when |
|---|---|
| `consumer_unavailable` | logs COMPLETE; `network_error` ≥ 5 and at least half of failed deliveries |
| `slow_consumer` | logs and timings COMPLETE; consumer handler max ≥ 80 % of the template's delay; no delivery failures |
| `credential_rejection` | logs COMPLETE; `auth_error` ≥ 5 and at least half of failed deliveries |
| `lock_contention` | blockers COMPLETE; at least 1 edge with blocker role `other`; blocking in at least 2 samples |
| `lock_pool_exhaustion` | blockers COMPLETE (the collector keeps sampling through the LedgerCore restart; only Prometheus sources may show the restart gap); at least 1 `other` edge; pool acquisition timeouts ≥ 1 |
| `healthy` | all sources COMPLETE; pending max ≤ 40; no delivery failures; transfer server-error ratio 0; no pool timeouts; no `other` blocker edges |
| `missing_evidence` | the template's deciding source (logs or blockers) is not COMPLETE **and** the symptom is visible in a source that remains: for consumer faults, a durable backlog (arrivals minus publications, accumulated over the window's buckets) of at least 40; for lock faults, request metrics available with a transfer server-error ratio above 0. Lock-fault templates never stop Prometheus |

**Invalid case-runs** are recorded with the failing rule, re-recorded in a new window with a new seed, and counted in the results. At most 3 attempts are made per case-run; a case-run with no valid attempt is reported as such and left out of the rate denominators. A run is never discarded or rerun because of a system's answer.

## 5. Scoring

### 5.1 Primary cause (fault categories)

- **For A:** its `primary_cause` label, compared with the ground truth.
- **For P:** the cause the report names as primary.
  - **Development:** classified by the keyword aid (`classify_report`) and read by the author. These are labelled development-only and unblinded.
  - **Held-out:** decided by the **owner's blind pick** on the review sheet. The owner sees the report's summary and findings only, without the case, category, ground truth or system, and picks the **root cause the report names**: (1) consumer refused connections, (2) slow consumer handler, (3) credential rejection (401), (4) lock contention, (5) lock contention exhausting the connection pool, (6) the connection pool (its exhaustion, size or timeout), not attributed to lock contention, (7) other cause, (8) no cause named. The sheet tells the owner to pick the cause named as the root, not every cause mentioned; a condition called only a possible or likely contributor is not the root cause.
  - **Mapping:** picks 1–3 match their categories; picks 4 and 5 match `lock_contention` and `lock_pool_exhaustion`; pick 6 matches no category.
  - **If the owner declines the pick:** the author's judgement is reported instead, labelled unblinded.
  - **Incomplete picks:** unless every held-out report has a valid pick (owner's, or the author's labelled unblinded), P's primary cause, lenient count and causal depth are reported as not scored. The keyword aid is never used on held-out runs.
- **No label in the report:** the report schema gets no `primary_cause` field, so the model is never handed the label set.

### 5.2 The joint-cause class (`lock_pool_exhaustion`)

- **Two injected changes, both necessary:** the pool reduction and the held locks (D45).
- **Correct primary cause (D45, unchanged):** the report names *lock contention* as the root cause, either as primary or as the cause of the pool exhaustion (picks 4 or 5). A report that names only the pool, or leads with the pool and does not attribute it to the locks (pick 6), is **incorrect**.
- **Causal depth is earned only** when the report links the two: the held locks or blocked transactions holding connections, which exhausted the small pool (pick 5).
- **Not carried over from the pilot:** the extended pilot scored 2 pool-led reports as correct, flagged as "not clean" and unblinded. Stage 5 does not. The lenient count (picks 4, 5 or 6) is reported beside the strict one, labelled secondary.
- **Reporting:** causal depth is reported separately for this class.

### 5.3 Abstention (`missing_evidence`)

- **Appropriate:** outcome **INCONCLUSIVE**.
- **Inappropriate:** **TENTATIVE**, SUPPORTED or NO_FAULT_OBSERVED. TENTATIVE does not count, because it still asserts a diagnosis.

### 5.4 False alarms (`healthy`)

| Outcome | Counted as |
|---|---|
| **SUPPORTED or TENTATIVE** | false alarm |
| **NO_FAULT_OBSERVED** | correct |
| **INCONCLUSIVE** | neither; reported separately |

### 5.5 Citations and unsupported findings (P)

- **Valid references:** every factual finding must cite evidence of its own job. The report checks enforce this (D30); its rate is reported.
- **Unsupported findings:**
  - **Sample:** 100 findings drawn uniformly at random, with a seed recorded in the sheet, from all held-out P findings.
  - **Judgement:** each is shown with only the rendered text of the observations it cites. The owner marks *supported*, *unsupported* or *unclear* against the rubric below.
  - **The rate:** unsupported ÷ (supported + unsupported). Unclear judgements are reported separately.
- **The rubric:** a finding is *supported* when the cited observations show what it claims, at the claimed strength.
  - An *observed* finding needs the fact itself in the cited text.
  - An *inferred* finding needs its premises there, with the inference stated as such.
  - A finding is *unsupported* when the cited observations do not show it, contradict it, or show only a weaker claim.
- **Numbers:** the D50 flagged-number rate (numbers in claims not found in the cited evidence) is reported alongside.

### 5.6 Other measures

- **Elapsed time:** labelled *replay elapsed*, which excludes source collection. It is not live latency.
- **Calls:** model and tool calls.
- **Cost:** estimated cost from the 2026-11-04 pricing file.
- **Breakdowns:** per-class outcomes, and invalid-case counts with reasons.

## 6. Distractors (plainly benign by construction)

- `none`.
- `overspend`: customer transfers that exceed the balance (HTTP 422), about 0.3/s.
- `replays`: repeated requests with an already-used idempotency key, about 0.3/s.
- `load_bump`: the workload rate rises to at most 3.8/s, under 50 % of the measured 8.03/s capacity, for 30 to 60 s.

**Excluded:** a short lock that blocks `ledger_app` in 2 or more samples is a fault, not a distractor, so it is not used as one.

## 7. What may change after development

- **May be changed, with each change recorded in §8:** P's system prompt, the gateway or report checks, after development results.
- **Re-checking after such a change:** development bundles are replayed for P; this costs only model calls, with no re-recording.
- **May not change after the first held-out window is recorded:**
  - the tool, renderer and collector code that produces bundles;
  - A's rules;
  - the case templates;
  - this protocol's scoring and validity rules.

## 8. Freeze record

- **Development freeze:** `evaluation/freeze-dev.json`, a SHA-256 manifest of `src/`, `evaluation/` (excluding results and runs), `migrations/` and `deploy/` (excluding local), plus LedgerCore's working-tree revision, the model ID and the prompt version.
- **Held-out freeze:** `evaluation/freeze-heldout.json`, written before the first held-out window. The harness refuses to record or replay when the current manifest differs from the active freeze.
- **Prompt and gateway changes after development** (*frozen at the held-out freeze*):
  - **Revision 1 (prompt `2026-11-06.2`), after the 35 development case-runs:**
    - **Why:**
      - **Abstention was 0/5.** Every missing-evidence report said the cause was not determined but was labelled TENTATIVE. Neither the prompt nor the schema defined the outcome labels.
      - **Lock-pool reports led with the pool change.** The author read about 0 or 1 of the 5 as correct under strict D45. They named the pool change first and listed the blocking session beside it.
    - **What changed:**
      - The system prompt defines the four outcomes by what the evidence shows about the cause, not the symptom.
      - It also asks the model to say how contributing conditions combine, instead of listing them side by side. The wording names no category, source or fault.
      - The §14 number check (D50) accepts a percentage that restates a ratio in the evidence, so 8.1 % matches 0.0808. This false positive caused 8 of the 24 development flags.
      - The background-traffic threads (§6) now continue through a LedgerCore restart. In lp-d2 and lp-d3 they had stopped at the restart.
      - `score.py` can score a development replay (`--prompt-version`).
      - **Held-out scoring, fixed before the held-out freeze:** primary cause is not scored without a complete set of picks (before this fix, `score.py` fell back to the keyword aid). Pick 6 is reworded to match §5.2, and the sheet now says to pick the root cause.
      - `stage5.py replay` now checks the freeze, as this section already required (only recording had checked it before), and logs one line for each replay.
    - **Acceptance check, set before the replay:**
      - **Scope:** P is replayed on all 35 development bundles.
      - **Accepted if all three hold:**
        - at least 4 of 5 missing-evidence runs are INCONCLUSIVE;
        - no healthy run is SUPPORTED or TENTATIVE;
        - at most 1 fault run turns INCONCLUSIVE among those whose cause the first run named correctly (author's reading).
      - **Not gated:** lock-pool causal depth is reported from the author's reading.
    - **Limit:** at most two revisions. If the second also misses, the held-out freeze is written anyway and the abstention gate is reported as it falls.
    - **Result: accepted, so there is no second revision.**
      - **Missing evidence:** 5 of 5 INCONCLUSIVE.
      - **Healthy:** 5 of 5 NO_FAULT_OBSERVED.
      - **Fault runs:** none turned INCONCLUSIVE.
      - **Side effect, not gated:** fault-run labels shifted toward TENTATIVE. Before the change, 15 of 25 were SUPPORTED; after it, 3 of 25 were.
      - **Record:** `evaluation/results/stage5/replay-development-2026-11-06.2-*.log` and `summary-development-2026-11-06.2.json`.

## 9. Uncertainty

- **Method:** 95 % intervals by bootstrap over the 35 held-out cases (10,000 resamples). Each case's 3 runs are resampled together, as one cluster.
- **Expected width:** with 25 held-out fault cases the interval on the 80 % primary-cause gate will be wide. It is reported next to every rate.

## 10. Gates (spec §16; D48)

- **Absolute gates for P:**
  - at least 80 % correct primary causes on valid fault runs;
  - at least 80 % appropriate abstention on valid missing-evidence runs;
  - at most 10 % false alarms on valid healthy runs;
  - every factual finding cites valid evidence;
  - at most 5 % unsupported findings on the blinded sample.
- **Trade-off clause:** P at least 10 points more accurate than A, or at least 20 % fewer tool calls at similar accuracy. Both use 7 tools, so in practice this is the accuracy route.
- **Every gate is reported pass or fail, with counts and intervals.**
