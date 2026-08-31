"""
tests/portfolio/test_analytics.py
--------------------------------------
Tests for the risk measurements.

Two themes run through these. First, the arithmetic is checked against
values computed by hand rather than against whatever the code happens
to return, so a refactor that changes a result has to justify itself.
Second — and more important — every "not enough data" path is tested
explicitly, because the dangerous failure here is not a wrong number
but a confident number computed from four observations.
"""

import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.portfolio import analytics
from src.portfolio.analytics import (
    MIN_CORRELATION_OBSERVATIONS, MIN_VAR_OBSERVATIONS, MIN_VOLATILITY_OBSERVATIONS,
    TRADING_DAYS_PER_YEAR, align_return_series, compute_concentration,
    compute_correlation_summary, compute_drawdown, compute_liquidity_participation,
    compute_value_at_risk, compute_volatility, pearson_correlation, percentile,
    portfolio_return_series, sample_stdev,
)
from tests.portfolio.helpers import AS_OF, make_snapshot, make_valuation


def days(count, start=None):
    base = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [base + timedelta(days=i) for i in range(count)]


class TestStatisticsHelpers(unittest.TestCase):
    def test_sample_stdev_matches_hand_computation(self):
        # values 2,4,4,4,5,5,7,9 -> sample stdev = sqrt(32/7)
        values = [2, 4, 4, 4, 5, 5, 7, 9]
        self.assertAlmostEqual(sample_stdev(values), math.sqrt(32 / 7), places=10)

    def test_sample_stdev_needs_two_points(self):
        self.assertIsNone(sample_stdev([1.0]))
        self.assertIsNone(sample_stdev([]))

    def test_perfect_positive_correlation_is_one(self):
        self.assertAlmostEqual(pearson_correlation([1, 2, 3], [2, 4, 6]), 1.0)

    def test_perfect_negative_correlation_is_minus_one(self):
        self.assertAlmostEqual(pearson_correlation([1, 2, 3], [6, 4, 2]), -1.0)

    def test_constant_series_has_undefined_correlation_not_zero(self):
        """0.0 would read as 'independent', which is a claim the data cannot support."""
        self.assertIsNone(pearson_correlation([1, 1, 1], [1, 2, 3]))

    def test_mismatched_lengths_return_none(self):
        self.assertIsNone(pearson_correlation([1, 2], [1, 2, 3]))

    def test_percentile_endpoints(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(percentile(values, 0.0), 1.0)
        self.assertEqual(percentile(values, 1.0), 5.0)

    def test_percentile_interpolates(self):
        self.assertAlmostEqual(percentile([0.0, 10.0], 0.5), 5.0)

    def test_percentile_of_empty_is_none(self):
        self.assertIsNone(percentile([], 0.5))


class TestVolatility(unittest.TestCase):
    def test_annualizes_by_sqrt_252(self):
        returns = [0.01, -0.01] * 30
        estimate = compute_volatility(returns, 365)
        daily = sample_stdev(returns)
        self.assertAlmostEqual(estimate.value, daily * math.sqrt(TRADING_DAYS_PER_YEAR))

    def test_below_minimum_observations_is_insufficient_not_computed(self):
        estimate = compute_volatility([0.01] * (MIN_VOLATILITY_OBSERVATIONS - 1), 365)
        self.assertTrue(estimate.insufficient_data)
        self.assertIsNone(estimate.value)

    def test_at_minimum_observations_is_computed(self):
        returns = [0.01, -0.01] * (MIN_VOLATILITY_OBSERVATIONS // 2)
        estimate = compute_volatility(returns, 365)
        self.assertFalse(estimate.insufficient_data)
        self.assertIsNotNone(estimate.value)

    def test_zero_variance_series_reports_zero_volatility(self):
        estimate = compute_volatility([0.0] * 50, 365)
        self.assertEqual(estimate.value, 0.0)

    def test_method_and_convention_are_recorded(self):
        estimate = compute_volatility([0.01, -0.01] * 30, 365)
        self.assertEqual(estimate.return_frequency, "daily")
        self.assertAlmostEqual(estimate.annualization_factor,
                               math.sqrt(TRADING_DAYS_PER_YEAR))
        self.assertEqual(estimate.observations, 60)

    def test_empty_series_is_insufficient(self):
        self.assertTrue(compute_volatility([], 365).insufficient_data)


class TestValueAtRisk(unittest.TestCase):
    def test_below_minimum_observations_is_insufficient(self):
        result = compute_value_at_risk([0.01] * (MIN_VAR_OBSERVATIONS - 1))
        self.assertTrue(result.insufficient_data)
        self.assertIsNone(result.value)

    def test_var_is_a_positive_fraction(self):
        returns = [-0.05] * 15 + [0.01] * 85
        result = compute_value_at_risk(returns, confidence_level=0.95)
        self.assertFalse(result.insufficient_data)
        self.assertGreater(result.value, 0)

    def test_var_is_zero_when_the_tail_holds_no_losses(self):
        """
        With only 5 losing days in 100, the interpolated 5% quantile
        lands in positive territory. A 95% VaR of zero is then the
        correct reading — "on 95% of past days this book did not lose"
        — not a computation that failed. Pinned because the floor that
        produces it could otherwise look like an accident.
        """
        result = compute_value_at_risk([-0.05] * 5 + [0.01] * 95,
                                       confidence_level=0.95)
        self.assertFalse(result.insufficient_data)
        self.assertEqual(result.value, 0.0)

    def test_expected_shortfall_is_at_least_var(self):
        """ES averages the tail beyond VaR, so it can never be the smaller number."""
        returns = [-0.10, -0.08, -0.06] + [0.01] * 97
        result = compute_value_at_risk(returns)
        self.assertGreaterEqual(result.expected_shortfall, result.value)

    def test_all_positive_returns_floor_var_at_zero_not_negative(self):
        result = compute_value_at_risk([0.01] * 100)
        self.assertGreaterEqual(result.value, 0.0)

    def test_horizon_scaling_uses_sqrt_and_is_disclosed(self):
        returns = [-0.05] * 5 + [0.01] * 95
        one_day = compute_value_at_risk(returns, horizon_days=1)
        four_day = compute_value_at_risk(returns, horizon_days=4)
        self.assertAlmostEqual(four_day.value, one_day.value * 2.0, places=10)
        self.assertIn("sqrt", four_day.note)

    def test_confidence_level_is_recorded(self):
        result = compute_value_at_risk([0.01] * 100, confidence_level=0.99)
        self.assertEqual(result.confidence_level, 0.99)

    def test_a_wider_confidence_level_reports_at_least_as_much_risk(self):
        returns = [-0.20, -0.10, -0.05] + [0.01] * 97
        at_95 = compute_value_at_risk(returns, confidence_level=0.95)
        at_99 = compute_value_at_risk(returns, confidence_level=0.99)
        self.assertGreaterEqual(at_99.value, at_95.value)


class TestDrawdown(unittest.TestCase):
    def test_needs_at_least_two_observations(self):
        curve = [(days(1)[0], 100.0)]
        self.assertTrue(compute_drawdown(curve).insufficient_data)

    def test_monotonic_rise_has_no_drawdown(self):
        timestamps = days(4)
        metrics = compute_drawdown(list(zip(timestamps, [100.0, 110.0, 120.0, 130.0])))
        self.assertEqual(metrics.max_drawdown, 0.0)
        self.assertEqual(metrics.current_drawdown, 0.0)

    def test_peak_to_trough_is_measured_from_the_running_peak(self):
        timestamps = days(4)
        metrics = compute_drawdown(list(zip(timestamps, [100.0, 200.0, 150.0, 180.0])))
        self.assertAlmostEqual(metrics.max_drawdown, -0.25)     # 200 -> 150
        self.assertAlmostEqual(metrics.current_drawdown, -0.10)  # 200 -> 180
        self.assertEqual(metrics.peak_equity, 200.0)
        self.assertEqual(metrics.trough_equity, 150.0)

    def test_recovery_keeps_max_drawdown_but_clears_current(self):
        timestamps = days(4)
        metrics = compute_drawdown(list(zip(timestamps, [100.0, 50.0, 100.0, 100.0])))
        self.assertAlmostEqual(metrics.max_drawdown, -0.50)
        self.assertAlmostEqual(metrics.current_drawdown, 0.0)

    def test_unordered_input_is_sorted_before_measuring(self):
        timestamps = days(3)
        shuffled = [(timestamps[2], 150.0), (timestamps[0], 100.0), (timestamps[1], 200.0)]
        metrics = compute_drawdown(shuffled)
        self.assertAlmostEqual(metrics.max_drawdown, -0.25)

    def test_none_equity_values_are_ignored(self):
        timestamps = days(3)
        metrics = compute_drawdown(
            [(timestamps[0], 100.0), (timestamps[1], None), (timestamps[2], 80.0)])
        self.assertEqual(metrics.observations, 2)
        self.assertAlmostEqual(metrics.max_drawdown, -0.20)


class TestConcentration(unittest.TestCase):
    def test_single_position_is_fully_concentrated(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=0.0)
        metrics = compute_concentration(snapshot)
        self.assertAlmostEqual(metrics.largest_weight, 1.0)
        self.assertAlmostEqual(metrics.hhi, 1.0)
        self.assertAlmostEqual(metrics.effective_positions, 1.0)

    def test_equal_weights_give_effective_positions_equal_to_count(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0),
            make_valuation("b", 10.0, 10.0),
            make_valuation("c", 10.0, 10.0),
            make_valuation("d", 10.0, 10.0),
        ], cash=0.0)
        metrics = compute_concentration(snapshot)
        self.assertAlmostEqual(metrics.effective_positions, 4.0, places=6)
        self.assertAlmostEqual(metrics.hhi, 0.25, places=6)

    def test_cash_lowers_hhi_but_not_effective_position_count(self):
        """
        The two denominators answer different questions: HHI measures
        exposure against equity (cash genuinely de-risks), while
        effective breadth describes the invested book.
        """
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0),
            make_valuation("b", 10.0, 10.0),
        ], cash=800.0)
        metrics = compute_concentration(snapshot)
        self.assertLess(metrics.hhi, 0.5)
        self.assertAlmostEqual(metrics.effective_positions, 2.0, places=6)
        self.assertAlmostEqual(metrics.invested_weight, 0.2, places=6)

    def test_effective_positions_never_exceeds_position_count(self):
        snapshot = make_snapshot([
            make_valuation("a", 1.0, 10.0),
            make_valuation("b", 1.0, 10.0),
        ], cash=10_000.0)
        metrics = compute_concentration(snapshot)
        self.assertLessEqual(metrics.effective_positions, metrics.position_count)

    def test_short_counts_as_concentration_rather_than_offsetting(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0),
            make_valuation("b", -10.0, 10.0),
        ], cash=1000.0)
        metrics = compute_concentration(snapshot)
        self.assertAlmostEqual(metrics.invested_weight, 0.2, places=6)

    def test_empty_portfolio_reports_no_concentration(self):
        metrics = compute_concentration(make_snapshot([], cash=100.0))
        self.assertIsNone(metrics.hhi)
        self.assertEqual(metrics.position_count, 0)

    def test_zero_equity_yields_no_metrics_rather_than_division_error(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0),
            make_valuation("b", -10.0, 10.0),
        ], cash=0.0)
        metrics = compute_concentration(snapshot)
        self.assertIsNone(metrics.hhi)


