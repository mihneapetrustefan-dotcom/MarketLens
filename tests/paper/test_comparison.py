"""
tests/paper/test_comparison.py
-----------------------------------
Paper-versus-backtest comparison (Phase 13, spec 65-67, 83, 84).

The tests that matter most here are not the arithmetic ones. They are
the tests that pin down what the module REFUSES to say: that a short
paper period is never conclusive, that a return difference is never
treated as evidence, and that a slippage gap is reported as a
configuration bug rather than as a market observation.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.paper.comparison import (
    DRIFT_THRESHOLD, MIN_TRADES_FOR_COMPARISON, ComparisonReport,
    MetricComparison, compare, detect_drift, paper_metrics_from,
)

AT = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


def backtest_metrics(**overrides):
    base = dict(signals_per_day=2.0, fill_rate=0.90, rejection_rate=0.05,
                slippage_bps=5.0, cost_per_trade=12.0, turnover=1.5,
                avg_holding_days=10.0, total_return=0.18, win_rate=0.55,
                max_drawdown=-0.08)
    base.update(overrides)
    return base


def paper_metrics(**overrides):
    base = dict(trades=40, days=90, signals_per_day=2.1, fill_rate=0.88,
                rejection_rate=0.06, slippage_bps=5.1, cost_per_trade=12.2,
                turnover=1.4, avg_holding_days=11.0, total_return=0.03,
                win_rate=0.50, max_drawdown=-0.05)
    base.update(overrides)
    return base


class TestMetricComparison(unittest.TestCase):

    def test_differences_are_none_when_one_side_is_missing(self):
        only_paper = MetricComparison(metric="fill_rate", paper=0.8)
        self.assertIsNone(only_paper.absolute_difference)
        self.assertIsNone(only_paper.relative_difference)
        self.assertFalse(only_paper.is_measurable)
        self.assertFalse(only_paper.has_drifted)

    def test_a_zero_baseline_has_no_relative_difference(self):
        """
        Dividing by a zero backtest value would report infinite drift on
        a metric that simply never occurred in the backtest.
        """
        metric = MetricComparison(metric="rejection_rate", backtest=0.0, paper=0.02)
        self.assertAlmostEqual(metric.absolute_difference, 0.02)
        self.assertIsNone(metric.relative_difference)
        self.assertFalse(metric.has_drifted)

    def test_relative_difference_uses_the_backtest_as_the_baseline(self):
        metric = MetricComparison(metric="turnover", backtest=2.0, paper=3.0)
        self.assertAlmostEqual(metric.relative_difference, 0.5)

    def test_a_negative_baseline_still_gives_a_signed_direction(self):
        """
        Drawdown is negative. A paper drawdown shallower than the
        backtest's must read as a positive difference, not be flipped by
        the sign of the denominator.
        """
        metric = MetricComparison(metric="max_drawdown", backtest=-0.20, paper=-0.10)
        self.assertGreater(metric.absolute_difference, 0)
        self.assertAlmostEqual(metric.relative_difference, 0.5)

    def test_drift_needs_to_exceed_the_threshold_not_merely_reach_it(self):
        exactly = MetricComparison(metric="turnover", backtest=1.0,
                                   paper=1.0 + DRIFT_THRESHOLD)
        beyond = MetricComparison(metric="turnover", backtest=1.0,
                                  paper=1.0 + DRIFT_THRESHOLD + 0.01)
        self.assertFalse(exactly.has_drifted)
        self.assertTrue(beyond.has_drifted)


class TestConclusiveness(unittest.TestCase):
    """Spec 84: paper trading does not prove a strategy works."""

    def test_a_short_sample_is_never_conclusive(self):
        report = compare("s1", paper_metrics(trades=3, days=10),
                         backtest_metrics(), at=AT)
        self.assertFalse(report.is_conclusive)
        self.assertTrue(any("below the" in n for n in report.notes))

    def test_enough_trades_but_too_few_days_is_still_not_conclusive(self):
        report = compare("s1", paper_metrics(trades=200, days=14),
                         backtest_metrics(), at=AT)
        self.assertFalse(report.is_conclusive)

    def test_enough_days_but_too_few_trades_is_still_not_conclusive(self):
        report = compare("s1", paper_metrics(trades=1, days=400),
                         backtest_metrics(), at=AT)
        self.assertFalse(report.is_conclusive)

    def test_the_bar_for_conclusive_is_explicit_and_high(self):
        report = compare("s1",
                         paper_metrics(trades=MIN_TRADES_FOR_COMPARISON, days=60),
                         backtest_metrics(), at=AT)
        self.assertTrue(report.is_conclusive)

    def test_the_caveat_travels_with_the_summary(self):
        """
        A number lifted out of the summary into a slide must carry its
        own warning, or it stops carrying one at all.
        """
        summary = compare("s1", paper_metrics(), backtest_metrics(), at=AT).summary()
        self.assertIn("caveat", summary)
        self.assertIn("cannot establish", summary["caveat"])
        self.assertIn("conclusive", summary)


class TestReturnIsNeverDiagnostic(unittest.TestCase):

    def test_outcome_metrics_are_reported_but_not_diagnostic(self):
        report = compare("s1", paper_metrics(), backtest_metrics(), at=AT)
        outcomes = {m.metric: m for m in report.metrics
                    if m.metric in ("total_return", "win_rate", "max_drawdown")}
        self.assertEqual(len(outcomes), 3)
        for metric in outcomes.values():
            self.assertFalse(metric.is_diagnostic, metric.metric)
            self.assertTrue(metric.is_measurable)

    def test_a_huge_return_gap_produces_no_drift_finding(self):
        """
        The backtest made 18%, paper lost 20%. That is exactly the
        comparison someone would want to call a verdict, and exactly the
        one the module must refuse to call one.
        """
        report = compare("s1", paper_metrics(total_return=-0.20),
                         backtest_metrics(total_return=0.18), at=AT)
        self.assertEqual([m.metric for m in report.drifted], [])
        self.assertEqual(detect_drift(report), [])

    def test_mechanical_metrics_are_diagnostic(self):
        report = compare("s1", paper_metrics(), backtest_metrics(), at=AT)
        mechanical = {m.metric for m in report.metrics if m.is_diagnostic}
        self.assertIn("fill_rate", mechanical)
        self.assertIn("rejection_rate", mechanical)
        self.assertIn("turnover", mechanical)
        self.assertNotIn("total_return", mechanical)


class TestDriftDetection(unittest.TestCase):

    def test_a_matching_pipeline_reports_no_drift(self):
        report = compare("s1", paper_metrics(), backtest_metrics(), at=AT)
        self.assertEqual(detect_drift(report), [])

    def test_a_collapsed_fill_rate_is_flagged_with_its_causes(self):
        report = compare("s1", paper_metrics(fill_rate=0.20),
                         backtest_metrics(fill_rate=0.90), at=AT)
        findings = {f["metric"]: f for f in detect_drift(report)}
        self.assertIn("fill_rate", findings)
        self.assertEqual(findings["fill_rate"]["direction"], "paper is lower")
        self.assertIn("liquidity", findings["fill_rate"]["likely_causes"])

    def test_no_finding_ever_claims_to_be_conclusive(self):
        report = compare("s1", paper_metrics(fill_rate=0.1, turnover=9.0,
                                             slippage_bps=80.0),
                         backtest_metrics(), at=AT)
        findings = detect_drift(report)
        self.assertTrue(findings)
        for finding in findings:
            self.assertFalse(finding["conclusive"], finding["metric"])

    def test_a_slippage_gap_is_reported_as_a_configuration_bug(self):
        """
        Both phases share the Phase 12 slippage model. A divergence
        therefore cannot be a market observation - it means the two runs
        were configured with different model versions, and calling it
        anything else would send someone hunting for a market
        explanation that does not exist.
        """
        report = compare("s1", paper_metrics(slippage_bps=40.0),
                         backtest_metrics(slippage_bps=5.0), at=AT)
        findings = [f for f in detect_drift(report)
                    if f["metric"] == "slippage_bps"]
        self.assertTrue(any(f["direction"] == "configuration mismatch"
                            for f in findings))
        self.assertTrue(any("VERSIONS" in f["likely_causes"] for f in findings))

    def test_cost_per_trade_is_treated_the_same_way(self):
        report = compare("s1", paper_metrics(cost_per_trade=60.0),
                         backtest_metrics(cost_per_trade=12.0), at=AT)
        findings = [f for f in detect_drift(report)
                    if f["metric"] == "cost_per_trade"]
        self.assertTrue(any(f["direction"] == "configuration mismatch"
                            for f in findings))

    def test_a_missing_backtest_metric_cannot_drift(self):
        report = compare("s1", paper_metrics(),
                         backtest_metrics(avg_holding_days=None), at=AT)
        self.assertNotIn("avg_holding_days", [m.metric for m in report.drifted])


class TestComparisonWithoutABacktest(unittest.TestCase):

    def test_paper_metrics_are_reported_alone(self):
        report = compare("s1", paper_metrics(), backtest=None, at=AT)
        self.assertTrue(any("no backtest run" in n for n in report.notes))
        self.assertEqual(report.measurable, [])
        self.assertEqual(detect_drift(report), [])

    def test_the_paper_side_is_still_carried(self):
        report = compare("s1", paper_metrics(fill_rate=0.42), backtest=None, at=AT)
        fill_rate = next(m for m in report.metrics if m.metric == "fill_rate")
        self.assertAlmostEqual(fill_rate.paper, 0.42)
        self.assertIsNone(fill_rate.backtest)


class TestPaperMetricsFromRecords(unittest.TestCase):
    """Metrics rebuilt from persisted rows, long after the session exited."""

    def snapshots(self, equities, start=AT):
        rows = []
        peak = None
        for index, equity in enumerate(equities):
            peak = equity if peak is None else max(peak, equity)
            rows.append({"at": (start + timedelta(days=index)).isoformat(),
                         "equity": equity,
                         "drawdown": (equity - peak) / peak})
        return rows

    def test_an_empty_session_produces_no_invented_numbers(self):
        metrics = paper_metrics_from([], [], [], initial_capital=100_000.0)
        self.assertEqual(metrics["trades"], 0)
        self.assertEqual(metrics["days"], 0)
        for key in ("fill_rate", "rejection_rate", "slippage_bps",
                    "cost_per_trade", "total_return"):
            self.assertIsNone(metrics[key], key)

    def test_days_come_from_the_snapshot_span(self):
        metrics = paper_metrics_from(
            self.snapshots([100_000.0] * 11), [], [], initial_capital=100_000.0)
        self.assertEqual(metrics["days"], 10)

    def test_fill_and_rejection_rates_are_fractions_of_all_orders(self):
        orders = ([{"state": "filled"}] * 6 + [{"state": "rejected"}] * 2
                  + [{"state": "cancelled"}] * 2)
        metrics = paper_metrics_from([], orders, [], initial_capital=100_000.0)
        self.assertAlmostEqual(metrics["fill_rate"], 0.6)
        self.assertAlmostEqual(metrics["rejection_rate"], 0.2)

    def test_slippage_is_measured_against_the_reference_price(self):
        fills = [{"quantity": 10, "price": 101.0, "reference_price": 100.0,
                  "commission": 5.0},
                 {"quantity": 10, "price": 99.0, "reference_price": 100.0,
                  "commission": 5.0}]
        metrics = paper_metrics_from([], [], fills, initial_capital=100_000.0)
        # Both fills moved 100 bps against the reference; taking the
        # absolute value keeps a buy and a sell from cancelling out into
        # a reported zero.
        self.assertAlmostEqual(metrics["slippage_bps"], 100.0)
        self.assertAlmostEqual(metrics["cost_per_trade"], 5.0)

    def test_a_fill_without_a_reference_price_is_skipped_not_zeroed(self):
        fills = [{"quantity": 10, "price": 101.0, "reference_price": 100.0,
                  "commission": 5.0},
                 {"quantity": 10, "price": 50.0, "reference_price": None,
                  "commission": 5.0}]
        metrics = paper_metrics_from([], [], fills, initial_capital=100_000.0)
        self.assertAlmostEqual(metrics["slippage_bps"], 100.0)

    def test_max_drawdown_is_the_deepest_not_the_last(self):
        metrics = paper_metrics_from(
            self.snapshots([100_000.0, 80_000.0, 95_000.0]), [], [],
            initial_capital=100_000.0)
        self.assertAlmostEqual(metrics["max_drawdown"], -0.20)

    def test_total_return_is_measured_against_initial_capital(self):
        metrics = paper_metrics_from(
            self.snapshots([100_000.0, 110_000.0]), [], [],
            initial_capital=100_000.0)
        self.assertAlmostEqual(metrics["total_return"], 0.10)

    def test_signals_per_day_is_left_for_the_caller(self):
        """
        Signals are not derivable from orders - most signals never reach
        an order. Returning a number here would be an invented one.
        """
        metrics = paper_metrics_from([], [], [], initial_capital=100_000.0)
        self.assertIn("signals_per_day", metrics)
        self.assertIsNone(metrics["signals_per_day"])

    def test_reconstructed_metrics_feed_compare_directly(self):
        fills = [{"quantity": 10, "price": 101.0, "reference_price": 100.0,
                  "commission": 5.0}] * 6
        orders = [{"state": "filled"}] * 6
        metrics = paper_metrics_from(
            self.snapshots([100_000.0] * 5), orders, fills,
            initial_capital=100_000.0)
        report = compare("s1", metrics, backtest_metrics(), at=AT)
        self.assertEqual(report.paper_trades, 6)
        self.assertFalse(report.is_conclusive)


if __name__ == "__main__":
    unittest.main()
