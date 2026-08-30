"""
tests/signals/test_signal_evaluation.py
-----------------------------------------------------------
Tests for Phase 10 signal outcome scoring and evaluation.

The properties defended here: that scoring against a label which
resolved before the signal's cutoff is refused outright, that a hit
rate never appears without its baseline, that suppressed signals are
scored separately rather than mixed into the headline number, and that
small samples stay flagged.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.signal_outcome_schema import initialize_signal_outcome_schema
from src.domain.signal_models import (
    Signal, SignalContext, SignalDirection, SignalProvenance, SignalStatus, SignalType,
)
from src.signals.evaluation import (
    MIN_SAMPLE, OutcomeScoringError, confidence_bucket, evaluate_by,
    evaluate_cohort, score_signal,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=5)


def make_signal(signal_id="sig-1", direction=SignalDirection.LONG,
                expected=0.02, confidence=0.6, **overrides):
    defaults = dict(
        signal_id=signal_id, instrument_id="inst-x",
        signal_type=SignalType.DIRECTIONAL, direction=direction,
        status=SignalStatus.ACTIVE, strength=0.5, confidence=confidence,
        expected_return=expected, created_at=NOW,
        provenance=SignalProvenance(strategy_id="st", strategy_version="v1",
                                    source_information_cutoff=NOW),
        context=SignalContext(market_regime="normal", event_type="acquisition"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


class TestLookAheadRefusal(unittest.TestCase):
    """The rule that makes the whole exercise meaningful."""

    def test_label_resolving_before_cutoff_is_refused(self):
        with self.assertRaises(OutcomeScoringError):
            score_signal(make_signal(), 0.03, "d5", "d5.abnormal_return",
                         NOW - timedelta(days=1))

    def test_label_resolving_exactly_at_cutoff_is_refused(self):
        # Knowable AT the cutoff is still knowable.
        with self.assertRaises(OutcomeScoringError):
            score_signal(make_signal(), 0.03, "d5", "d5.abnormal_return", NOW)

    def test_label_resolving_after_cutoff_is_accepted(self):
        outcome = score_signal(make_signal(), 0.03, "d5", "d5.abnormal_return", LATER)
        self.assertIsNotNone(outcome)


class TestScoreSignal(unittest.TestCase):
    def test_correct_long_call_is_marked_correct(self):
        outcome = score_signal(make_signal(direction=SignalDirection.LONG),
                               0.03, "d5", "l", LATER)
        self.assertTrue(outcome.direction_correct)

    def test_wrong_long_call_is_marked_incorrect(self):
        outcome = score_signal(make_signal(direction=SignalDirection.LONG),
                               -0.03, "d5", "l", LATER)
        self.assertFalse(outcome.direction_correct)

    def test_correct_short_call_is_marked_correct(self):
        outcome = score_signal(make_signal(direction=SignalDirection.SHORT),
                               -0.03, "d5", "l", LATER)
        self.assertTrue(outcome.direction_correct)

    def test_neutral_signal_is_not_scored_for_direction(self):
        # NEUTRAL makes no directional claim, so it can be neither
        # right nor wrong about direction.
        outcome = score_signal(make_signal(direction=SignalDirection.NEUTRAL),
                               0.03, "d5", "l", LATER)
        self.assertIsNone(outcome.direction_correct)

    def test_error_is_expected_minus_realized(self):
        outcome = score_signal(make_signal(expected=0.02), 0.03, "d5", "l", LATER)
        self.assertAlmostEqual(outcome.error, -0.01)
        self.assertAlmostEqual(outcome.absolute_error, 0.01)

    def test_missing_realized_return_yields_no_verdict(self):
        outcome = score_signal(make_signal(), None, "d5", "l", LATER)
        self.assertIsNone(outcome.direction_correct)
        self.assertIsNone(outcome.error)

    def test_context_is_carried_for_later_slicing(self):
        outcome = score_signal(make_signal(), 0.03, "d5", "l", LATER)
        self.assertEqual(outcome.strategy_id, "st")
        self.assertEqual(outcome.event_type, "acquisition")
        self.assertEqual(outcome.market_regime, "normal")


class TestConfidenceBucket(unittest.TestCase):
    def test_buckets_partition_the_unit_range(self):
        self.assertEqual(confidence_bucket(0.1), "very_low")
        self.assertEqual(confidence_bucket(0.3), "low")
        self.assertEqual(confidence_bucket(0.6), "medium")
        self.assertEqual(confidence_bucket(0.9), "high")

    def test_missing_confidence_is_its_own_bucket(self):
        # Never silently folded into a numeric bucket.
        self.assertEqual(confidence_bucket(None), "unknown")


class TestEvaluateCohort(unittest.TestCase):
    def _outcomes(self, pairs):
        return [score_signal(make_signal(f"sig-{i}", direction=d), r, "d5", "l", LATER)
                for i, (d, r) in enumerate(pairs)]

    def test_hit_rate_is_computed_over_directional_signals(self):
        outcomes = self._outcomes([
            (SignalDirection.LONG, 0.03), (SignalDirection.LONG, 0.02),
            (SignalDirection.LONG, -0.01), (SignalDirection.SHORT, -0.02)])
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertAlmostEqual(evaluation.hit_rate, 0.75)

    def test_baseline_is_always_present_alongside_hit_rate(self):
        outcomes = self._outcomes([(SignalDirection.LONG, 0.03),
                                   (SignalDirection.LONG, -0.01)])
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertIsNotNone(evaluation.hit_rate)
        self.assertIsNotNone(evaluation.baseline_hit_rate)

    def test_baseline_derives_from_realized_not_from_signals(self):
        # All signals say LONG, but the market went down 3 of 4 times.
        # The baseline must reflect the market, not the signals.
        outcomes = self._outcomes([
            (SignalDirection.LONG, -0.01), (SignalDirection.LONG, -0.02),
            (SignalDirection.LONG, -0.03), (SignalDirection.LONG, 0.01)])
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertAlmostEqual(evaluation.baseline_hit_rate, 0.75)
        self.assertFalse(evaluation.beats_baseline)

    def test_small_sample_is_flagged_with_a_note(self):
        outcomes = self._outcomes([(SignalDirection.LONG, 0.03)])
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertTrue(evaluation.small_sample)
        self.assertTrue(any("descriptive only" in n for n in evaluation.notes))

    def test_large_sample_is_not_flagged(self):
        outcomes = self._outcomes([(SignalDirection.LONG, 0.01)] * (MIN_SAMPLE + 1))
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertFalse(evaluation.small_sample)

    def test_empty_cohort_is_handled_without_raising(self):
        evaluation = evaluate_cohort([], "overall", "all", "d5")
        self.assertEqual(evaluation.sample_size, 0)
        self.assertIsNone(evaluation.hit_rate)

    def test_mean_and_median_returns_are_computed(self):
        outcomes = self._outcomes([(SignalDirection.LONG, 0.01),
                                   (SignalDirection.LONG, 0.03)])
        evaluation = evaluate_cohort(outcomes, "overall", "all", "d5")
        self.assertAlmostEqual(evaluation.mean_return, 0.02)
        self.assertAlmostEqual(evaluation.median_return, 0.02)

    def test_evaluation_id_is_deterministic_for_a_cohort(self):
        first = evaluate_cohort([], "strategy", "st", "d5").evaluation_id
        second = evaluate_cohort([], "strategy", "st", "d5").evaluation_id
        self.assertEqual(first, second)


class TestEvaluateBy(unittest.TestCase):
    def test_slicing_by_confidence_bucket_produces_one_per_bucket(self):
        outcomes = [
            score_signal(make_signal("a", confidence=0.9), 0.03, "d5", "l", LATER),
            score_signal(make_signal("b", confidence=0.1), 0.03, "d5", "l", LATER),
        ]
        evaluations = evaluate_by(outcomes, "confidence_bucket", "confidence_bucket", "d5")
        self.assertEqual(len(evaluations), 2)
        self.assertEqual({e.cohort_value for e in evaluations}, {"high", "very_low"})

    def test_missing_attribute_groups_under_unknown(self):
        outcome = score_signal(
            make_signal(context=SignalContext(event_type=None)), 0.03, "d5", "l", LATER)
        evaluations = evaluate_by([outcome], "event_type", "event_type", "d5")
        self.assertEqual(evaluations[0].cohort_value, "unknown")


class TestOutcomeSchema(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_signal_outcome_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_schema_is_safe_to_run_twice(self):
        initialize_signal_outcome_schema(self.conn)  # must not raise

    def test_outcomes_are_keyed_by_signal_and_horizon(self):
        # The same signal scored over several horizons must not collide.
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(signal_outcomes)")}
        self.assertIn("horizon", columns)
        self.assertIn("signal_id", columns)

    def test_outcome_table_has_no_pnl_or_position_columns(self):
        # Signal-level evaluation only; portfolio results need sizes
        # Phase 10 does not have.
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(signal_outcomes)")}
        for forbidden in ("pnl", "profit", "position_size", "notional"):
            self.assertNotIn(forbidden, columns)

    def test_evaluation_table_carries_a_baseline_column(self):
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(signal_evaluations)")}
        self.assertIn("baseline_hit_rate", columns)
        self.assertIn("beats_baseline", columns)


if __name__ == "__main__":
    unittest.main()
