# atomic-admission

**Thesis:** Agent overspend is stopped at the meter, not at the payment rail.
The party that meters the call atomically decrements a non-negative credit in the *same step* that admits it.
At zero, the call never runs. So nothing is billable, no matter how many IPs, retries, or credentials the agent throws at it.

## Why this exists

The common shape: **cost is created when the call is accepted, not when the invoice settles.** September–October 2026 produced the incidents that motivate it: an agent that spent $5,000 via 36 untraceable IPs and then argued it did nothing wrong; a runaway accounting-agent loop (15,000+ API calls, ~$50K in under an hour, per Mandiant); 10–50× agentic billing shocks. Blocking settlement after the fact leaves the customer owing the money. Declining a checkout the agent never visits changes nothing.

The enforcement point moves to where liability is *created*: admission.

## The mechanism

Three invariants, enforced by one atomic transaction per admission.

1. **Atomic admission (K1).** Check balance, decrement, and write the reservation in a single `BEGIN IMMEDIATE` transaction. A job either holds a durable reservation or it never started. There is no third state, including across `kill -9`.
2. **Idempotent admission (K2).** Every admission carries an idempotency key. Replays return the original verdict. A retry can never double-charge.
3. **Hard cap (K3).** Total liability (reserved-but-unsettled + settled) can never exceed the cap. The balance floor is zero, sampled continuously under concurrency.

Marcus Richards: draft only. Every rewritten sentence is a reframe of a claim already in the supplied README — VERIFIED by direct comparison; no new metric, guarantee, or incident. "The common shape:" is the source's frame, kept; the rejected verdict sentence is absent. "Adversarial infrastructure" omitted — INFERRED that inserting the spine definition would add a claim the source does not make. File the wording judgment in Decisions; file the patch in Grok Build only if you approve the push.

## Prove it

```sh
python3 harness.py all          # full falsification run (~5 min)
python3 harness.py --quick all  # CI-scale run (~15 s)
```

The harness states its kill conditions up front and exits nonzero if any fires:

| Kill condition | Test |
|---|---|
| K1 — any job starts without a durable reservation; ledger inconsistent after `kill -9` | 8 hammer processes, SIGKILL mid-burst, then: balance drift = 0, zero orphan starts, zero duplicate keys |
| K2 — a duplicate retry creates duplicate liability | 10,000 admissions across 100 workers, 50% replay rate; replay verdict must equal original verdict, zero mismatches |
| K3 — liability ever exceeds the $100 cap | continuous balance sampling under full concurrency; charged total == cap exactly, floor never breached |

Latest full run (2026-10-06): **ALL PASS** — 100 admitted / 9,900 rejected at exactly $100.00, 0 idempotency mismatches, 0 drift after kill -9. See [REPORT.md](REPORT.md).

## What this is not

- Not a vendor integration. Deployment requires the meter-holder's cooperation — the admission check must live where the call is metered. The harness proves the *mechanism*; the deployment is a separate negotiation.
- Not a receipt or proof layer. This sells prevention, not evidence. (A sibling thesis — subpoena-ready agent receipts — was killed on trust-root grounds; this shape was designed to survive exactly that objection.)
- Not a judgment layer. It bounds authority; it does not decide whether the spend was wise.

## Origin

Built 2026-10-06 after a scout of X/Reddit/news (subpoena week, Oct 2026) surfaced five real-world holes, and a two-team adversarial review (Perplexity on Sol 6.1 + Grok adversary, held to a high bar) killed the receipt-based and rail-based product theses. Both bots independently converged on vendor-side atomic admission as the surviving shape; the harness is its falsifier, and the shape survived.

## License

MIT — see [LICENSE](LICENSE).
