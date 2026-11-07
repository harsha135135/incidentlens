# Stage 3 pilot report

**FIXTURE RESULTS.** Live recorded-window pilot, 2026-11-06. This is the early-stop check (decisions D13), not the evaluation, so no usefulness claim is made from it.

## What ran

- **Windows:** 6 recorded windows, 2 per scenario (healthy, unavailable consumer, slow consumer), in two passes with different seeds (`pilot-20261106T093932Z/`, `pilot-20261106T095030Z/`).
- **Window shape:** 150 s at the calibrated healthy rate of 3.2 transfers/s. The fault ran from +30 s to +120 s. The slow consumer added 400 ms of handler time, below LedgerCore's 3 s delivery timeout.
- **Blind to the label:** every system got the same neutral symptom, "Customers say that transfer notifications are arriving late." The fault label existed only in the controller's files.
- **Systems,** run in a seeded random order per window (D41):
  - **A:** fixed rules over the fixed bundle.
  - **B:** one model call over the same bundle, with the same renderer.
  - **C:** the agent. It used the real provider (`claude-sonnet-5-5`), with the runner inside an OpenShell sandbox, through the public API.
- **The fixed bundle:** request metrics (transfers), outbox stats, delivery logs (any, 100), delivery timings.

**These were the first real-provider, real-sandbox end-to-end runs (§17).** All 6 C jobs completed with a model-written report: no fallback, no repair, no recovery.

## Primary cause, read manually from every report

| Scenario | Ground truth | A | B | C |
|---|---|---|---|---|
| healthy | none | 2/2 | 2/2 | 2/2 |
| unavailable consumer | consumer refused connections | 2/2 | 2/2 | 2/2 |
| slow consumer | consumer handler time | **0/2** (no rule matched) | 2/2 | 2/2 |
| **total** | | **4/6** | **6/6** | **6/6** |

- **A's misses:** the slow period ended 30 s before the window did, and the backlog then drained. Over the whole window, A's rules saw neither a 15 % throughput shortfall nor net pending growth.
- **A's thresholds are left unchanged.** Changing them after seeing the results would tune A on the pilot. Any change must be justified and frozen in `evaluation/protocol.md` before Stage 5.
- **Classifier misread:** the keyword aid labelled one correct B report as "other". It read only the summary and two findings, and "refused connections" was in the third. Automatic classification is not used for the evaluation (D41).

**Reasoning quality (C):**
- It separated observed facts from inferred mechanisms; the slow-consumer link was marked *inferred*, once with outcome TENTATIVE.
- It weighed alternatives and named limitations, such as the short window and that only some delivery rows were listed.
- Fault durations it reported matched the controller: "about 90 s" for a 90 s fault.

## Cost, calls and time

| | B (one call) | C (agent) |
|---|---|---|
| Mean estimated cost per run (pricing file 2026-11-04) | **$0.0173** | **$0.0404** (range $0.031–0.050) |
| Model calls | 1 | 4 on average (3–5) |
| Tool calls | 4 (fixed) | 5 on average (4–6) |
| Input / output tokens per run | about 3,300 / 900 | 9,900–18,200 / 1,140–1,370 |
| Elapsed | about 9–11 s for the call (plus bundle collection) | 18–30 s end to end, including the sandbox |

- **Estimate for the full suite:** about $8.50. That is roughly 105 held-out B and C runs at these rates plus development runs, assuming similar evidence sizes.

## Early stop: C shows no advantage on these faults

- **Accuracy:** C was no more accurate than B (6/6 each).
- **Efficiency:** C used more tool calls (5 against 4) and cost about 2.3 times as much.
- **Against the spec's gate:** §16 asks for at least 10 points more accuracy than the stronger baseline, or at least 20 % fewer tool calls at similar accuracy. The pilot shows neither.

**What the failure cases show:**

1. **The fixed bundle already holds everything these two faults need.** Both are outbox and consumer faults, and B sees all four tools' results in one call. Adaptive tool choice cannot win when the fixed bundle is already complete and small.
2. **C wastes calls it cannot know are useless.**
   - In 4 of 6 runs it requested request metrics for the in-process `consumer` route, which has no traffic, because LedgerCore delivers to an external consumer here.
   - It only learns this from that tool's limitation, after the call.
   - The runtime configuration tool, which would tell it the consumer target, is a Stage 4 tool.
3. **C's per-call cost is dominated by re-reading the growing evidence:** about 3,000–4,200 input tokens per model call, times 3–5 calls.

**What the pilot cannot answer:** whether C gains when the fixed bundle is insufficient or wasteful. That happens when the cause lies outside the outbox and consumer path (pool saturation, lock contention, credential rejection), where the decisive source is a different tool for each fault class. Spec §16 builds its efficiency comparison on exactly that.

## Raw files