class TestAlignment(unittest.TestCase):
    def test_intersects_dates_rather_than_filling_gaps(self):
        timestamps = days(5)
        series = {
            "a": [(timestamps[i], 0.01) for i in range(5)],
            "b": [(timestamps[i], 0.02) for i in (0, 2, 4)],
        }
        dates, aligned = align_return_series(series)
        self.assertEqual(len(dates), 3)
        self.assertEqual(len(aligned["a"]), 3)
        self.assertEqual(len(aligned["b"]), 3)

    def test_no_overlap_returns_nothing(self):
        early, late = days(2), days(2, datetime(2026, 6, 1, tzinfo=timezone.utc))
        dates, aligned = align_return_series({
            "a": [(early[0], 0.01)], "b": [(late[0], 0.02)]})
        self.assertEqual(dates, [])
        self.assertEqual(aligned, {})

    def test_alignment_is_by_calendar_date_not_exact_timestamp(self):
        """Equity candles stamp 04:00Z and crypto 00:00Z; same day must still align."""
        base = datetime(2026, 3, 2, tzinfo=timezone.utc)
        series = {
            "equity": [(base.replace(hour=4), 0.01)],
            "crypto": [(base.replace(hour=0), 0.02)],
        }
        dates, aligned = align_return_series(series)
        self.assertEqual(len(dates), 1)
        self.assertEqual(len(aligned["equity"]), 1)


