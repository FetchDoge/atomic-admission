# atomic-admission

**Stops agent overspend at the meter: the party that meters a call debits prepaid credit in the same transaction that admits it.**

[![falsification-harness](https://github.com/FetchDoge/atomic-admission/actions/workflows/ci.yml/badge.svg)](https://github.com/FetchDoge/atomic-admission/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](.github/workflows/ci.yml)

At zero credit the call never runs, so nothing becomes billable, however many IPs, retries or credentials the agent throws at it. `harness.py` holds the mechanism and the falsification run that tries to break it. It uses only the standard library (`sqlite3` in WAL mode, `threading`, `subprocess`).

## What it guarantees

Each admission runs as one `BEGIN IMMEDIATE` transaction that enforces three invariants:

1. **Atomic admission (K1).** The check, the debit and the reservation write commit together or not at all. A job either holds a durable reservation or it never started. There is no third state, even across `kill -9`.
2. **Idempotent admission (K2).** Every admission carries an idempotency key, and a replay returns the original verdict. A retry never double-charges.
3. **Hard cap (K3).** Total liability (reserved plus settled) never exceeds the cap, and the balance never drops below zero.

The admission path **fails closed**: any exception inside the transaction rolls it back and returns *rejected* (`admit()` in `harness.py`).

## Quickstart

```sh
git clone https://github.com/FetchDoge/atomic-admission && cd atomic-admission
python3 harness.py --quick all  # CI scale, about a second
python3 harness.py all          # full falsification run, about 5 minutes
```

The harness states its kill conditions up front and exits nonzero if any of them fires:

| Kill condition | Test |
|---|---|
| K1: a job starts without a durable reservation, or the ledger is inconsistent after `kill -9` | 8 hammer processes, SIGKILL mid-burst. Afterwards: balance drift 0, zero orphan starts, zero duplicate keys |
| K2: a duplicate retry creates duplicate liability | 10,000 admissions across 100 workers at a 50% replay rate. Each replay verdict must equal the original, with zero mismatches |
| K3: liability ever exceeds the $100 cap | Continuous balance sampling under full concurrency. Charged total equals the cap exactly, and the floor is never breached |

## How it fails

| Condition | Behaviour |
|---|---|
| Balance below the call's cost | Records a `rejected` reservation and returns *rejected*. The call never starts |
| Replay of a known idempotency key | Returns the stored verdict without touching the balance. A rejected key stays rejected |
| Any error inside the admission transaction | Rolls back and returns *rejected* (fail-closed) |
| Process killed mid-transaction | SQLite WAL recovery discards the uncommitted transaction. The harness's Phase B checks this: drift 0, orphans 0 |
| `settle()` on a key that isn't `reserved` | No-op. Only a reserved row moves to `settled` |

**Limits:** this is a reference mechanism inside a falsification harness, not a packaged library. It assumes one SQLite ledger with a single balance. It has no top-up or refund path, and settlement never returns credit.

## Evidence

- **Full run, 2026-10-06** ([REPORT.md](REPORT.md)): ALL PASS. 100 admitted and 9,900 rejected at exactly $100.00, with 0 idempotency mismatches and 0 drift after `kill -9`.
- **Quick run, 2026-10-07:** `python3 harness.py --quick all` exited 0 locally. 100 admitted and 400 rejected, K1/K2/K3 PASS.
- **CI:** the `falsification-harness` workflow runs `python3 harness.py --quick all` on every push and pull request.

## Why this exists

The common shape: **cost is created when the call is accepted, not when the invoice settles.** Incidents reported in September–October 2026 motivate it: an agent that spent $5,000 via 36 untraceable IPs and then argued it did nothing wrong; a runaway accounting-agent loop (15,000+ API calls, ~$50K in under an hour, per Mandiant); 10–50× agentic billing shocks. Blocking settlement after the fact leaves the customer owing the money. Declining a checkout the agent never visits changes nothing.

The enforcement point moves to where liability is *created*: admission.

## What this is not

- Not a vendor integration. Deployment requires the meter-holder's cooperation — the admission check must live where the call is metered. The harness proves the *mechanism*; the deployment is a separate negotiation.
- Not a receipt or proof layer. This sells prevention, not evidence. (A sibling thesis — subpoena-ready agent receipts — was killed on trust-root grounds; this shape was designed to survive exactly that objection.)
- Not a judgment layer. It bounds authority; it does not decide whether the spend was wise.

## Origin

Built 2026-10-06 after a scout of X/Reddit/news (subpoena week, Oct 2026) surfaced five real-world holes, and a two-team adversarial review (Perplexity on Sol 6.1 + Grok adversary, held to a high bar) killed the receipt-based and rail-based product theses. Both bots independently converged on vendor-side atomic admission as the surviving shape; the harness is its falsifier, and the shape survived.

## Status

Mechanism proven by its falsifier. It isn't deployed anywhere, because deployment needs a meter-holder to host the admission check.

## License

MIT. See [LICENSE](LICENSE).
