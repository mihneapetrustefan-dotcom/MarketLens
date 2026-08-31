"""
tests/backtest/test_execution.py
-------------------------------------
The simulation executor and the cost/slippage models
(spec §11-§17, §59, §92).

Two themes. First, that costs and slippage are actually charged and
actually move the price against the trade — a slippage model that
silently improved fills would be worse than none. Second, that every
refusal to fill is RECORDED with a reason rather than silently
dropped, because a run that quietly lost a third of its orders reads
as a clean run.
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
    CostModel, ExecutionAssumptions, ExecutionTiming, OrderSide, OrderState,
    RejectReason, SimulatedOrder, SlippageMethod, SlippageModel,
)
from tests.backtest.helpers import add_bars, add_instrument, make_connection


class TestCostModel(unittest.TestCase):
    def test_basis_point_commission(self):
        model = CostModel(commission_bps=10.0)
        self.assertAlmostEqual(model.charge(100, 50.0), 5000 * 0.001)

    def test_per_share_commission(self):
        model = CostModel(commission_per_share=0.005)
        self.assertAlmostEqual(model.charge(200, 50.0), 1.0)

    def test_minimum_commission_applies(self):
        model = CostModel(commission_bps=1.0, minimum_commission=2.0)
        self.assertAlmostEqual(model.charge(1, 10.0), 2.0)

    def test_fees_are_added_on_top_of_commission(self):
        model = CostModel(commission_bps=10.0, fee_bps=5.0)
        self.assertAlmostEqual(model.charge(100, 50.0), 5000 * 0.0015)

    def test_cost_is_never_negative(self):
        self.assertGreaterEqual(CostModel().charge(-100, 50.0), 0.0)

    def test_short_and_long_are_charged_alike(self):
        model = CostModel(commission_bps=10.0)
        self.assertAlmostEqual(model.charge(100, 50.0), model.charge(-100, 50.0))

    def test_zero_model_is_flagged(self):
        self.assertTrue(CostModel().is_zero)
        self.assertFalse(CostModel(commission_bps=1.0).is_zero)


class TestSlippageModel(unittest.TestCase):
    def test_buys_pay_up(self):
        model = SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=10.0)
        self.assertGreater(model.apply(100.0, OrderSide.BUY), 100.0)

    def test_sells_receive_less(self):
        model = SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=10.0)
        self.assertLess(model.apply(100.0, OrderSide.SELL), 100.0)

    def test_none_method_leaves_the_price_untouched(self):
        model = SlippageModel(method=SlippageMethod.NONE)
        self.assertAlmostEqual(model.apply(100.0, OrderSide.BUY), 100.0)

    def test_slippage_is_never_negative(self):
        model = SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=-50.0)
        self.assertGreaterEqual(model.slippage_bps(), 0.0)

    def test_unmeasurable_volatility_falls_back_to_base_not_zero(self):
        """Assuming no slippage because a number was missing is the optimistic failure."""
        model = SlippageModel(method=SlippageMethod.VOLATILITY_SCALED, base_bps=7.0)
        self.assertAlmostEqual(model.slippage_bps(volatility=None), 7.0)

    def test_higher_volatility_produces_more_slippage(self):
        model = SlippageModel(method=SlippageMethod.VOLATILITY_SCALED, base_bps=5.0)
        self.assertGreater(model.slippage_bps(volatility=0.80),
                           model.slippage_bps(volatility=0.10))

    def test_participation_model_is_labelled_simplified(self):
        model = SlippageModel(method=SlippageMethod.PARTICIPATION_SCALED)
        self.assertTrue(model.is_simplified_impact)
        self.assertFalse(SlippageModel().is_simplified_impact)

    def test_larger_participation_produces_more_impact(self):
        model = SlippageModel(method=SlippageMethod.PARTICIPATION_SCALED, base_bps=1.0)
        self.assertGreater(model.slippage_bps(participation=0.30),
                           model.slippage_bps(participation=0.01))


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        # A flat, known series so fill prices are exactly predictable.
        add_bars(self.conn, "i-a", days=40, prices=[100.0] * 30,
                 weekdays_only=True, volume=10_000.0)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-a"])
        self.bars = self.calendar.bars("i-a")
        self.guard = TemporalGuard()

    def tearDown(self):
        self.conn.close()

    def build(self, assumptions=None, costs=None, slippage=None):
        return SimulationExecutor(
            self.calendar, costs or CostModel(), slippage or SlippageModel(
                method=SlippageMethod.NONE),
            assumptions or ExecutionAssumptions(max_participation=None),
            self.guard)

    def order(self, side=OrderSide.BUY, quantity=10.0, at=None):
        return SimulatedOrder(
            order_id="o1", run_id="r", instrument_id="i-a", side=side,
            quantity=quantity, created_at=at or self.bars[5].timestamp,
            decision_at=at or self.bars[5].timestamp)


class TestFilling(ExecutorTestCase):
    def test_order_fills_on_the_next_session(self):
        executor = self.build()
        order = self.order()
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(len(fills), 1)
        self.assertEqual(order.state, OrderState.FILLED)
        self.assertGreater(fills[0].filled_at, order.created_at)

    def test_fill_never_uses_the_bar_that_produced_the_decision(self):
        executor = self.build()
        order = self.order(at=self.bars[5].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(fills[0].bar_timestamp, self.bars[6].timestamp)

    def test_zero_quantity_is_rejected(self):
        executor = self.build()
        order = SimulatedOrder(
            order_id="o", run_id="r", instrument_id="i-a", side=OrderSide.BUY,
            quantity=1.0, created_at=self.bars[5].timestamp)
        order.quantity = 0.0        # bypass the constructor guard
        executor.execute(order, ExecutionContext(available_cash=100.0))
        self.assertEqual(order.reject_reason, RejectReason.ZERO_QUANTITY)

    def test_no_session_after_the_order_is_rejected_with_a_reason(self):
        executor = self.build()
        order = self.order(at=self.bars[-1].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(fills, [])
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(order.reject_reason, RejectReason.NO_PRICE)

    def test_unknown_instrument_is_rejected(self):
        executor = self.build()
        order = SimulatedOrder(
            order_id="o", run_id="r", instrument_id="nowhere",
            side=OrderSide.BUY, quantity=1.0, created_at=self.bars[5].timestamp)
        executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(order.reject_reason, RejectReason.NO_PRICE)

    def test_fill_beyond_the_horizon_is_refused(self):
        executor = self.build()
        order = self.order(at=self.bars[5].timestamp)
        fills = executor.execute(order, ExecutionContext(
            available_cash=100_000.0, horizon_end=self.bars[5].timestamp))
        self.assertEqual(fills, [])
        self.assertEqual(order.reject_reason, RejectReason.BEYOND_HORIZON)


class TestCash(ExecutorTestCase):
    def test_insufficient_cash_rejects_with_a_reason(self):
        executor = self.build()
        order = self.order(quantity=1000.0)
        fills = executor.execute(order, ExecutionContext(available_cash=1.0))
        self.assertEqual(fills, [])
        self.assertEqual(order.reject_reason, RejectReason.INSUFFICIENT_CASH)

    def test_partial_cash_produces_a_smaller_fill(self):
        executor = self.build()
        order = self.order(quantity=100.0)      # needs 10,000 at 100
        fills = executor.execute(order, ExecutionContext(available_cash=2_500.0))
        self.assertTrue(fills)
        self.assertLess(fills[0].quantity, 100.0)
        self.assertTrue(fills[0].is_partial)

    def test_a_sell_is_not_blocked_by_low_cash(self):
        executor = self.build()
        order = self.order(side=OrderSide.SELL, quantity=5.0)
        fills = executor.execute(order, ExecutionContext(
            available_cash=0.0, current_quantity=10.0))
        self.assertTrue(fills)


class TestShorting(ExecutorTestCase):
    def test_opening_a_short_is_refused_by_default(self):
        executor = self.build()
        order = self.order(side=OrderSide.SELL, quantity=10.0)
        fills = executor.execute(order, ExecutionContext(
            available_cash=100_000.0, current_quantity=0.0, allow_shorting=False))
        self.assertEqual(fills, [])
        self.assertEqual(order.reject_reason, RejectReason.SHORTING_DISABLED)

    def test_shorting_is_allowed_when_enabled(self):
        executor = self.build()
        order = self.order(side=OrderSide.SELL, quantity=10.0)
        fills = executor.execute(order, ExecutionContext(
            available_cash=100_000.0, current_quantity=0.0, allow_shorting=True))
        self.assertTrue(fills)

    def test_closing_a_long_is_not_treated_as_shorting(self):
        executor = self.build()
        order = self.order(side=OrderSide.SELL, quantity=10.0)
        fills = executor.execute(order, ExecutionContext(
            available_cash=0.0, current_quantity=10.0, allow_shorting=False))
        self.assertTrue(fills)


class TestLiquidityCap(ExecutorTestCase):
    def test_participation_cap_produces_a_partial_fill(self):
        executor = self.build(ExecutionAssumptions(
            max_participation=0.10, allow_partial_fills=True))
        order = self.order(quantity=5_000.0)    # bar volume is 10,000
        fills = executor.execute(order, ExecutionContext(available_cash=10_000_000.0))
        self.assertTrue(fills)
        self.assertAlmostEqual(fills[0].quantity, 1_000.0)

    def test_participation_cap_rejects_when_partials_are_disallowed(self):
        executor = self.build(ExecutionAssumptions(
            max_participation=0.10, allow_partial_fills=False))
        order = self.order(quantity=5_000.0)
        fills = executor.execute(order, ExecutionContext(available_cash=10_000_000.0))
        self.assertEqual(fills, [])
        self.assertEqual(order.reject_reason, RejectReason.LIQUIDITY_CAP)

    def test_a_small_order_is_unaffected_by_the_cap(self):
        executor = self.build(ExecutionAssumptions(max_participation=0.10))
        order = self.order(quantity=10.0)
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertAlmostEqual(fills[0].quantity, 10.0)

    def test_partial_fills_across_sessions_accumulate(self):
        executor = self.build(ExecutionAssumptions(
            max_participation=0.10, allow_partial_fills=True, max_bars_to_fill=3))
        order = self.order(quantity=2_500.0)
        fills = executor.execute(order, ExecutionContext(available_cash=10_000_000.0))
        self.assertGreater(len(fills), 1)
        self.assertAlmostEqual(sum(f.quantity for f in fills), 2_500.0)
        self.assertEqual(order.state, OrderState.FILLED)


class TestPricingAndCosts(ExecutorTestCase):
    def test_commission_is_charged_on_the_fill(self):
        executor = self.build(costs=CostModel(commission_bps=10.0))
        order = self.order(quantity=10.0)
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        # NEXT_BAR_OPEN fills at the bar's OPEN (99.5 in this fixture),
        # not its close — so notional is 995, not 1000.
        self.assertAlmostEqual(fills[0].commission, 995 * 0.001)

    def test_slippage_moves_a_buy_price_up_and_is_recorded(self):
        executor = self.build(slippage=SlippageModel(
            method=SlippageMethod.FIXED_BPS, base_bps=20.0))
        order = self.order()
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        fill = fills[0]
        self.assertGreater(fill.price, fill.reference_price)
        self.assertGreater(fill.slippage_cost, 0.0)

    def test_reference_price_is_preserved_for_audit(self):
        executor = self.build(slippage=SlippageModel(
            method=SlippageMethod.FIXED_BPS, base_bps=20.0))
        order = self.order()
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        # The untouched bar price survives alongside the charged one, so
        # the slippage actually applied is auditable.
        self.assertAlmostEqual(fills[0].reference_price, 99.5)
        self.assertNotAlmostEqual(fills[0].price, fills[0].reference_price)

    def test_next_bar_open_timing_uses_the_open(self):
        executor = self.build(ExecutionAssumptions(
            timing=ExecutionTiming.NEXT_BAR_OPEN, max_participation=None))
        order = self.order()
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertAlmostEqual(fills[0].reference_price, 99.5)

    def test_next_bar_close_timing_uses_the_close(self):
        executor = self.build(ExecutionAssumptions(
            timing=ExecutionTiming.NEXT_BAR_CLOSE, max_participation=None))
        order = self.order()
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertAlmostEqual(fills[0].reference_price, 100.0)


class TestSameBarExecution(ExecutorTestCase):
    """The deliberately-unrealistic timing, permitted only when selected."""

    def test_same_bar_close_fills_against_the_deciding_bar(self):
        executor = self.build(ExecutionAssumptions(
            timing=ExecutionTiming.SAME_BAR_CLOSE, max_participation=None))
        order = self.order(at=self.bars[5].timestamp)
        fills = executor.execute(order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(fills[0].bar_timestamp, self.bars[5].timestamp)

    def test_the_default_timing_would_have_raised_on_that_same_bar(self):
        """Proves the same-moment allowance is what permits it, not luck."""
        guard = TemporalGuard()
        with self.assertRaises(TemporalViolation):
            guard.check_fill_after_order(
                self.bars[5].timestamp, self.bars[5].timestamp)


class TestExecutorDescription(ExecutorTestCase):
    def test_describe_records_every_model_version(self):
        described = self.build().describe()
        for key in ("engine", "timing", "cost_model", "slippage_model"):
            self.assertIn(key, described)


if __name__ == "__main__":
    unittest.main()
