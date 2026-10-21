"""Unit tests for atomic_admission engine.

Tests the spec-first invariants:
  - Atomic admission with hard cap (K1, K3)
  - Idempotent replay of admissions and settlements (K2)
  - Partial settlement with exact credit reconciliation
  - Over-settlement rejection (fails closed, cannot escalate liability)
  - Pre-execution cancellation / release (returns 100% reserved credit)
  - Zero ledger drift under all operations
"""
import os
import tempfile
import unittest

from atomic_admission import AtomicMeter


class TestAtomicMeter(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_ledger.db")
        self.cap = 100.0
        self.meter = AtomicMeter(self.db_path, initial_cap=self.cap)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_initial_state(self):
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)
        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_admit_success_debits_balance(self):
        ok = self.meter.admit("job-1", 25.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(self.meter.get_balance(), 75.0)
        status = self.meter.get_reservation("job-1")
        self.assertIsNotNone(status)
        self.assertEqual(status["state"], "reserved")
        self.assertAlmostEqual(status["amount"], 25.0)
        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_admit_idempotency_does_not_double_charge(self):
        ok1 = self.meter.admit("job-1", 25.0)
        self.assertTrue(ok1)
        self.assertAlmostEqual(self.meter.get_balance(), 75.0)

        # Replay same key with same or different amount
        ok2 = self.meter.admit("job-1", 25.0)
        self.assertTrue(ok2)
        self.assertAlmostEqual(self.meter.get_balance(), 75.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_admit_insufficient_balance_rejected(self):
        ok = self.meter.admit("too-expensive", 101.0)
        self.assertFalse(ok)
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)
        status = self.meter.get_reservation("too-expensive")
        self.assertEqual(status["state"], "rejected")

        # Replay on rejected key returns original verdict (False)
        ok_replay = self.meter.admit("too-expensive", 101.0)
        self.assertFalse(ok_replay)
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_admit_invalid_amount_fails_closed(self):
        with self.assertRaises(ValueError):
            self.meter.admit("negative", -5.0)
        with self.assertRaises(ValueError):
            self.meter.admit("zero", 0.0)
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)

    def test_settle_full_reservation(self):
        self.meter.admit("job-full", 30.0)
        self.assertAlmostEqual(self.meter.get_balance(), 70.0)

        ok = self.meter.settle("job-full")
        self.assertTrue(ok)
        self.assertAlmostEqual(self.meter.get_balance(), 70.0)
        status = self.meter.get_reservation("job-full")
        self.assertEqual(status["state"], "settled")
        self.assertAlmostEqual(status["amount"], 30.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_settle_partial_amount_refunds_unspent_credit(self):
        # Reserve $40.00 for expected worst-case token usage
        self.meter.admit("job-partial", 40.0)
        self.assertAlmostEqual(self.meter.get_balance(), 60.0)

        # Actual compute only costs $15.50
        ok = self.meter.settle("job-partial", actual_amount=15.50)
        self.assertTrue(ok)

        # Balance must reflect the refund of $24.50 (60.0 + 24.5 = 84.50)
        self.assertAlmostEqual(self.meter.get_balance(), 84.50)
        status = self.meter.get_reservation("job-partial")
        self.assertEqual(status["state"], "settled")
        self.assertAlmostEqual(status["amount"], 15.50)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_settle_idempotency_prevents_duplicate_refunds(self):
        self.meter.admit("job-idem-settle", 50.0)
        self.meter.settle("job-idem-settle", actual_amount=20.0)
        self.assertAlmostEqual(self.meter.get_balance(), 80.0)

        # Duplicate settle call on already settled reservation
        ok2 = self.meter.settle("job-idem-settle", actual_amount=20.0)
        self.assertTrue(ok2)
        # Balance must NOT increase again
        self.assertAlmostEqual(self.meter.get_balance(), 80.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_settle_over_reserved_amount_fails_closed(self):
        self.meter.admit("job-over", 20.0)
        self.assertAlmostEqual(self.meter.get_balance(), 80.0)

        # Attempting to settle for more than reserved ceiling ($25 > $20)
        with self.assertRaises(ValueError):
            self.meter.settle("job-over", actual_amount=25.0)

        # State and balance remain untouched
        self.assertAlmostEqual(self.meter.get_balance(), 80.0)
        status = self.meter.get_reservation("job-over")
        self.assertEqual(status["state"], "reserved")
        self.assertAlmostEqual(status["amount"], 20.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_release_aborted_call_refunds_full_credit(self):
        self.meter.admit("job-aborted", 35.0)
        self.assertAlmostEqual(self.meter.get_balance(), 65.0)

        # Call aborted before execution, releasing reservation
        ok = self.meter.release("job-aborted")
        self.assertTrue(ok)
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)

        status = self.meter.get_reservation("job-aborted")
        self.assertEqual(status["state"], "released")
        self.assertAlmostEqual(status["amount"], 0.0)

        # Subsequent release on same key is idempotent
        ok_dup = self.meter.release("job-aborted")
        self.assertTrue(ok_dup)
        self.assertAlmostEqual(self.meter.get_balance(), 100.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)

    def test_release_on_settled_job_rejected(self):
        self.meter.admit("job-done", 30.0)
        self.meter.settle("job-done", actual_amount=30.0)

        # Cannot release an already settled job
        ok = self.meter.release("job-done")
        self.assertFalse(ok)
        self.assertAlmostEqual(self.meter.get_balance(), 70.0)

        ok, msg = self.meter.verify_consistency()
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
