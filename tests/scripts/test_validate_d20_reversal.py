"""
tests/scripts/test_validate_d20_reversal.py
-----------------------------------------------------------
The one-shot protected test harness (Phase 25.9B, §29).

These pin the guards that make a single protected evaluation mean
something: readiness is judged from counts before any statistic, an
unready window is never opened, a final verdict locks the window, and
the frozen specification cannot drift.
"""

import os
import sqlite3
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scripts.validate_d20_reversal as V
from src.data_access.experiment_schema import initialize_experiment_schema


def rows_for(dates, per_date, ic_sign=-1, seed=0):
    """Synthetic (date, feature, target) with a planted relationship."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(dates):
        f = rng.normal(size=per_date)
        t = ic_sign * f + rng.normal(scale=0.5, size=per_date)
        rows.extend((f"2026-08-{d + 1:02d}", float(a), float(b))
                    for a, b in zip(f, t))
    return rows


class TestFrozenSpecification(unittest.TestCase):

    def test_the_hypothesis_is_exactly_the_registered_one(self):
        """§11: no d20->d10, 60d->30d, negative->positive, abnormal->raw."""
        self.assertEqual(V.FEATURE, "market.return_60d")
        self.assertEqual(V.EXPECTED_SIGN, -1)
        self.assertEqual(V.TARGET_LONG, "d20.abnormal_return.anchor-v2")
        self.assertEqual(V.TARGET_SHORT, "d5.abnormal_return.anchor-v2")
        self.assertNotIn("raw", V.TARGET_LONG)

    def test_the_protected_window_is_the_registered_one(self):
        self.assertEqual(V.PROTECTED_START, "2026-08-15T01:23:31")
        self.assertEqual(V.PROTECTED_END, "2026-08-27T17:09:32")

    def test_the_fingerprint_changes_if_the_spec_changes(self):
        original = V.fingerprint()
        saved = V.EXPECTED_SIGN
        try:
            V.EXPECTED_SIGN = 1
            self.assertNotEqual(V.fingerprint(), original)
        finally:
            V.EXPECTED_SIGN = saved


class TestReadinessGate(unittest.TestCase):

    def test_no_resolved_labels_is_not_ready(self):
        """The actual state of the protected window on 2026-09-13."""
        gate = V.readiness([])
        self.assertFalse(gate["ready"])
        self.assertEqual(gate["rows"], 0)

    def test_too_few_dates_is_not_ready_even_with_many_rows(self):
        gate = V.readiness(rows_for(dates=3, per_date=50))
        self.assertFalse(gate["ready"])
        self.assertTrue(any("eligible dates" in f for f in gate["failures"]))

    def test_enough_rows_and_dates_is_ready(self):
        """
        14 x 15 = 210 rows. MDE <= 0.20 needs at least 194; 180 gives
        0.208 and is correctly refused, which is what the gate is for.
        """
        gate = V.readiness(rows_for(dates=14, per_date=15))
        self.assertTrue(gate["ready"], gate["failures"])

    def test_just_below_the_power_requirement_is_refused(self):
        gate = V.readiness(rows_for(dates=12, per_date=15))
        self.assertFalse(gate["ready"])
        self.assertTrue(any("detectable" in f for f in gate["failures"]))

    def test_minimum_detectable_effect_shrinks_with_sample(self):
        self.assertGreater(V.minimum_detectable_effect(50),
                           V.minimum_detectable_effect(500))
        self.assertAlmostEqual(V.minimum_detectable_effect(92), 0.289, places=2)


class TestClassification(unittest.TestCase):
    """
    Uses 200 permutations to keep the suite fast. This exercises the
    classification LOGIC; the frozen production constant is restored
    after each test and is what the real run uses.
    """

    def setUp(self):
        self._saved = V.PERMUTATIONS
        V.PERMUTATIONS = 200

    def tearDown(self):
        V.PERMUTATIONS = self._saved

    def test_a_planted_negative_relationship_is_supported(self):
        result = V.evaluate(rows_for(dates=12, per_date=15, ic_sign=-1))
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertLess(result["mean_ic"], 0)

    def test_the_wrong_sign_is_not_supported(self):
        """A strong POSITIVE relationship is a failure, not a success."""
        result = V.evaluate(rows_for(dates=12, per_date=15, ic_sign=1))
        self.assertEqual(result["verdict"], "NOT SUPPORTED")

    def test_no_relationship_is_not_supported(self):
        rng = np.random.default_rng(3)
        rows = [(f"2026-08-{d + 1:02d}", float(rng.normal()), float(rng.normal()))
                for d in range(12) for _ in range(15)]
        self.assertEqual(V.evaluate(rows)["verdict"], "NOT SUPPORTED")

    def test_there_is_no_weak_support_category(self):
        verdicts = {V.evaluate(rows_for(12, 15, s, seed=k))["verdict"]
                    for s in (-1, 1) for k in range(3)}
        self.assertTrue(verdicts <= {"SUPPORTED", "NOT SUPPORTED",
                                     "INSUFFICIENT DATA"})


class TestSingleEvaluationLock(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_experiment_schema(self.conn)

    def test_an_unready_attempt_does_not_lock_the_window(self):
        """Counting opens nothing, so readiness may be re-checked."""
        V.persist(self.conn, "insufficient_data", {"opened": False})
        self.assertIsNone(V.already_evaluated(self.conn))

    def test_a_final_verdict_locks_the_window(self):
        for status in ("supported", "not_supported"):
            conn = sqlite3.connect(":memory:")
            initialize_experiment_schema(conn)
            V.persist(conn, status, {"opened": True})
            self.assertEqual(V.already_evaluated(conn), status)

    def test_the_persisted_record_carries_reproducibility_fields(self):
        V.persist(self.conn, "insufficient_data", {"opened": False})
        row = self.conn.execute("""
            SELECT method_version, label_version, fingerprint, source_reference
            FROM experiments WHERE experiment_id = ?
        """, (V.EXPERIMENT_ID,)).fetchone()
        self.assertEqual(row[0], "anchor-v2")
        self.assertEqual(row[1], "v2")
        self.assertEqual(row[2], V.fingerprint())
        self.assertIn(V.PREREGISTRATION, row[3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
