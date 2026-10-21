# atomic-admission

**Stops agent overspend at the meter: the party that meters a call debits prepaid credit in the same transaction that admits it.**

[![falsification-harness](https://github.com/FetchDoge/atomic-admission/actions/workflows/ci.yml/badge.svg)](https://github.com/FetchDoge/atomic-admission/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](.github/workflows/ci.yml)

At zero credit the call never runs, so nothing becomes billable, however many IPs, retries or credentials the agent throws at it. `atomic_admission.py` provides the reusable admission engine, while `harness.py` holds the mechanism's falsification test suite. It uses only the Python standard library (`sqlite3` in WAL mode, `threading`, `subprocess`).

## What it guarantees

Each admission runs as one `BEGIN IMMEDIATE` transaction that enforces four invariants:

1. **Atomic admission (K1).** The check, the debit and the reservation write commit together or not at all. A job either holds a durable reservation or it never started. There is no third state, even across `kill -9`.
2. **Idempotent admission (K2).** Every admission carries an idempotency key, and a replay returns the original verdict. A retry never double-charges or double-refunds.
3. **Hard cap (K3).** Total liability (reserved plus settled) never exceeds the cap, and the balance never drops below zero.
4. **Reconciled settlement & credit refund.** When an admitted call settles with actual token/compute usage (`actual_amount <= reserved_amount`), unspent credit is returned to the balance in the same atomic transaction. Over-settlement attempts (`actual > reserved`) fail closed with `ValueError`. Pre-execution cancellation (`release()`) refunds 100% of the reserved credit.

The admission path **fails closed**: any exception inside the transaction rolls it back and returns *rejected* (`admit()` in `atomic_admission.py`).

## Quickstart

### Using the engine in code

```python
from atomic_admission import AtomicMeter

# Initialize local SQLite WAL meter with $100.00 initial prepaid cap
meter = AtomicMeter("ledger.db", initial_cap=100.0)

# Atomically reserve worst-case estimate ($5.00) at admission
if meter.admit("call-req-001", amount=5.0):
    try:
        # Execute agent tool / LLM call...
        actual_cost = 1.85  # e.g. 185 tokens consumed
        meter.settle("call-req-001", actual_amount=actual_cost)  # refunds $3.15 to balance
    except Exception:
        meter.release("call-req-001")  # abort: refunds 100% ($5.00) back to balance
```

### Running tests and falsification

```sh
git clone https://github.com/FetchDoge/atomic-admission && cd atomic-admission
python3 -m unittest discover tests -v  # 11 unit tests covering K1-K3, refunds, and fail-closed gates
python3 harness.py --quick all         # CI scale falsification run (Phase A & B)
python3 harness.py all                 # full 10,000-admission falsification run (concurrency + SIGKILL)
```

The harness states its kill conditions up front and exits nonzero if any of them fires:

| Kill condition | Test |
|---|---|
| K1: a job starts without a durable reservation, or the ledger is inconsistent after `kill -9` | 8 hammer processes, SIGKILL mid-burst. Afterwards: balance drift 0, zero orphan starts, zero duplicate keys |
| K2: a duplicate retry creates duplicate liability | 10,000 admissions across 100 workers at a 50% replay rate. Each replay verdict must equal the original, with zero mismatches |
| K3: liability ever exceeds the $100 cap | Continuous balance sampling under full concurrency with partial refunds. Charged total equals the cap exactly, and the floor is never breached |

## How it fails

| Condition | Behaviour |
|---|---|
| Balance below the call's cost | Records a `rejected` reservation and returns *rejected*. The call never starts |
| Replay of a known idempotency key | Returns the stored verdict without touching the balance. A rejected key stays rejected; a released key stays released |
| Settlement exceeds reserved ceiling (`actual > reserved`) | Fails closed with `ValueError`. Transaction rolls back; balance and reservation remain untouched |
| Pre-execution cancellation (`release()`) | Refunds 100% of reserved credit to balance, marks state as `released` |
| Any error inside the admission transaction | Rolls back and returns *rejected* (fail-closed) |
| Process killed mid-transaction | SQLite WAL recovery discards the uncommitted transaction. The harness's Phase B checks this: drift 0, orphans 0 |
| `settle()` on a key that isn't `reserved` | No-op. Only a reserved row moves to `settled` |

**Limits:** this is a reference meter engine designed for gateway integration. It assumes one SQLite ledger per tenant with a single balance. It has no external payment rail integration (e.g., Stripe/PayPal webhooks); internal settlement reconciliation refunds unspent reservations atomically.

## Evidence

- **Unit test suite, 2026-10-21:** `python3 -m unittest discover tests -v` — 11 passed in 0.05s. Covers debiting, replay idempotency, insufficient balance rejection, full settlement, partial settlement credit refund, duplicate settle idempotency, over-settlement rejection, and cancellation release.
- **Full falsification run, 2026-10-21:** `python3 harness.py all` exited 0 locally. 100 threads, 10,000 admissions, concurrent partial settlement, over-settle attack probes, and 8-process hammer with `kill -9` mid-burst. K1, K2, K3 ALL PASS (drift=0.000000, 0 orphans, 0 mismatches).
- **CI:** the `falsification-harness` workflow runs unit tests and `python3 harness.py --quick all` on every push and pull request.

## Why this exists

The common shape: **cost is created when the call is accepted, not when the invoice settles.** Incidents reported in September–October 2026 motivate it: an agent that spent $5,000 via 36 untraceable IPs and then argued it did nothing wrong; a runaway accounting-agent loop (15,000+ API calls, ~$50K in under an hour, per Mandiant); 10–50× agentic billing shocks. Blocking settlement after the fact leaves the customer owing the money. Declining a checkout the agent never visits changes nothing.

The enforcement point moves to where liability is *created*: admission.

## What this is not

- Not a vendor integration. Deployment requires the meter-holder's cooperation — the admission check must live where the call is metered. The engine provides the core primitive; gateway middleware is an adapter around it.
- Not a receipt or proof layer. This sells prevention, not evidence. (A sibling thesis — subpoena-ready agent receipts — was killed on trust-root grounds; this shape was designed to survive exactly that objection.)
- Not a judgment layer. It bounds authority; it does not decide whether the spend was wise.

## Origin

Built 2026-10-06 after a scout of X/Reddit/news (subpoena week, Oct 2026) surfaced five real-world holes, and a two-team adversarial review (two independent AI-model reviewers, held to a high bar) killed the receipt-based and rail-based product theses. Both reviewers independently converged on vendor-side atomic admission as the surviving shape; the harness is its falsifier, and the shape survived.

## Status

Mechanism proven by unit tests and falsifier. Packaged as `atomic_admission.py` for direct embedding into agent gateway runtimes.

Built by Marcus Richards, directing AI coding agents.

## License

Copyright 2026 Marcus Richards.
Licensed under the Apache License 2.0. See LICENSE.