class TestPortfolioReturnSeries(unittest.TestCase):
    def test_combines_with_signed_weights(self):
        timestamps = days(3)
        series = {
            "a": [(t, 0.02) for t in timestamps],
            "b": [(t, -0.02) for t in timestamps],
        }
        returns, count = portfolio_return_series({"a": 0.5, "b": 0.5}, series)
        self.assertEqual(count, 3)
        for value in returns:
            self.assertAlmostEqual(value, 0.0)

    def test_a_short_position_flips_the_sign_of_its_contribution(self):
        timestamps = days(3)
        series = {"a": [(t, 0.02) for t in timestamps]}
        long_returns, _ = portfolio_return_series({"a": 1.0}, series)
        short_returns, _ = portfolio_return_series({"a": -1.0}, series)
        self.assertAlmostEqual(long_returns[0], -short_returns[0])

    def test_weights_are_normalized_over_instruments_with_history(self):
        timestamps = days(3)
        series = {"a": [(t, 0.10) for t in timestamps]}
        # "b" has no history at all; the measurable part is 100% "a".
        returns, _ = portfolio_return_series({"a": 0.25, "b": 0.25}, series)
        self.assertAlmostEqual(returns[0], 0.10)

    def test_no_history_yields_no_series(self):
        returns, count = portfolio_return_series({"a": 1.0}, {})
        self.assertEqual((returns, count), ([], 0))

    def test_empty_weights_yield_no_series(self):
        timestamps = days(3)
        returns, count = portfolio_return_series(
            {}, {"a": [(t, 0.01) for t in timestamps]})
        self.assertEqual(count, 0)


