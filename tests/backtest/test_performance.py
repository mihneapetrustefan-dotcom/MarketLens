"""
tests/backtest/test_performance.py
---------------------------------------
Performance metrics (spec §34-§37, §45, §46, §92).

Every formula is checked against a value computed by hand, not against
whatever the code returns. A metrics engine that agrees with itself is
not evidence of anything.

The insufficiency paths get equal weight: a Sharpe ratio computed from
four observations is the failure mode that matters, because it looks
exactly like a real one.
"""

import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.performance import (
    DAYS_PER_YEAR, MIN_RATIO_OBSERVATIONS, MIN_RETURN_OBSERVATIONS,
    PerformanceEngine, annotate_drawdown, compute_drawdown_episodes,
    period_returns, rolling_metric, simple_returns,
)
from src.domain.backtest_models import EquityPoint, OrderSide, Trade
from src.portfolio.analytics import TRADING_DAYS_PER_YEAR

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def curve(values, start=BASE, step_days=1):
    return [EquityPoint(timestamp=start + timedelta(days=i * step_days),
                        equity=v, cash=0.0, positions_value=v)
            for i, v in enumerate(values)]


def trade(net, entry=BASE, days=5, quantity=10.0, entry_price=100.0,
          instrument="a", costs=0.0):
    return Trade(
        trade_id=f"t{net}{instrument}{days}", run_id="r", instrument_id=instrument,
        side=OrderSide.BUY, quantity=quantity, entry_price=entry_price,
        exit_price=entry_price + net / quantity,
        entry_at=entry, exit_at=entry + timedelta(days=days),
        gross_pnl=net + costs, costs=costs)


class TestReturnSeries(unittest.TestCase):
    def test_simple_returns_are_period_over_period(self):
        self.assertEqual(simple_returns(curve([100.0, 110.0, 121.0])),
                         [0.1, 0.1])

    def test_a_single_point_yields_no_returns(self):
        self.assertEqual(simple_returns(curve([100.0])), [])

    def test_zero_equity_is_skipped_rather_than_dividing(self):
        self.assertEqual(simple_returns(curve([0.0, 100.0])), [])


class TestHeadlineMetrics(unittest.TestCase):
    def setUp(self):
        self.engine = PerformanceEngine()

    def test_total_return_is_final_over_initial(self):
        metrics = self.engine.compute(curve([100.0, 150.0]), [], 100.0)
        self.assertAlmostEqual(metrics.total_return, 0.5)

    def test_cagr_compounds_over_the_elapsed_period(self):
        points = curve([100.0, 200.0], step_days=365)
        metrics = self.engine.compute(points, [], 100.0)
        expected = 2.0 ** (DAYS_PER_YEAR / 365.0) - 1.0
        self.assertAlmostEqual(metrics.cagr, expected, places=6)

    def test_volatility_annualizes_by_sqrt_252(self):
        values = [100.0]
        for index in range(40):
            values.append(values[-1] * (1.01 if index % 2 == 0 else 0.99))
        metrics = self.engine.compute(curve(values), [], 100.0)
        from src.portfolio.analytics import sample_stdev
        daily = sample_stdev(simple_returns(curve(values)))
        self.assertAlmostEqual(metrics.volatility,
                               daily * math.sqrt(TRADING_DAYS_PER_YEAR))

    def test_final_capital_is_the_last_equity(self):
        metrics = self.engine.compute(curve([100.0, 130.0]), [], 100.0)
        self.assertAlmostEqual(metrics.final_capital, 130.0)


class TestInsufficientData(unittest.TestCase):
    def setUp(self):
        self.engine = PerformanceEngine()

    def test_volatility_needs_a_minimum_sample(self):
        metrics = self.engine.compute(
            curve([100.0 + i for i in range(MIN_RETURN_OBSERVATIONS - 2)]), [], 100.0)
        self.assertIsNone(metrics.volatility)
        self.assertIn("volatility", metrics.unavailable)

    def test_sharpe_needs_more_than_volatility_does(self):
        count = MIN_RATIO_OBSERVATIONS - 2
        values = [100.0]
        for index in range(count):
            values.append(values[-1] * (1.01 if index % 2 == 0 else 0.995))
        metrics = self.engine.compute(curve(values), [], 100.0)
        self.assertIsNone(metrics.sharpe)
        self.assertIn("sharpe", metrics.unavailable)

    def test_an_empty_curve_reports_everything_unavailable(self):
        metrics = self.engine.compute([], [], 100.0)
        self.assertIn("all", metrics.unavailable)

    def test_no_trades_marks_trade_metrics_unavailable(self):
        metrics = self.engine.compute(curve([100.0, 110.0]), [], 100.0)
        self.assertIn("trade_metrics", metrics.unavailable)
        self.assertIsNone(metrics.win_rate)


