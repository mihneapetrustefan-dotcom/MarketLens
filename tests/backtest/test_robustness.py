"""
tests/backtest/test_robustness.py
--------------------------------------
Sensitivity, walk-forward and resampling (spec §60-§65, §48).

The most important assertions here are the refusals: that the bootstrap
declines to produce an interval from a handful of trades, and that it
carries its independence assumption with it. A resampled confidence
interval is the easiest number in this whole phase to quote out of
context.
"""

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.robustness import (
    MIN_TRADES_FOR_BOOTSTRAP, RobustnessHarness, bootstrap_trades,
    walk_forward_configurations, walk_forward_windows,
)
from src.domain.backtest_models import (
    BacktestConfiguration, BacktestResult, BacktestStatus, PerformanceMetrics,
    RunIdentity, SlippageMethod,
)
from tests.backtest.helpers import END, START, make_config


def fake_result(config: BacktestConfiguration, total_return: float,
                trades: int = 40) -> BacktestResult:
    """A stand-in result so the harness can be tested without a database."""
    metrics = PerformanceMetrics(total_return=total_return, total_trades=trades)
    return BacktestResult(
        run_id=f"run-{config.fingerprint()}", backtest_id="bt",
        status=BacktestStatus.COMPLETED, configuration=config,
        identity=RunIdentity(backtest_id="bt", run_id="r",
                             config_fingerprint=config.fingerprint()),
        metrics=metrics)


class TestCostSensitivity(unittest.TestCase):
    def setUp(self):
        # Return falls as commission rises — a fragile-looking strategy.
        self.harness = RobustnessHarness(
            lambda config: fake_result(
                config, 0.20 - config.costs.commission_bps * 0.015))

    def test_one_scenario_per_level(self):
        report = self.harness.cost_sensitivity(make_config(), (0.0, 5.0, 10.0))
        self.assertEqual(len(report.scenarios), 3)

    def test_each_scenario_is_a_distinct_configuration(self):
        report = self.harness.cost_sensitivity(make_config(), (0.0, 5.0, 20.0))
        fingerprints = {s.result.identity.config_fingerprint
                        for s in report.scenarios}
        self.assertEqual(len(fingerprints), 3)

    def test_a_sign_flip_is_reported_as_fragile(self):
        report = self.harness.cost_sensitivity(make_config(), (0.0, 20.0))
        self.assertTrue(report.flips_sign)
        self.assertTrue(report.is_fragile)

    def test_a_stable_strategy_is_not_fragile(self):
        harness = RobustnessHarness(lambda config: fake_result(config, 0.25))
        report = harness.cost_sensitivity(make_config(), (0.0, 5.0, 20.0))
        self.assertFalse(report.flips_sign)
        self.assertFalse(report.is_fragile)

    def test_spread_measures_the_range(self):
        report = self.harness.cost_sensitivity(make_config(), (0.0, 10.0))
        self.assertAlmostEqual(report.spread, 0.15, places=6)

    def test_the_summary_records_the_method(self):
        report = self.harness.cost_sensitivity(make_config(), (0.0, 5.0))
        self.assertIn("re-run", report.summary()["note"])


class TestSlippageSensitivity(unittest.TestCase):
    def test_zero_slippage_uses_the_none_method(self):
        captured = []

        def runner(config):
            captured.append(config.slippage.method)
            return fake_result(config, 0.1)

        RobustnessHarness(runner).slippage_sensitivity(make_config(), (0.0, 10.0))
        self.assertEqual(captured[0], SlippageMethod.NONE)
        self.assertEqual(captured[1], SlippageMethod.FIXED_BPS)


class TestParameterSensitivity(unittest.TestCase):
    def test_one_run_per_value(self):
        harness = RobustnessHarness(lambda config: fake_result(config, 0.1))
        report = harness.parameter_sensitivity(
            make_config(), "sizing_target_weight", [0.02, 0.05, 0.10])
        self.assertEqual(len(report.scenarios), 3)

    def test_the_parameter_is_actually_applied(self):
        captured = []

        def runner(config):
            captured.append(config.sizing_target_weight)
            return fake_result(config, 0.1)

        RobustnessHarness(runner).parameter_sensitivity(
            make_config(), "sizing_target_weight", [0.02, 0.08])
        self.assertEqual(captured, [0.02, 0.08])

    def test_an_unknown_field_is_rejected(self):
        harness = RobustnessHarness(lambda config: fake_result(config, 0.1))
        with self.assertRaises(ValueError):
            harness.parameter_sensitivity(make_config(), "not_a_field", [1])