- **Per run:** `pilot-*/run-*.json`. Each holds the window, the system order, the bundle coverage, and every system's full report, usage and cost.
- **Logs:** `pilot-pass1-*.log`, `pilot-pass2-*.log`.
- **Ground truth:** the controller's records in `evaluation/runs/pilot-*/controller.jsonl`.

## Extended pilot (decisions D44, D45, D46)

**What changed from the first pilot:**
- **Faults:** the decisive source now differs by class.
- **Bundle and tools:** B's bundle was fixed at all 7 tools and C was offered the same 7. A's new rules were frozen in code before any run.
- **Windows:** 7, in `pilot-20261106T104730Z/`, from seed 20261109: healthy ×1, lock contention ×2, lock contention exhausting a smaller pool ×2, credential rejection ×2.
- **Symptoms,** frozen in D45: the lock scenarios shared one ("transfers failing or slow"); the others used "notifications late".
- **The first attempt was discarded** (D46): it crashed before recording any run, because the code was edited mid-run. The rerun used a fixed gateway.

**Validity:** all 7 C jobs completed with model reports. Across 42 real model calls: 0 malformed replies, 0 errors, 0 repairs, 0 recoveries, 0 fallbacks.

### Primary cause, read manually from every report

| Scenario | Ground truth | A | B | C |
|---|---|---|---|---|
| healthy | none | 1/1 | 1/1 | 1/1 |
| lock contention | lock held by a non-ledger_app session | 2/2 | 2/2 | 2/2 |
| lock contention + small pool | locks, which exhausted the pool | 2/2 | 2/2 | 2/2 |
| credential rejection | consumer returned 401 | 2/2 | 2/2 | 2/2 |
| **total** | | **7/7** | **7/7** | **7/7** |

**How to read that total (corrected after review):**
- **Unambiguous: 5/5 for all three systems** (healthy, plain lock contention, credential rejection).
- **The 2 lock-plus-small-pool windows are not clean 2/2s.** B and C each found both parts (the locks and the pool exhaustion), but each *led with* the pool shrink. I scored them as an unblinded reviewer. The scoring is the same for B and C, so the comparison does not move.
- **A's 7/7 needs a caveat.** Its lock and pool rules were frozen before the runs, but written after fault validation had shown exactly which signals these injectors produce. They are rules designed with knowledge of the faults, which is why even A reached the ceiling.
- **Accuracy could not discriminate.** These faults are easy enough that every system hit the ceiling, so the pilots say nothing about whether C is more accurate on hard cases.
- **Not yet tested at all:**
  - abstention (no missing-evidence window has run);
  - false alarms beyond 3 healthy windows;
  - §16's variations (distracting benign messages, overlapping symptoms).

**The two lock scenarios:**
- **Plain lock contention:** B and C both said the pool was healthy and pointed to an idle-in-transaction session blocking `ledger_app`. Both stayed cautious, with outcome TENTATIVE and the link to the 500s marked inferred.
- **Locks plus a small pool:** both found both parts: the restart that shrank the pool from 10 to 2 connections, 127 acquisition timeouts, and the lock holder.
  - **Causal depth:** C stated the link ("tying up the scarce pool connections"); B listed the pool and the locks side by side.
  - **What both led with:** the pool change, which is a real part of the injected change.
  - **Scoring:** primary cause correct for both, causal depth slightly better for C. This is a reviewer's reading, not a deterministic label.

### Cost, calls and time

| | B (one call, 7 tools) | C (agent, 7 tools offered) |
|---|---|---|
| Tool calls | 7 (fixed) | **7.0 on average** (6, 6, 6, 7, 7, 8, 9) |
| Model calls | 1 | 6.0 on average (up to 8, the whole budget) |
| Mean estimated cost per run | **$0.018** | **$0.064** (range $0.053–0.086) |
| Elapsed | about 10 s for the call (plus bundle collection) | 28–36 s |

**Against the criterion frozen in D44:** C needed 5.6 or fewer tool calls at matched accuracy, or 10 points more accuracy. It managed neither: 7.0 calls, equal accuracy.

**Why, from the action logs:** C does not stop once a cause is supported; it checks the remaining sources to rule out alternatives.
- In a credential-rejection run it found the 401s by its fourth tool call, then still requested runtime config, the idle in-process consumer route (after the config showed an external consumer), delivery timings, pool metrics and blockers: 9 tool calls and all 8 model calls.
- That thoroughness yields careful reports with alternatives weighed. But it costs more than collecting everything once, which is exactly what B does.

## Both pilots together

- **Windows:** 13, across 6 scenario types.
- **Accuracy (manual reading):** A 11/13, B 13/13, C 13/13. Two of those windows had B and C leading with the pool rather than the locks.
- **Calls and cost:** C never used fewer tool calls than B's bundle. It cost 2.3 times B in the first pilot and 3.6 times in the extended pilot.
- **No usefulness claim** is made from 13 windows. What can be said is that no advantage of the agent was observed, while B was as accurate at a fraction of the cost.
