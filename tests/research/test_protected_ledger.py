"""
tests/research/test_protected_ledger.py
-----------------------------------------------------------
The single-use protected-test ledger and the open-once sequence
(Phase 25.9C, §12, §13).

EVERY TEST USES A TEMPORARY LEDGER. The real ledger at
research/protected_tests/ledger.jsonl is asserted untouched at the end.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scripts.validate_d20_reversal as V
from src.data_access.experiment_schema import initialize_experiment_schema
from src.research import protected_ledger as L

REAL_LEDGER = L.DEFAULT_LEDGER


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ledger.jsonl")
        with open(REAL_LEDGER, "rb") as handle:
            self.real_before = handle.read()

    def tearDown(self):
        shutil.rmtree(self.dir)
        with open(REAL_LEDGER, "rb") as handle:
            self.assertEqual(handle.read(), self.real_before,
                             "a test modified the REAL protected-test ledger")


class TestLedgerIntegrity(LedgerCase):

    def test_registration_then_status(self):
        L.register("t1", "fp1", {}, self.path)
        self.assertEqual(L.status("t1", self.path)["state"], "NOT_CONSUMED")

    def test_an_unregistered_test_cannot_be_opened(self):
        with self.assertRaises(L.LedgerError):
            L.require_openable("nope", "fp", self.path)

    def test_re_registration_is_refused(self):
        """A changed hypothesis needs a new identity, never a re-register."""
        L.register("t1", "fp1", {}, self.path)
        with self.assertRaises(L.LedgerError):
            L.register("t1", "fp2", {}, self.path)

    def test_a_changed_spec_cannot_open_the_registered_test(self):
        L.register("t1", "fp1", {}, self.path)
        with self.assertRaises(L.LedgerError) as refusal:
            L.require_openable("t1", "fp-different", self.path)
        self.assertIn("new test id", str(refusal.exception))

    def test_editing_an_entry_is_detected(self):
        L.register("t1", "fp1", {}, self.path)
        L.append("t1", L.OPENING, {}, self.path)
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        edited = json.loads(lines[0])
        edited["spec_fingerprint"] = "tampered"
        lines[0] = json.dumps(edited, sort_keys=True) + "\n"
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with self.assertRaises(L.LedgerError):
            L.verify(self.path)

    def test_removing_a_line_is_detected(self):
        """Deleting the OPENING entry to 'unconsume' a test breaks the chain."""
        L.register("t1", "fp1", {}, self.path)
        L.append("t1", L.OPENING, {}, self.path)
        L.append("t1", L.CONSUMED, {}, self.path)
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines([lines[0], lines[2]])
        with self.assertRaises(L.LedgerError):
            L.verify(self.path)

    def test_opening_alone_consumes_the_test(self):
        L.register("t1", "fp1", {}, self.path)
        L.append("t1", L.OPENING, {}, self.path)
        self.assertEqual(L.status("t1", self.path)["state"], "CONSUMED")
        with self.assertRaises(L.LedgerError):
            L.require_openable("t1", "fp1", self.path)


class TestOpenOnce(LedgerCase):
    """The validator's protected sequence, driven with stubs."""

    def setUp(self):
        super().setUp()
        L.register(V.EXPERIMENT_ID, V.fingerprint(), {}, self.path)
        self.conn = sqlite3.connect(":memory:")
        initialize_experiment_schema(self.conn)
        self.gate = {"rows": 0}
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    @staticmethod
    def ready(conn, now, path):
        return {"ready": True, "reasons": [], "dataset_identity": "ds-fixture"}

    @staticmethod
    def not_ready(conn, now, path):
        return {"ready": False, "reasons": ["fixture not ready"], "dataset_identity": "x"}

    def test_not_ready_never_opens_and_never_consumes(self):
        calls = []
        outcome = V.open_once(self.conn, [], self.gate, self.path, self.now,
                              readiness_fn=self.not_ready,
                              evaluate_fn=lambda rows: calls.append(rows))
        self.assertFalse(outcome["opened"])
        self.assertEqual(calls, [], "the statistic ran on a NOT READY window")
        self.assertEqual(L.status(V.EXPERIMENT_ID, self.path)["state"], "NOT_CONSUMED")

    def test_opening_is_recorded_before_the_statistic_runs(self):
        """A crash during evaluation must still leave the test consumed."""
        def crashing(rows):
            state = L.status(V.EXPERIMENT_ID, self.path)["state"]
            self.assertEqual(state, "CONSUMED", "OPENING was not written first")
            raise RuntimeError("simulated crash mid-evaluation")

        with self.assertRaises(RuntimeError):
            V.open_once(self.conn, [], self.gate, self.path, self.now,
                        readiness_fn=self.ready, evaluate_fn=crashing)
        self.assertEqual(L.status(V.EXPERIMENT_ID, self.path)["state"], "CONSUMED")

    def test_a_second_execution_is_refused(self):
        result = {"verdict": "NOT SUPPORTED", "mean_ic": 0.0}
        first = V.open_once(self.conn, [], self.gate, self.path, self.now,
                            readiness_fn=self.ready, evaluate_fn=lambda rows: result)
        self.assertTrue(first["opened"])
        ran = []
        second = V.open_once(self.conn, [], self.gate, self.path, self.now,
                             readiness_fn=self.ready,
                             evaluate_fn=lambda rows: ran.append(1) or result)
        self.assertEqual(second["exit_code"], 3)
        self.assertEqual(ran, [])

    def test_consumption_survives_a_fresh_working_copy(self):
        """
        The 25.9B lock lived in a disposable database. A brand-new
        database must not reopen a consumed test.
        """
        result = {"verdict": "SUPPORTED", "mean_ic": -0.2}
        V.open_once(self.conn, [], self.gate, self.path, self.now,
                    readiness_fn=self.ready, evaluate_fn=lambda rows: result)
        fresh = sqlite3.connect(":memory:")
        initialize_experiment_schema(fresh)
        self.assertIsNone(V.already_evaluated(fresh))          # the DB forgot
        again = V.open_once(fresh, [], self.gate, self.path, self.now,
                            readiness_fn=self.ready, evaluate_fn=lambda rows: result)
        self.assertEqual(again["exit_code"], 3)                # the ledger did not

    def test_the_consumed_entry_carries_identity_but_no_statistic(self):
        result = {"verdict": "NOT SUPPORTED", "mean_ic": 0.123, "p_value_one_sided": 0.4}
        V.open_once(self.conn, [], self.gate, self.path, self.now,
                    readiness_fn=self.ready, evaluate_fn=lambda rows: result)
        consumed = [e for e in L.read(self.path) if e["state"] == L.CONSUMED][0]
        self.assertEqual(consumed["dataset_identity"], "ds-fixture")
        self.assertIn("result_fingerprint", consumed)
        self.assertNotIn("mean_ic", consumed)


class TestTheRealLedger(unittest.TestCase):

    def test_the_real_d20_test_is_registered_and_unconsumed(self):
        """§13: the actual protected test must remain unconsumed."""
        status = L.status(V.EXPERIMENT_ID)
        self.assertEqual(status["state"], "NOT_CONSUMED")
        self.assertEqual(status["registration"]["spec_fingerprint"], V.fingerprint())
        self.assertEqual(status["registration"]["preregistration_commit"], "754a7bd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
