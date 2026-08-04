"""
test_time_decay.py
----------------------
Unit tests for Time Decay v1 (time_decay.py).

TESTING STRATEGY:
Every test passes an explicit `reference_time`, so results are fully
deterministic and never depend on real wall-clock time at test-run
time.
"""

import unittest
from datetime import datetime, timedelta, timezone

from time_decay import TimeDecayCalculator


class TestComputeWeight(unittest.TestCase):
    def setUp(self):
        self.reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        self.calculator = TimeDecayCalculator(half_life_hours=72.0)

    def test_weight_at_zero_age_is_one(self):
        weight = self.calculator.compute_weight(self.reference, reference_time=self.reference)
        self.assertAlmostEqual(weight, 1.0, places=6)

    def test_weight_at_exactly_one_half_life_is_half(self):
        collected_at = self.reference - timedelta(hours=72)
        weight = self.calculator.compute_weight(collected_at, reference_time=self.reference)
        self.assertAlmostEqual(weight, 0.5, places=6)

    def test_weight_at_two_half_lives_is_quarter(self):
        collected_at = self.reference - timedelta(hours=144)
        weight = self.calculator.compute_weight(collected_at, reference_time=self.reference)
        self.assertAlmostEqual(weight, 0.25, places=6)

    def test_weight_decreases_monotonically_with_age(self):
        w_1h = self.calculator.compute_weight(self.reference - timedelta(hours=1), reference_time=self.reference)
        w_24h = self.calculator.compute_weight(self.reference - timedelta(hours=24), reference_time=self.reference)
        w_100h = self.calculator.compute_weight(self.reference - timedelta(hours=100), reference_time=self.reference)
        self.assertGreater(w_1h, w_24h)
        self.assertGreater(w_24h, w_100h)

    def test_missing_timestamp_returns_full_weight(self):
        self.assertEqual(self.calculator.compute_weight(None, reference_time=self.reference), 1.0)

    def test_malformed_timestamp_returns_full_weight(self):
        self.assertEqual(self.calculator.compute_weight("not-a-date", reference_time=self.reference), 1.0)

    def test_future_timestamp_is_clipped_to_full_weight(self):
        future = self.reference + timedelta(hours=10)
        self.assertEqual(self.calculator.compute_weight(future, reference_time=self.reference), 1.0)

    def test_accepts_iso_string_and_datetime_equivalently(self):
        collected_at_dt = self.reference - timedelta(hours=36)
        collected_at_str = collected_at_dt.isoformat()
        w_dt = self.calculator.compute_weight(collected_at_dt, reference_time=self.reference)
        w_str = self.calculator.compute_weight(collected_at_str, reference_time=self.reference)
        self.assertAlmostEqual(w_dt, w_str, places=6)


class TestHalfLifeConfiguration(unittest.TestCase):
    def test_default_half_life_matches_documented_value(self):
        calculator = TimeDecayCalculator()
        self.assertEqual(calculator.half_life_hours, 480.0)

    def test_default_half_life_gives_roughly_35_percent_weight_after_one_month(self):
        reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        calculator = TimeDecayCalculator()  # uses the 480h (20-day) default
        one_month_ago = reference - timedelta(days=30)
        weight = calculator.compute_weight(one_month_ago, reference_time=reference)
        self.assertAlmostEqual(weight, 0.35, delta=0.05)

    def test_shorter_half_life_decays_faster(self):
        reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        collected_at = reference - timedelta(hours=24)

        fast_decay = TimeDecayCalculator(half_life_hours=12.0)
        slow_decay = TimeDecayCalculator(half_life_hours=200.0)

        w_fast = fast_decay.compute_weight(collected_at, reference_time=reference)
        w_slow = slow_decay.compute_weight(collected_at, reference_time=reference)
        self.assertLess(w_fast, w_slow)

    def test_zero_or_negative_half_life_raises(self):
        with self.assertRaises(ValueError):
            TimeDecayCalculator(half_life_hours=0)
        with self.assertRaises(ValueError):
            TimeDecayCalculator(half_life_hours=-5)


class TestCategoryHalfLives(unittest.TestCase):
    """Tests for per-category half-life overrides."""

    def setUp(self):
        self.reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)

    def test_category_override_is_used_when_present(self):
        calculator = TimeDecayCalculator(half_life_hours=480.0, category_half_lives={"crypto": 120.0})
        collected_at = self.reference - timedelta(hours=120)
        weight = calculator.compute_weight(collected_at, reference_time=self.reference, category="crypto")
        self.assertAlmostEqual(weight, 0.5, places=6)  # exactly one crypto half-life

    def test_unmatched_category_falls_back_to_default(self):
        calculator = TimeDecayCalculator(half_life_hours=480.0, category_half_lives={"crypto": 120.0})
        collected_at = self.reference - timedelta(hours=480)
        weight = calculator.compute_weight(collected_at, reference_time=self.reference, category="stocks")
        self.assertAlmostEqual(weight, 0.5, places=6)  # exactly one DEFAULT half-life

    def test_no_category_given_falls_back_to_default(self):
        calculator = TimeDecayCalculator(half_life_hours=480.0, category_half_lives={"crypto": 120.0})
        collected_at = self.reference - timedelta(hours=480)
        weight = calculator.compute_weight(collected_at, reference_time=self.reference)
        self.assertAlmostEqual(weight, 0.5, places=6)

    def test_crypto_decays_faster_than_stocks_for_same_age(self):
        calculator = TimeDecayCalculator(half_life_hours=480.0, category_half_lives={"crypto": 120.0})
        collected_at = self.reference - timedelta(hours=200)
        crypto_weight = calculator.compute_weight(collected_at, reference_time=self.reference, category="crypto")
        stock_weight = calculator.compute_weight(collected_at, reference_time=self.reference, category="stocks")
        self.assertLess(crypto_weight, stock_weight)

    def test_invalid_category_half_life_raises(self):
        with self.assertRaises(ValueError):
            TimeDecayCalculator(category_half_lives={"crypto": 0})
        with self.assertRaises(ValueError):
            TimeDecayCalculator(category_half_lives={"crypto": -10})

    def test_no_category_half_lives_behaves_exactly_as_before(self):
        # A plain TimeDecayCalculator() (no category_half_lives passed)
        # must behave identically whether or not a category is supplied.
        calculator = TimeDecayCalculator()
        collected_at = self.reference - timedelta(hours=100)
        weight_no_category = calculator.compute_weight(collected_at, reference_time=self.reference)
        weight_with_category = calculator.compute_weight(collected_at, reference_time=self.reference, category="crypto")
        self.assertEqual(weight_no_category, weight_with_category)


if __name__ == "__main__":
    unittest.main(verbosity=2)