class TestCorrelationSummary(unittest.TestCase):
    def test_thin_pairs_are_counted_as_insufficient_not_averaged_in(self):
        timestamps = days(5)
        series = {"a": [(t, 0.01) for t in timestamps],
                  "b": [(t, 0.02) for t in timestamps]}
        summary = compute_correlation_summary(series)
        self.assertEqual(summary.computed_pairs, 0)
        self.assertEqual(summary.insufficient_pairs, 1)
        self.assertIsNone(summary.average_correlation)

    def test_identical_series_correlate_at_one_and_are_flagged(self):
        count = MIN_CORRELATION_OBSERVATIONS + 5
        timestamps = days(count)
        values = [0.01 if i % 2 else -0.01 for i in range(count)]
        series = {"a": list(zip(timestamps, values)),
                  "b": list(zip(timestamps, values))}
        summary = compute_correlation_summary(series)
        self.assertEqual(summary.computed_pairs, 1)
        self.assertAlmostEqual(summary.max_correlation, 1.0, places=6)
        self.assertEqual(len(summary.highly_correlated_pairs), 1)

    def test_single_instrument_has_no_pairs(self):
        timestamps = days(50)
        summary = compute_correlation_summary({"a": [(t, 0.01) for t in timestamps]})
        self.assertEqual(summary.computed_pairs, 0)

    def test_minimum_observations_used_is_reported(self):
        count = MIN_CORRELATION_OBSERVATIONS + 2
        timestamps = days(count)
        values = [0.01 if i % 3 else -0.02 for i in range(count)]
        series = {"a": list(zip(timestamps, values)),
                  "b": list(zip(timestamps, list(reversed(values))))}
        summary = compute_correlation_summary(series)
        self.assertEqual(summary.min_observations_used, count)


class TestLiquidity(unittest.TestCase):
    class _Point:
        def __init__(self, volume):
            self.volume = volume

    def test_participation_is_quantity_over_average_volume(self):
        points = [self._Point(1000.0) for _ in range(20)]
        self.assertAlmostEqual(
            compute_liquidity_participation(100.0, points), 0.10)

    def test_missing_volume_returns_none_rather_than_assuming_liquid(self):
        points = [self._Point(None) for _ in range(20)]
        self.assertIsNone(compute_liquidity_participation(100.0, points))

    def test_zero_volume_returns_none(self):
        points = [self._Point(0.0) for _ in range(20)]
        self.assertIsNone(compute_liquidity_participation(100.0, points))

    def test_short_quantity_uses_absolute_size(self):
        points = [self._Point(1000.0) for _ in range(20)]
        self.assertAlmostEqual(
            compute_liquidity_participation(-100.0, points), 0.10)


if __name__ == "__main__":
    unittest.main()
