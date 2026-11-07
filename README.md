# IncidentLens

IncidentLens investigates a symptom in a local LedgerCore service. It uses a fixed set of read-only diagnostic tools and writes a report in which every claim cites saved evidence. The design is in [`docs/TECHNICAL-DESIGN.md`](docs/TECHNICAL-DESIGN.md).

**Status:** in progress. Stages 0–4 have passed their exit gates, with the limits recorded in `TODO.md` (for example, pool saturation cannot be produced on its own without hooks, D45). Stage 5's held-out evaluation has been recorded (105 of 105 case-runs valid) and is waiting for the owner's blinded review before it is scored. Stage 6 (release) has not started. Each stage's exit gate, the commands run and their outcomes are in [`TODO.md`](TODO.md). Conflicts between the specification and the real repositories are resolved in [`docs/decisions.md`](docs/decisions.md).

Two results are reported separately, and the first does not imply the second:
- **Reliability:** is it dependable software?
- **Usefulness:** does it actually help diagnose faults?

Neither has a final result yet. The sections below stay empty until the held-out evaluation is scored; development and pilot numbers, with their limits, are in `TODO.md`.

## What runs where

| Process | Port (loopback only) | Notes |
|---|---|---|
| IncidentLens PostgreSQL 17 | 55433 | Jobs, evidence and collected telemetry; durability on |
| Prometheus v3.15.1 | 19090 | Scrapes LedgerCore every 5 s, keeps 24 h |
| LedgerCore (instrumented) | 18080 | Diagnostic records enabled by `scripts/stack.py` |
| LedgerCore PostgreSQL 17 | 55432 | IncidentLens reads it only as `ledger_diag_reader` (read-only, 2 s timeout) |
| Fixture consumer | 18090 events, 18091 control | **Test fixture** for consumer faults; its results are labelled fixture results |
| Collector | none | Blocker samples every 2 s, log tailing, heartbeats |

## Setup from a fresh checkout

**Requirements:**
- macOS on Apple Silicon or Linux.
- Docker (Docker Desktop on macOS).
- [uv](https://docs.astral.sh/uv/).
- JDK 21 or later.
- A LedgerCore checkout beside this folder (`../ledgercore`), or set `LEDGERCORE_DIR`.

```sh
uv sync                                         # Python 3.12 and the locked packages
uv run python scripts/setup_local.py            # generates credentials into deploy/local/ (ignored by Git)
uv run python scripts/stack.py up               # databases, Prometheus, fixture consumer, LedgerCore, collector
PYTHONPATH=src uv run python evaluation/calibrate.py   # measures capacity and fixes the healthy workload rate
```

- **What `stack.py up` does, in order:** it starts the containers, applies IncidentLens's migrations, builds and starts LedgerCore (Flyway applies migration V6), provisions the read-only diagnostics login, then starts the collector.
- **Stop:** `uv run python scripts/stack.py down`. Add `--volumes` to delete the databases too.
- **Model calls (from Stage 3):** they need `ANTHROPIC_API_KEY` in `.env.local`. That file is ignored by Git; keep it readable only by you.

## Tests

```sh
uv run pytest                     # unit tests, plus integration tests against the real IncidentLens PostgreSQL
uv run pytest tests/stack -v      # live scenarios, several minutes, faults injected by the controller
```

**Live scenarios:** they compare what the tools report with the evaluation controller's ground truth, for:
- a healthy load;
- an unavailable consumer;
- a slow consumer;
- an induced telemetry gap;
- LedgerCore's original in-process consumer.

Raw results are written to `evaluation/results/`.

**LedgerCore's own suite**, including the diagnostics tests, runs with `./mvnw verify` in the LedgerCore folder.

## Reliability results

Pending: the held-out evaluation is recorded and waits for scoring (Stage 5).

## Usefulness results

Pending: the held-out evaluation is recorded and waits for the owner's blinded review (Stage 5).
