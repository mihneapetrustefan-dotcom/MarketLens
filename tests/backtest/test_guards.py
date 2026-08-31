"""
tests/backtest/test_guards.py
----------------------------------
Temporal guards (spec §51, §52).

Each guard is checked in both directions — that a correct ordering
passes silently, and that a reversed one raises. The second half
matters more: a guard that never fires is indistinguishable from no
guard at all, and these tests are the only thing that proves the
difference.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.guards import TemporalGuard, TemporalViolation

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(hours=1)
EARLIER = T0 - timedelta(hours=1)


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.guard = TemporalGuard(run_id="test")


class TestFeatureNotFuture(GuardTestCase):
    def test_feature_before_the_cutoff_passes(self):
        self.guard.check_feature_not_future(EARLIER, T0)

    def test_feature_at_the_cutoff_passes(self):
        self.guard.check_feature_not_future(T0, T0)

    def test_feature_after_the_cutoff_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_feature_not_future(LATER, T0)

    def test_missing_feature_time_is_not_a_violation(self):
        self.guard.check_feature_not_future(None, T0)


class TestSignalAfterInformation(GuardTestCase):
    def test_signal_after_its_cutoff_passes(self):
        self.guard.check_signal_after_information(LATER, T0)

    def test_signal_before_its_cutoff_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_signal_after_information(EARLIER, T0)


class TestOrderAfterSignal(GuardTestCase):
    def test_order_after_the_signal_passes(self):
        self.guard.check_order_after_signal(LATER, T0)

    def test_order_before_the_signal_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_order_after_signal(EARLIER, T0)

    def test_unknown_signal_time_is_not_a_violation(self):
        self.guard.check_order_after_signal(T0, None)


class TestFillAfterOrder(GuardTestCase):
    """The load-bearing inequality."""

    def test_fill_strictly_after_the_order_passes(self):
        self.guard.check_fill_after_order(LATER, T0)

    def test_fill_at_the_same_moment_raises_by_default(self):
        with self.assertRaises(TemporalViolation) as caught:
            self.guard.check_fill_after_order(T0, T0)
        self.assertIn("look-ahead", str(caught.exception))

    def test_fill_before_the_order_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_fill_after_order(EARLIER, T0)

    def test_same_moment_is_allowed_only_when_explicitly_permitted(self):
        self.guard.check_fill_after_order(T0, T0, allow_same_moment=True)

    def test_a_fill_before_the_order_still_raises_when_same_moment_is_allowed(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_fill_after_order(EARLIER, T0, allow_same_moment=True)


class TestOutcomeAfterDecision(GuardTestCase):
    def test_outcome_after_the_decision_passes(self):
        self.guard.check_outcome_after_decision(LATER, T0)

    def test_outcome_at_the_decision_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_outcome_after_decision(T0, T0)

    def test_outcome_before_the_decision_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_outcome_after_decision(EARLIER, T0)


class TestDataReads(GuardTestCase):
    def test_bar_at_the_anchor_passes(self):
        self.guard.check_bar_not_future(T0, T0)

    def test_bar_after_the_anchor_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_bar_not_future(LATER, T0)

    def test_moment_beyond_the_horizon_raises(self):
        with self.assertRaises(TemporalViolation):
            self.guard.check_within_horizon(LATER, T0)

    def test_moment_within_the_horizon_passes(self):
        self.guard.check_within_horizon(EARLIER, T0)


class TestModelTraining(GuardTestCase):
    """Spec §47, §49 — the in-sample replay guard."""

    def test_model_trained_before_the_decision_passes(self):
        self.guard.check_model_trained_before(EARLIER, T0, "m")

    def test_model_trained_after_the_decision_raises(self):
        with self.assertRaises(TemporalViolation) as caught:
            self.guard.check_model_trained_before(LATER, T0, "ridge:v1")
        self.assertIn("in-sample replay", str(caught.exception))

    def test_unknown_training_time_is_not_a_violation(self):
        self.guard.check_model_trained_before(None, T0)


class TestAccounting(GuardTestCase):
    def test_checks_are_counted(self):
        self.guard.check_feature_not_future(EARLIER, T0)
        self.guard.check_order_after_signal(LATER, T0)
        self.assertEqual(self.guard.checks_performed, 2)
        self.assertEqual(self.guard.by_check["feature_not_future"], 1)

    def test_a_guard_that_never_ran_is_visible_as_such(self):
        """Zero checks means unguarded, not safe — the summary says so."""
        self.assertEqual(self.guard.summary()["checks_performed"], 0)

    def test_non_strict_mode_records_instead_of_raising(self):
        guard = TemporalGuard(strict=False)
        guard.check_fill_after_order(EARLIER, T0)
        self.assertEqual(len(guard.recorded), 1)
        self.assertEqual(guard.summary()["violations_recorded"], 1)

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            self.guard.check_bar_not_future(datetime(2026, 6, 1), T0)


if __name__ == "__main__":
    unittest.main()