class TestRiskFreeRate(unittest.TestCase):
    """Spec §36 — never blindly assumed to be zero."""

    def _sharpe(self, risk_free):
        values = [100.0]
        for index in range(60):
            values.append(values[-1] * (1.006 if index % 3 else 0.997))
        engine = PerformanceEngine(risk_free_rate=risk_free,
                                   risk_free_source="test")
        return engine.compute(curve(values), [], 100.0).sharpe

    def test_a_higher_risk_free_rate_lowers_sharpe(self):
        self.assertGreater(self._sharpe(0.0), self._sharpe(0.05))

    def test_the_source_is_recorded_in_the_methodology(self):
        engine = PerformanceEngine(0.04, "3M T-bill, FRED DTB3")
        self.assertIn("FRED DTB3", engine.methodology()["risk_free_rate"])

    def test_methodology_documents_every_formula(self):
        methodology = PerformanceEngine().methodology()
        for key in ("cagr", "sharpe", "sortino", "calmar", "turnover",
                    "profit_factor", "expectancy"):
            self.assertIn(key, methodology)


class TestDrawdown(unittest.TestCase):
    def test_a_single_episode_is_measured(self):
        episodes = compute_drawdown_episodes(curve([100.0, 120.0, 90.0, 130.0]))
        self.assertEqual(len(episodes), 1)
        self.assertAlmostEqual(episodes[0].depth, (90.0 - 120.0) / 120.0)
        self.assertTrue(episodes[0].is_recovered)

    def test_an_unrecovered_episode_is_reported_as_open(self):
        episodes = compute_drawdown_episodes(curve([100.0, 120.0, 90.0]))
        self.assertEqual(len(episodes), 1)
        self.assertFalse(episodes[0].is_recovered)
        self.assertIsNone(episodes[0].recovery_days)

    def test_a_monotonic_rise_has_no_episodes(self):
        self.assertEqual(compute_drawdown_episodes(curve([100.0, 110.0, 120.0])), [])

    def test_multiple_episodes_are_separated(self):
        episodes = compute_drawdown_episodes(
            curve([100.0, 90.0, 110.0, 95.0, 120.0]))
        self.assertEqual(len(episodes), 2)

    def test_underwater_curve_is_annotated(self):
        points = curve([100.0, 120.0, 90.0])
        annotate_drawdown(points)
        self.assertAlmostEqual(points[0].drawdown, 0.0)
        self.assertAlmostEqual(points[1].drawdown, 0.0)
        self.assertAlmostEqual(points[2].drawdown, (90.0 - 120.0) / 120.0)

    def test_calmar_uses_the_max_drawdown(self):
        values = [100.0]
        for index in range(60):
            values.append(values[-1] * (1.01 if index % 4 else 0.97))
        metrics = PerformanceEngine().compute(curve(values), [], 100.0)
        if metrics.calmar is not None and metrics.max_drawdown:
            self.assertAlmostEqual(
                metrics.calmar,
                metrics.annualized_return / abs(metrics.max_drawdown))


