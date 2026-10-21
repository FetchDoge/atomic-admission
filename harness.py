#!/usr/bin/env python3
"""
Falsification harness — Shape 1: vendor-side atomic admission.

Mechanism under test: the party that meters the call atomically decrements a
non-negative credit in the SAME step that admits it. At zero, the call never
runs, so nothing is billable.

Binary kill conditions:
  K1 (atomicity): after kill -9 mid-burst, the ledger must be consistent —
      every committed reservation has its balance decrement and vice versa;
      no started job lacks a committed reservation.
  K2 (idempotency): duplicate retries on the same idempotency key must not
      create duplicate liability or duplicate refunds.
  K3 (cap): total liability (reserved-but-unsettled + settled) must never
      exceed CAP, and ledger balance must never drop below zero.

Usage:
  harness.py phaseA   # concurrency + idempotency + cap + reconciliation (100 threads, 10k admissions)
  harness.py phaseB   # crash: spawn hammer procs, kill -9 mid-burst, check K1
  harness.py hammer <prefix> <n>   # child mode: tight admit loop
"""
import os
import random
import signal
import subprocess
import sys
import threading
import time

from atomic_admission import AtomicMeter, EPS

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ledger.db")
CAP = 100.0
COST = 1.0

_meter = AtomicMeter(DB, initial_cap=CAP)


def init_db():
    for p in (DB, DB + "-wal", DB + "-shm", DB + "-journal"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    global _meter
    _meter = AtomicMeter(DB, initial_cap=CAP)


def admit(idem, amount):
    return _meter.admit(idem, amount)


def settle(idem, actual_amount=None):
    return _meter.settle(idem, actual_amount=actual_amount)


def release(idem):
    return _meter.release(idem)


def mark_started(job, idem):
    _meter.mark_started(job, idem)


def read_balance():
    return _meter.get_balance()


def consistency_report():
    return _meter.verify_consistency()


def phase_a(n_threads=100, n_jobs=100):
    """Concurrency + idempotency + cap + credit reconciliation."""
    init_db()
    stats = {
        "admitted": 0,
        "rejected": 0,
        "released": 0,
        "partial_settled": 0,
        "full_settled": 0,
        "idem_mismatch": 0,
        "over_settle_caught": 0,
    }
    lock = threading.Lock()
    min_bal = [CAP]
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            b = read_balance()
            with lock:
                if b < min_bal[0]:
                    min_bal[0] = b
            time.sleep(0.005)

    def worker(wid):
        for i in range(n_jobs):
            idem = f"w{wid}-{i}"
            ok = admit(idem, COST)
            with lock:
                stats["admitted" if ok else "rejected"] += 1

            if ok:
                r = random.random()
                if r < 0.10:
                    # Case 1: Pre-execution cancellation/timeout
                    rel_ok = release(idem)
                    with lock:
                        if rel_ok:
                            stats["released"] += 1
                else:
                    mark_started(idem, idem)
                    if random.random() < 0.2:
                        time.sleep(random.random() * 0.002)

                    # Malicious over-settlement probe: attempt to claim more than reserved
                    if random.random() < 0.05:
                        try:
                            settle(idem, actual_amount=COST * 2.0)
                        except ValueError:
                            with lock:
                                stats["over_settle_caught"] += 1

                    if r < 0.40:
                        # Case 2: Partial settlement (reconciliation of unspent credit)
                        actual = round(random.uniform(0.1, 0.8) * COST, 4)
                        s_ok = settle(idem, actual_amount=actual)
                        with lock:
                            if s_ok:
                                stats["partial_settled"] += 1
                    else:
                        # Case 3: Full settlement
                        s_ok = settle(idem)
                        with lock:
                            if s_ok:
                                stats["full_settled"] += 1

            # Duplicate retry on the same key — must never double-charge.
            # Strict rule: replay verdict must equal the original verdict.
            if random.random() < 0.5:
                ok2 = admit(idem, COST)
                if ok2 != ok:
                    with lock:
                        stats["idem_mismatch"] += 1

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    mon.join()
    dur = time.time() - t0

    ok_consist, details = consistency_report()
    bal = read_balance()
    k2_ok = stats["idem_mismatch"] == 0
    k3_ok = min_bal[0] >= -EPS and ok_consist and bal >= -EPS

    print(f"phaseA: {dur:.1f}s admitted={stats['admitted']} rejected={stats['rejected']} "
          f"released={stats['released']} partial={stats['partial_settled']} full={stats['full_settled']} "
          f"min_balance={min_bal[0]:.2f} end_balance={bal:.2f}")
    print(f"K2 idempotency: {'PASS' if k2_ok else 'KILL'} "
          f"(mismatches={stats['idem_mismatch']})")
    print(f"K3 cap & reconciliation: {'PASS' if k3_ok else 'KILL'} "
          f"({details})")
    return k2_ok and k3_ok


def phase_b(n_hammers=8, n_each=4000, sleep_s=0.6):
    """Crash test: hammer the ledger from child procs, kill -9 mid-burst, check K1."""
    init_db()
    procs = [
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "hammer", f"c{i}", str(n_each)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(n_hammers)
    ]
    time.sleep(sleep_s)
    for p in procs:
        try:
            p.send_signal(signal.SIGKILL)
        except Exception:
            pass
    for p in procs:
        p.wait()
    # reopen + recover, then verify
    ok, details = consistency_report()
    print(f"phaseB (kill -9 mid-burst): K1 atomicity: {'PASS' if ok else 'KILL'} ({details})")
    return ok


def hammer(prefix, n):
    for j in range(int(n)):
        idem = f"{prefix}-{j}"
        if admit(idem, COST):
            if j % 7 == 0:
                release(idem)
            else:
                mark_started(idem, idem)
                if j % 3 == 0:
                    settle(idem, actual_amount=0.5 * COST)
                else:
                    settle(idem)


if __name__ == "__main__":
    random.seed(1234)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--quick" in sys.argv
    mode = args[0] if args else "phaseA"
    # --quick: CI-friendly scale (500 admissions + small crash phase)
    pa = (20, 25) if quick else (100, 100)
    pb = (4, 500, 0.3) if quick else (8, 4000, 0.6)
    if mode == "phaseA":
        sys.exit(0 if phase_a(*pa) else 1)
    elif mode == "phaseB":
        sys.exit(0 if phase_b(*pb) else 1)
    elif mode == "hammer":
        hammer(args[1], args[2])
    elif mode == "all":
        a = phase_a(*pa)
        b = phase_b(*pb)
        print(f"OVERALL: {'ALL PASS — shape survives' if (a and b) else 'KILL — shape fails'}")
        sys.exit(0 if (a and b) else 1)
