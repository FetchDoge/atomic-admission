# Falsification report — atomic-admission (2026-10-06)

## Method (traces)

- Mechanism implemented in `harness.py` (stdlib only: `sqlite3` WAL mode, `BEGIN IMMEDIATE` transactions, `threading`, `subprocess`).
- Phase A: 100 threads × 100 admissions = 10,000 admissions, 50% duplicate-retry replay rate, continuous balance sampling (5 ms), then K2/K3 checks.
- Phase B: fresh ledger, 8 hammer child processes in a tight admit loop, `SIGKILL` all mid-burst (~0.6 s), reopen with WAL recovery, then K1 consistency checks.
- Full run: `python3 harness.py all` → exit 0. CI runs `python3 harness.py --quick all` (500 admissions + small crash phase, ~12 s) → exit 0.

## Results (verified — executed code, this machine)

| Check | Result |
|---|---|
| Phase A duration | 261.2 s |
| Admitted / rejected | 100 / 9,900 — cap exhausted *exactly* |
| Started jobs | 100 — every one holds a committed reservation |
| Charged total | $100.00 of $100.00 cap |
| Minimum balance observed (5 ms sampling, full concurrency) | $0.00 — floor never breached |
| K2 idempotency mismatches | 0 |
| K3 cap | PASS |
| Phase B (kill -9): balance drift | 0.000000 |
| Phase B: orphan starts (job without reservation) | 0 |
| Phase B: duplicate idempotency keys | 0 |
| K1 atomicity | PASS |
| **OVERALL** | **ALL PASS — shape survives** |

## Claim tiers

- *Verified:* every number above (executed scoring/ledger code, this run).
- *Inferred:* the mechanism generalizes — the invariants are properties of the transaction design, not of the workload shape tested.
- *Unknown / not claimed:* behavior under a real vendor's meter (deployment requires the meter-holder); adversarial operator with write access to the ledger process itself (threat model assumes the vendor's side is intact); performance at >10k concurrent admissions.

## Verdict

The shape survived its own falsifier. It is a proven mechanism design with a runnable proof — not a product claim, not a G-tier conferral. The next honest step is a deployment negotiation with a meter-holder, or a bounded demo (e.g., the Permit hackathon e-stop) framed as admission-gating rather than payment-declining.