class TestPeriodSensitivity(unittest.TestCase):
    def test_sub_periods_become_separate_runs(self):
        harness = RobustnessHarness(lambda config: fake_result(config, 0.1))
        midpoint = START + (END - START) / 2
        report = harness.period_sensitivity(
            make_config(), [(START, midpoint), (midpoint, END)])
        self.assertEqual(len(report.scenarios), 2)

    def test_an_inverted_window_is_skipped(self):
        harness = RobustnessHarness(lambda config: fake_result(config, 0.1))
        report = harness.period_sensitivity(make_config(), [(END, START)])
        self.assertEqual(report.scenarios, [])


class TestWalkForward(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_windows_are_generated(self):
        windows = walk_forward_windows(self.start, self.end)
        self.assertTrue(windows)

    def test_test_never_precedes_train(self):
        for window in walk_forward_windows(self.start, self.end):
            self.assertGreaterEqual(window["test_start"], window["train_end"])

    def test_windows_move_forward(self):
        windows = walk_forward_windows(self.start, self.end)
        starts = [w["test_start"] for w in windows]
        self.assertEqual(starts, sorted(starts))

    def test_only_test_windows_are_backtested(self):
        """Spec §47 — the in-sample period must stay distinguishable."""
        windows = walk_forward_windows(self.start, self.end)
        pairs = walk_forward_configurations(make_config(), windows)
        self.assertEqual(len(pairs), len(windows))
        for window, config in pairs:
            self.assertEqual(config.start, window["test_start"])
            self.assertEqual(config.end, window["test_end"])

    def test_each_fold_has_its_own_fingerprint(self):
        windows = walk_forward_windows(self.start, self.end)
        pairs = walk_forward_configurations(make_config(), windows)
        fingerprints = {config.fingerprint() for _, config in pairs}
        self.assertEqual(len(fingerprints), len(pairs))


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.pnls = [10.0, -5.0, 20.0, -8.0, 15.0] * 10   # 50 trades

    def test_too_few_trades_refuses_to_produce_an_interval(self):
        result = bootstrap_trades([1.0, -1.0, 2.0], iterations=100)
        self.assertTrue(result.insufficient_data)
        self.assertIsNone(result.percentile_5)
        self.assertIn(str(MIN_TRADES_FOR_BOOTSTRAP), result.note)

    def test_a_sufficient_sample_produces_percentiles(self):
        result = bootstrap_trades(self.pnls, iterations=200, seed=7)
        self.assertFalse(result.insufficient_data)
        self.assertIsNotNone(result.percentile_5)
        self.assertIsNotNone(result.percentile_95)
        self.assertLessEqual(result.percentile_5, result.percentile_50)
        self.assertLessEqual(result.percentile_50, result.percentile_95)

    def test_the_independence_assumption_travels_with_the_number(self):
        result = bootstrap_trades(self.pnls, iterations=100, seed=1)
        self.assertIn("independent", result.assumption)
        self.assertIn("too narrow", result.assumption)

    def test_the_seed_makes_it_reproducible(self):
        first = bootstrap_trades(self.pnls, iterations=200, seed=42)
        second = bootstrap_trades(self.pnls, iterations=200, seed=42)
        self.assertAlmostEqual(first.mean_total_pnl, second.mean_total_pnl)
        self.assertAlmostEqual(first.percentile_5, second.percentile_5)

    def test_a_different_seed_gives_a_different_draw(self):
        first = bootstrap_trades(self.pnls, iterations=200, seed=1)
        second = bootstrap_trades(self.pnls, iterations=200, seed=2)
        self.assertNotAlmostEqual(first.mean_total_pnl, second.mean_total_pnl)

    def test_the_seed_and_method_are_recorded(self):
        result = bootstrap_trades(self.pnls, iterations=150, seed=9)
        self.assertEqual(result.seed, 9)
        self.assertEqual(result.iterations, 150)
        self.assertEqual(result.method, "iid trade bootstrap")

    def test_probability_of_loss_is_a_fraction(self):
        result = bootstrap_trades(self.pnls, iterations=200, seed=3)
        self.assertGreaterEqual(result.probability_of_loss, 0.0)
        self.assertLessEqual(result.probability_of_loss, 1.0)

    def test_an_all_losing_sample_reports_high_loss_probability(self):
        result = bootstrap_trades([-5.0] * 40, iterations=200, seed=5)
        self.assertAlmostEqual(result.probability_of_loss, 1.0)


if __name__ == "__main__":
    unittest.main()
