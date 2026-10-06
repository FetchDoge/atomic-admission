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
      create duplicate liability.
  K3 (cap): total liability (reserved-but-unsettled + settled) must never
      exceed CAP.

Usage:
  harness.py phaseA   # concurrency + idempotency + cap (100 threads, 10k admissions)
  harness.py phaseB   # crash: spawn hammer procs, kill -9 mid-burst, check K1
  harness.py hammer <prefix> <n>   # child mode: tight admit loop
"""
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ledger.db")
CAP = 100.0
COST = 1.0
EPS = 1e-6


def init_db():
    for p in (DB, DB + "-wal", DB + "-shm", DB + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("CREATE TABLE ledger(balance REAL NOT NULL)")
    con.execute("INSERT INTO ledger(balance) VALUES (?)", (CAP,))
    con.execute(
        "CREATE TABLE reservations(idem TEXT PRIMARY KEY, amount REAL NOT NULL, state TEXT NOT NULL)"
    )
    con.execute("CREATE TABLE started(job TEXT PRIMARY KEY, idem TEXT NOT NULL)")
    con.commit()
    con.close()


def admit(idem, amount):
    """Atomic admission. True iff a durable reservation is committed (or was)."""
    con = sqlite3.connect(DB, timeout=60, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        ex = con.execute(
            "SELECT state FROM reservations WHERE idem=?", (idem,)
        ).fetchone()
        if ex:
            con.execute("COMMIT")
            return ex[0] in ("reserved", "settled")  # replay: original verdict
        bal = con.execute("SELECT balance FROM ledger").fetchone()[0]
        if bal < amount - EPS:
            con.execute(
                "INSERT INTO reservations(idem,amount,state) VALUES(?,?, 'rejected')",
                (idem, amount),
            )
            con.execute("COMMIT")
            return False
        con.execute("UPDATE ledger SET balance = balance - ?", (amount,))
        con.execute(
            "INSERT INTO reservations(idem,amount,state) VALUES(?,?, 'reserved')",
            (idem, amount),
        )
        con.execute("COMMIT")
        return True
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        return False
    finally:
        con.close()


def settle(idem):
    con = sqlite3.connect(DB, timeout=60, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE reservations SET state='settled' WHERE idem=? AND state='reserved'",
            (idem,),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        con.close()


def mark_started(job, idem):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("INSERT OR IGNORE INTO started(job,idem) VALUES(?,?)", (job, idem))
    con.commit()
    con.close()


def read_balance():
    con = sqlite3.connect(DB, timeout=60)
    bal = con.execute("SELECT balance FROM ledger").fetchone()[0]
    con.close()
    return bal


def consistency_report():
    """Returns (ok, details) for K1."""
    con = sqlite3.connect(DB, timeout=60)
    bal = con.execute("SELECT balance FROM ledger").fetchone()[0]
    reserved_sum = (
        con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM reservations WHERE state IN ('reserved','settled')"
        ).fetchone()[0]
    )
    orphans = con.execute(
        """SELECT COUNT(*) FROM started s LEFT JOIN reservations r
           ON s.idem = r.idem AND r.state IN ('reserved','settled')
           WHERE r.idem IS NULL"""
    ).fetchone()[0]
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT idem FROM reservations GROUP BY idem HAVING COUNT(*)>1)"
    ).fetchone()[0]
    con.close()
    drift = abs((reserved_sum + bal) - CAP)
    return (
        drift < 1e-4 and orphans == 0 and dupes == 0,
        f"drift={drift:.6f} orphans={orphans} dupe_idems={dupes} balance={bal:.2f}",
    )


def phase_a(n_threads=100, n_jobs=100):
    """Concurrency + idempotency + cap. Default: 100 threads, 10k admissions, dup retries."""
    init_db()
    stats = {"admitted": 0, "rejected": 0, "idem_mismatch": 0}
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
                mark_started(idem, idem)
                if random.random() < 0.3:
                    time.sleep(random.random() * 0.002)
                settle(idem)
            # duplicate retry on the same key — must never double-charge.
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

    con = sqlite3.connect(DB, timeout=60)
    bal = con.execute("SELECT balance FROM ledger").fetchone()[0]
    charged = (
        con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM reservations WHERE state IN ('reserved','settled')"
        ).fetchone()[0]
    )
    n_started = con.execute("SELECT COUNT(*) FROM started").fetchone()[0]
    con.close()

    k2_ok = stats["idem_mismatch"] == 0
    k3_ok = min_bal[0] >= -EPS and charged <= CAP + EPS
    print(f"phaseA: {dur:.1f}s admitted={stats['admitted']} rejected={stats['rejected']} "
          f"started={n_started} charged={charged:.2f} min_balance={min_bal[0]:.2f}")
    print(f"K2 idempotency: {'PASS' if k2_ok else 'KILL'} "
          f"(mismatches={stats['idem_mismatch']})")
    print(f"K3 cap: {'PASS' if k3_ok else 'KILL'} "
          f"(charged={charged:.2f} <= {CAP}, min_balance={min_bal[0]:.2f} >= 0)")
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
            mark_started(idem, idem)
            if j % 3 == 0:
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
