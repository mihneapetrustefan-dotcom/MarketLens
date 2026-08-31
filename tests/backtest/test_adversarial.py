"""
tests/backtest/test_adversarial.py
---------------------------------------
The six adversarial scenarios (spec §93), each written as an attempt to
CHEAT that must be caught.

These are different in kind from the rest of the suite. Every other
test asks "does the code do what it says". These ask "can I make the
backtest lie" — and each one is a real, specific way that backtests are
made to look good in practice. A framework that passes its unit tests
but fails these produces beautiful, worthless numbers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.calendar import MarketCalendar
from src.backtest.execution import ExecutionContext, SimulationExecutor
from src.backtest.guards import TemporalGuard, TemporalViolation
from src.domain.backtest_models import (
    CostModel, ExecutionAssumptions, ExecutionTiming, OrderSide, SimulatedOrder,
    SlippageMethod, SlippageModel, WarningCode,
)
from tests.backtest.helpers import (
    END, START, add_bars, add_instrument, make_config, make_connection,
    make_signal, standard_universe,
)

T = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


class TestCase1_FillBeforeTheSignal(unittest.TestCase):
    """
    CASE 1 — a signal generated at 10:00 is executed at the 09:59 price.
    EXPECTED: reject.
    """

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_bars(self.conn, "i-a", days=40, prices=[100.0] * 30)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-a"])
        self.bars = self.calendar.bars("i-a")

    def tearDown(self):
        self.conn.close()

    def test_filling_at_an_earlier_bar_is_refused(self):
        guard = TemporalGuard()
        order_time = self.bars[10].timestamp
        earlier_bar = self.bars[9].timestamp
        with self.assertRaises(TemporalViolation):
            guard.check_fill_after_order(earlier_bar, order_time)

    def test_the_executor_never_selects_an_earlier_bar(self):
        executor = SimulationExecutor(
            self.calendar, CostModel(), SlippageModel(method=SlippageMethod.NONE),
            ExecutionAssumptions(max_participation=None), TemporalGuard())
        order = SimulatedOrder(
            order_id="o", run_id="r", instrument_id="i-a", side=OrderSide.BUY,
            quantity=1.0, created_at=self.bars[10].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=1_000.0))
        for fill in fills:
            self.assertGreater(fill.bar_timestamp, order.created_at)


class TestCase2_SameDayCloseBeforeTheClose(unittest.TestCase):
    """
    CASE 2 — deciding on a day's close, then filling at that same close.
    EXPECTED: reject, unless the timing explicitly permits it.
    """

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_bars(self.conn, "i-a", days=40, prices=[100.0] * 30)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-a"])
        self.bars = self.calendar.bars("i-a")

    def tearDown(self):
        self.conn.close()

    def _executor(self, timing):
        return SimulationExecutor(
            self.calendar, CostModel(), SlippageModel(method=SlippageMethod.NONE),
            ExecutionAssumptions(timing=timing, max_participation=None),
            TemporalGuard())

    def test_default_timing_refuses_the_deciding_bar(self):
        executor = self._executor(ExecutionTiming.NEXT_BAR_OPEN)
        order = SimulatedOrder(
            order_id="o", run_id="r", instrument_id="i-a", side=OrderSide.BUY,
            quantity=1.0, created_at=self.bars[10].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=1_000.0))
        self.assertNotEqual(fills[0].bar_timestamp, self.bars[10].timestamp)

    def test_same_bar_close_is_permitted_only_when_selected(self):
        executor = self._executor(ExecutionTiming.SAME_BAR_CLOSE)
        order = SimulatedOrder(
            order_id="o", run_id="r", instrument_id="i-a", side=OrderSide.BUY,
            quantity=1.0, created_at=self.bars[10].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=1_000.0))
        self.assertEqual(fills[0].bar_timestamp, self.bars[10].timestamp)

    def test_choosing_it_marks_the_run_as_unrealistic(self):
        """Permitted, but never silently — the run carries the warning."""
        from src.backtest.engine import BacktestEngine
        standard_universe(self.conn)
        config = make_config(
            execution=ExecutionAssumptions(timing=ExecutionTiming.SAME_BAR_CLOSE))
        result = BacktestEngine(self.conn, config, signals=[]).run()
        self.assertIn(WarningCode.SAME_BAR_EXECUTION,
                      [w.code for w in result.warnings])


class TestCase3_CurrentUniverseForAHistoricalPeriod(unittest.TestCase):
    """
    CASE 3 — backtesting 2018 with today's index membership.
    EXPECTED: flag (this database has no point-in-time membership, so
    rejecting outright would forbid every run).
    """

    def setUp(self):
        self.conn = make_connection()
        standard_universe(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_implicit_universe_raises_survivorship_risk(self):
        from src.backtest.engine import BacktestEngine
        config = make_config(universe=[])       # derived from today's signals
        signals = [make_signal("i-aaa", START + timedelta(days=30))]
        result = BacktestEngine(self.conn, config, signals=signals).run()
        self.assertIn(WarningCode.SURVIVORSHIP_RISK,
                      [w.code for w in result.warnings])

    def test_the_warning_lowers_the_research_quality_score(self):
        from src.backtest.attribution import assess_quality
        from src.domain.backtest_models import PerformanceMetrics
        metrics = PerformanceMetrics(total_trades=50)
        clean = assess_quality(make_config(), metrics, [], 300, 2, 2)
        flagged = assess_quality(
            make_config(), metrics, [WarningCode.SURVIVORSHIP_RISK], 300, 2, 2)
        self.assertLess(flagged.score, clean.score)


class TestCase4_TodaysVolatilityForAPastDecision(unittest.TestCase):
    """
    CASE 4 — using a volatility estimate computed from data after the
    decision it informs.
    EXPECTED: reject.
    """

    def test_a_feature_stamped_after_the_cutoff_raises(self):
        guard = TemporalGuard()
        with self.assertRaises(TemporalViolation) as caught:
            guard.check_feature_not_future(
                T + timedelta(days=400), T, "volatility estimate")
        self.assertIn("volatility estimate", str(caught.exception))

    def test_a_feature_from_before_the_cutoff_passes(self):
        guard = TemporalGuard()
        guard.check_feature_not_future(T - timedelta(days=30), T, "volatility")

    def test_price_reads_never_cross_the_anchor(self):
        conn = make_connection()
        add_instrument(conn, "i-a", "AAA", "technology")
        add_bars(conn, "i-a", days=60, prices=[100.0] * 40)
        calendar = MarketCalendar(conn)
        calendar.load(["i-a"])
        bars = calendar.bars("i-a")
        anchor = bars[10].timestamp
        guard = TemporalGuard()
        # Everything the valuation path can see must precede the anchor.
        for index in range(11):
            guard.check_bar_not_future(bars[index].timestamp, anchor)
        with self.assertRaises(TemporalViolation):
            guard.check_bar_not_future(bars[11].timestamp, anchor)
        conn.close()


class TestCase5_RiskSeesFuturePortfolioValue(unittest.TestCase):
    """
    CASE 5 — the risk engine valuing the book with prices from after
    the decision.
    EXPECTED: reject.
    """

    def setUp(self):
        self.conn = make_connection()
        standard_universe(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_snapshot_prices_never_postdate_the_anchor(self):
        from src.portfolio.service import PortfolioService
        from src.domain.portfolio_models import Position, PositionSource

        calendar = MarketCalendar(self.conn)
        calendar.load(["i-aaa"])
        bars = calendar.bars("i-aaa")
        anchor = bars[20].timestamp

        service = PortfolioService(self.conn)
        position = Position(
            position_id="p", portfolio_id="pf", instrument_id="i-aaa",
            quantity=10.0, average_entry_price=100.0,
            source=PositionSource.SIMULATED, opened_at=bars[0].timestamp)
        snapshot = service.build_snapshot("pf", anchor, [position], cash=1000.0)

        for valuation in snapshot.valuations:
            self.assertLessEqual(valuation.price_timestamp, anchor)

    def test_a_future_bar_does_not_change_the_valuation(self):
        from src.portfolio.service import PortfolioService
        from src.domain.portfolio_models import Position, PositionSource

        calendar = MarketCalendar(self.conn)
        calendar.load(["i-aaa"])
        anchor = calendar.bars("i-aaa")[20].timestamp

        service = PortfolioService(self.conn)
        position = Position(
            position_id="p", portfolio_id="pf", instrument_id="i-aaa",
            quantity=10.0, average_entry_price=100.0,
            source=PositionSource.SIMULATED, opened_at=anchor - timedelta(days=30))

        before = service.build_snapshot("pf", anchor, [position], cash=1000.0).equity
        future = (anchor + timedelta(days=2)).replace(hour=4)
        self.conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("i-aaa", "1d", future.isoformat(), 9_999.0, 9_999.0, 9_999.0,
             9_999.0, 9_999.0, 1000.0, "test", future.isoformat()))
        self.conn.commit()
        after = service.build_snapshot("pf", anchor, [position], cash=1000.0).equity
        self.assertAlmostEqual(before, after)


class TestCase6_ModelTrainedOnTheTestPeriod(unittest.TestCase):
    """
    CASE 6 — a model trained over the whole history generating
    predictions for periods inside its own training window.
    EXPECTED: reject, or flag explicitly as contaminated.
    """

    def test_a_model_trained_after_the_decision_raises(self):
        guard = TemporalGuard()
        with self.assertRaises(TemporalViolation) as caught:
            guard.check_model_trained_before(
                T + timedelta(days=200), T, "ridge_abnormal_return:v1")
        self.assertIn("in-sample replay", str(caught.exception))

    def test_a_model_trained_before_the_decision_passes(self):
        guard = TemporalGuard()
        guard.check_model_trained_before(T - timedelta(days=200), T, "ridge:v1")

    def test_walk_forward_test_windows_never_precede_their_training(self):
        from src.backtest.robustness import walk_forward_windows
        windows = walk_forward_windows(
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            train_months=6, test_months=2, step_months=2)
        self.assertTrue(windows)
        for window in windows:
            self.assertGreaterEqual(window["test_start"], window["train_end"])
            self.assertGreater(window["test_end"], window["test_start"])

    def test_in_sample_flag_lowers_the_quality_score(self):
        from src.backtest.attribution import assess_quality
        from src.domain.backtest_models import PerformanceMetrics
        metrics = PerformanceMetrics(total_trades=200)
        clean = assess_quality(make_config(), metrics, [], 800, 2, 2)
        contaminated = assess_quality(
            make_config(), metrics, [WarningCode.IN_SAMPLE_MODEL], 800, 2, 2)
        self.assertLess(contaminated.score, clean.score)
        self.assertTrue(any("training window" in note
                            for note in contaminated.notes))


if __name__ == "__main__":
    unittest.main()
