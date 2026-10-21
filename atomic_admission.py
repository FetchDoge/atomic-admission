"""Atomic Admission Engine — Vendor-side pre-spend enforcement at the meter.

The party that meters an AI agent call atomically debits prepaid credit in the
SAME step that admits it. At zero credit, the call never runs, so nothing is
billable.

Guarantees:
  - K1 (Atomicity): check, debit, and reservation commit together via SQLite
    BEGIN IMMEDIATE. Crash recovery (WAL mode) rolls back incomplete transactions.
  - K2 (Idempotency): every operation carries an idempotency key. Replays return
    the original verdict and never double-charge or double-refund.
  - K3 (Hard Cap): total liability (reserved + settled) never exceeds the cap,
    and ledger balance never drops below zero.
  - Credit Reconciliation: at settlement, actual compute usage is committed,
    and any unspent reservation is returned to the balance in the same atomic
    step. Over-settlement (actual > reserved) fails closed.
  - Zero Dependencies: Python 3 standard library only (sqlite3).
"""
import os
import sqlite3
from typing import Any, Dict, Optional, Tuple

EPS = 1e-6


class AtomicMeter:
    """Manages prepaid quota and atomic admission for agent calls."""

    def __init__(self, db_path: str, initial_cap: float = 100.0, timeout: float = 60.0):
        self.db_path = os.path.abspath(db_path)
        self.initial_cap = float(initial_cap)
        self.timeout = float(timeout)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self.timeout, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=60000;")
        return con

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = self._get_connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "CREATE TABLE IF NOT EXISTS ledger ("
                "  balance REAL NOT NULL,"
                "  cap REAL NOT NULL"
                ");"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS reservations ("
                "  idem TEXT PRIMARY KEY,"
                "  amount REAL NOT NULL,"
                "  state TEXT NOT NULL"
                ");"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS started ("
                "  job TEXT PRIMARY KEY,"
                "  idem TEXT NOT NULL"
                ");"
            )
            # Ensure cap column exists if table existed previously without it
            try:
                con.execute("ALTER TABLE ledger ADD COLUMN cap REAL NOT NULL DEFAULT 100.0")
            except sqlite3.OperationalError:
                pass

            row = con.execute("SELECT balance, cap FROM ledger LIMIT 1").fetchone()
            if not row:
                con.execute(
                    "INSERT INTO ledger (balance, cap) VALUES (?, ?)",
                    (self.initial_cap, self.initial_cap),
                )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            con.close()

    def admit(self, idem: str, amount: float) -> bool:
        """Atomic admission. True iff a durable reservation is committed (or was)."""
        if amount <= 0:
            raise ValueError(f"Admission amount must be positive, got {amount}")

        con = self._get_connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            ex = con.execute(
                "SELECT state FROM reservations WHERE idem=?", (idem,)
            ).fetchone()
            if ex:
                con.execute("COMMIT")
                # Replay: return the original admission verdict
                return ex[0] in ("reserved", "settled", "released")

            bal = con.execute("SELECT balance FROM ledger LIMIT 1").fetchone()[0]
            if bal < amount - EPS:
                con.execute(
                    "INSERT INTO reservations(idem, amount, state) VALUES(?, ?, 'rejected')",
                    (idem, amount),
                )
                con.execute("COMMIT")
                return False

            con.execute("UPDATE ledger SET balance = balance - ?", (amount,))
            con.execute(
                "INSERT INTO reservations(idem, amount, state) VALUES(?, ?, 'reserved')",
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

    def settle(self, idem: str, actual_amount: Optional[float] = None) -> bool:
        """Settle an admitted reservation, reconciling actual usage.

        If actual_amount is provided, commits the actual amount and refunds
        unspent credit (reserved_amount - actual_amount) back to the balance.
        If actual_amount exceeds reserved_amount, fails closed with ValueError.
        """
        if actual_amount is not None and actual_amount < 0:
            raise ValueError(f"Settlement amount cannot be negative, got {actual_amount}")

        con = self._get_connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT amount, state FROM reservations WHERE idem=?", (idem,)
            ).fetchone()
            if not row:
                con.execute("COMMIT")
                return False

            reserved_amount, state = row
            if state == "settled":
                con.execute("COMMIT")
                return True  # Idempotent replay

            if state != "reserved":
                con.execute("COMMIT")
                return False

            final_amount = reserved_amount if actual_amount is None else float(actual_amount)
            if final_amount > reserved_amount + EPS:
                con.execute("ROLLBACK")
                raise ValueError(
                    f"Settlement amount {final_amount:.4f} exceeds reserved ceiling {reserved_amount:.4f}"
                )

            unspent = reserved_amount - final_amount
            if unspent > EPS:
                con.execute("UPDATE ledger SET balance = balance + ?", (unspent,))

            con.execute(
                "UPDATE reservations SET state='settled', amount=? WHERE idem=?",
                (final_amount, idem),
            )
            con.execute("COMMIT")
            return True
        except ValueError:
            raise
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            return False
        finally:
            con.close()

    def release(self, idem: str) -> bool:
        """Cancel an admitted reservation before execution, refunding 100% of credit."""
        con = self._get_connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT amount, state FROM reservations WHERE idem=?", (idem,)
            ).fetchone()
            if not row:
                con.execute("COMMIT")
                return False

            reserved_amount, state = row
            if state == "released":
                con.execute("COMMIT")
                return True  # Idempotent replay

            if state != "reserved":
                con.execute("COMMIT")
                return False

            con.execute("UPDATE ledger SET balance = balance + ?", (reserved_amount,))
            con.execute(
                "UPDATE reservations SET state='released', amount=0.0 WHERE idem=?",
                (idem,),
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

    def mark_started(self, job: str, idem: str) -> None:
        """Records that a job has physically started execution."""
        con = self._get_connection()
        try:
            con.execute("INSERT OR IGNORE INTO started(job, idem) VALUES(?, ?)", (job, idem))
        finally:
            con.close()

    def get_balance(self) -> float:
        """Returns the current available ledger balance."""
        con = self._get_connection()
        try:
            row = con.execute("SELECT balance FROM ledger LIMIT 1").fetchone()
            return row[0] if row else 0.0
        finally:
            con.close()

    def get_reservation(self, idem: str) -> Optional[Dict[str, Any]]:
        """Returns the state and amount of a reservation, or None."""
        con = self._get_connection()
        try:
            row = con.execute(
                "SELECT idem, amount, state FROM reservations WHERE idem=?", (idem,)
            ).fetchone()
            if not row:
                return None
            return {"idem": row[0], "amount": row[1], "state": row[2]}
        finally:
            con.close()

    def verify_consistency(self) -> Tuple[bool, str]:
        """Validates K1 atomicity and K3 cap integrity."""
        con = self._get_connection()
        try:
            row = con.execute("SELECT balance, cap FROM ledger LIMIT 1").fetchone()
            if not row:
                return False, "Ledger table is missing or empty"
            bal, cap = row

            reserved_sum = con.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE state IN ('reserved', 'settled')"
            ).fetchone()[0]

            orphans = con.execute(
                """SELECT COUNT(*) FROM started s LEFT JOIN reservations r
                   ON s.idem = r.idem AND r.state IN ('reserved', 'settled')
                   WHERE r.idem IS NULL"""
            ).fetchone()[0]

            dupes = con.execute(
                "SELECT COUNT(*) FROM (SELECT idem FROM reservations GROUP BY idem HAVING COUNT(*) > 1)"
            ).fetchone()[0]

            drift = abs((reserved_sum + bal) - cap)
            ok = drift < 1e-4 and orphans == 0 and dupes == 0
            details = f"drift={drift:.6f} orphans={orphans} dupe_idems={dupes} balance={bal:.2f} cap={cap:.2f}"
            return ok, details
        finally:
            con.close()