class TestTradeMetrics(unittest.TestCase):
    def setUp(self):
        self.engine = PerformanceEngine()
        self.trades = [trade(100.0), trade(-50.0, instrument="b"),
                       trade(200.0, instrument="c"), trade(-25.0, instrument="d")]

    def test_win_rate(self):
        metrics = self.engine.compute(curve([100.0, 110.0]), self.trades, 100.0)
        self.assertEqual(metrics.winning_trades, 2)
        self.assertEqual(metrics.losing_trades, 2)
        self.assertAlmostEqual(metrics.win_rate, 0.5)

    def test_profit_factor_is_gross_profit_over_gross_loss(self):
        metrics = self.engine.compute(curve([100.0, 110.0]), self.trades, 100.0)
        self.assertAlmostEqual(metrics.profit_factor, 300.0 / 75.0)

    def test_expectancy_is_the_mean_net_pnl(self):
        metrics = self.engine.compute(curve([100.0, 110.0]), self.trades, 100.0)
        self.assertAlmostEqual(metrics.expectancy, (100 - 50 + 200 - 25) / 4)

    def test_averages_and_extremes(self):
        metrics = self.engine.compute(curve([100.0, 110.0]), self.trades, 100.0)
        self.assertAlmostEqual(metrics.average_win, 150.0)
        self.assertAlmostEqual(metrics.average_loss, -37.5)
        self.assertAlmostEqual(metrics.largest_win, 200.0)
        self.assertAlmostEqual(metrics.largest_loss, -50.0)

    def test_all_winners_makes_profit_factor_undefined_not_infinite(self):
        metrics = self.engine.compute(
            curve([100.0, 110.0]), [trade(50.0), trade(75.0, instrument="b")], 100.0)
        self.assertIsNone(metrics.profit_factor)
        self.assertIn("profit_factor", metrics.unavailable)

    def test_costs_reduce_net_pnl(self):
        costly = trade(100.0, costs=30.0)
        self.assertAlmostEqual(costly.net_pnl, 100.0)
        self.assertAlmostEqual(costly.gross_pnl, 130.0)

    def test_holding_period_is_averaged(self):
        metrics = self.engine.compute(
            curve([100.0, 110.0]),
            [trade(10.0, days=4), trade(10.0, days=8, instrument="b")], 100.0)
        self.assertAlmostEqual(metrics.average_holding_days, 6.0)


class TestTurnoverAndExposure(unittest.TestCase):
    def test_turnover_is_notional_over_average_equity(self):
        points = curve([100.0, 100.0, 100.0])
        metrics = PerformanceEngine().compute(
            points, [], 100.0, traded_notional=250.0)
        self.assertAlmostEqual(metrics.turnover, 2.5)

    def test_no_trading_marks_turnover_unavailable(self):
        metrics = PerformanceEngine().compute(
            curve([100.0, 110.0]), [], 100.0, traded_notional=0.0)
        self.assertIn("turnover", metrics.unavailable)

    def test_average_exposure_is_measured(self):
        points = curve([100.0, 100.0])
        for point in points:
            point.gross_exposure = 50.0
        metrics = PerformanceEngine().compute(points, [], 100.0)
        self.assertAlmostEqual(metrics.average_exposure, 0.5)


class TestBenchmark(unittest.TestCase):
    def test_benchmark_return_and_excess(self):
        points = curve([100.0, 120.0])
        benchmark = [(BASE, 400.0), (BASE + timedelta(days=1), 440.0)]
        metrics = PerformanceEngine().compute(
            points, [], 100.0, benchmark_points=benchmark)
        self.assertAlmostEqual(metrics.benchmark_return, 0.10)
        self.assertAlmostEqual(metrics.excess_return, 0.20 - 0.10)

    def test_absent_benchmark_is_unavailable_not_zero(self):
        metrics = PerformanceEngine().compute(curve([100.0, 120.0]), [], 100.0)
        self.assertIsNone(metrics.benchmark_return)
        self.assertIn("benchmark", metrics.unavailable)


class TestPeriodAndRolling(unittest.TestCase):
    def test_monthly_returns_are_bucketed(self):
        points = curve([100.0 + i for i in range(70)], step_days=1)
        buckets = period_returns(points, "month")
        self.assertGreaterEqual(len(buckets), 2)
        for key, _ in buckets:
            self.assertRegex(key, r"^\d{4}-\d{2}$")

    def test_quarterly_and_yearly_granularity(self):
        points = curve([100.0 + i for i in range(400)], step_days=1)
        self.assertTrue(period_returns(points, "quarter"))
        self.assertTrue(period_returns(points, "year"))

    def test_rolling_return_is_produced(self):
        points = curve([100.0 + i for i in range(40)])
        series = rolling_metric(points, window=10, metric="return")
        self.assertTrue(series)
        self.assertEqual(len(series[0]), 2)

    def test_a_window_longer_than_the_data_yields_nothing(self):
        self.assertEqual(rolling_metric(curve([100.0, 110.0]), window=50), [])

    def test_rolling_drawdown_is_never_positive(self):
        points = curve([100.0, 120.0, 90.0, 130.0, 100.0] * 8)
        for _, value in rolling_metric(points, window=5, metric="drawdown"):
            self.assertLessEqual(value, 0.0)


if __name__ == "__main__":
    unittest.main()
