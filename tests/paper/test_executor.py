"""
tests/paper/test_executor.py
---------------------------------
The paper executor: order lifecycle, order types, fills
(spec §14-§20, §75, §86).

The order-type tests carry the most weight. Spec §16 forbids claiming
realistic simulation for a type whose behaviour is not modelled, so each
type is checked against a bar whose range is known exactly — a limit
that should not fill, one that should, and the stop-limit case where
OHLC genuinely cannot tell you what happened first.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import MarketCalendar
from src.backtest.execution import ExecutionContext, ExecutionEngine
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from src.domain.paper_models import (
    DataFreshness, ExecutionVenue, HealthState, OrderSide, PaperOrderState,
    PaperOrderType, PaperRejectReason, TimeInForce,
)
from src.paper.executor import (
    BrokerLikeInterface, PaperExecutor, SIMPLIFIED_MICROSTRUCTURE,
    fill_to_simulated,
)
from tests.backtest.helpers import add_bars, add_instrument
from tests.paper.helpers import END, flat_universe, make_connection, make_order


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        flat_universe(self.conn)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-flat"])
        self.bars = self.calendar.bars("i-flat")
        self.ledger = PortfolioLedger(100_000.0, run_id="sess-1")
        self.executor = self.build()

    def tearDown(self):
        self.conn.close()

    def build(self, costs=None, slippage=None, **overrides):
        defaults = dict(account_id="acct-1", session_id="sess-1",
                        max_participation=None)
        defaults.update(overrides)
        executor = PaperExecutor(
            self.calendar, self.ledger,
            costs or CostModel(commission_bps=2.0),
            slippage or SlippageModel(method=SlippageMethod.NONE), **defaults)
        executor.connect()
        return executor

    def place(self, **overrides):
        overrides.setdefault("at", self.bars[10].timestamp)
        order = make_order(**overrides)
        return self.executor.place_order(order, overrides["at"])


class TestInterfaces(ExecutorTestCase):
    """Spec §44, §86, §87 — both contracts, so paper and live share a shape."""

    def test_satisfies_the_phase_12_execution_engine(self):
        self.assertIsInstance(self.executor, ExecutionEngine)

    def test_satisfies_the_broker_like_interface(self):
        self.assertIsInstance(self.executor, BrokerLikeInterface)

    def test_the_broker_interface_has_no_authentication_method(self):
        """
        Spec §85 — the shape a broker adapter fills in must not include a
        place to put credentials, or someone will.
        """
        forbidden = {"login", "authenticate", "set_credentials", "api_key"}
        self.assertEqual(
            forbidden & set(BrokerLikeInterface.__abstractmethods__), set())

    def test_connect_touches_nothing_external(self):
        self.assertTrue(self.executor.connect())
        self.assertTrue(self.executor.is_connected())
        self.executor.disconnect()
        self.assertFalse(self.executor.is_connected())

    def test_health_reports_failed_when_disconnected(self):
        self.executor.disconnect()
        self.assertEqual(self.executor.health(END), HealthState.FAILED)

    def test_describe_states_it_reaches_no_broker(self):
        described = self.executor.describe()
        self.assertFalse(described["connects_to_broker"])
        self.assertEqual(described["venue"], "paper")


class TestPlacement(ExecutorTestCase):
    def test_a_valid_order_is_accepted_not_filled(self):
        """Placing and executing are different moments, even for a market order."""
        order = self.place()
        self.assertEqual(order.state, PaperOrderState.ACCEPTED)
        self.assertEqual(order.filled_quantity, 0.0)

    def test_zero_quantity_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            make_order(quantity=0.0)

    def test_an_unknown_instrument_is_rejected(self):
        order = self.place(instrument_id="i-nowhere")
        self.assertEqual(order.reject_reason,
                         PaperRejectReason.UNKNOWN_INSTRUMENT)

    def test_a_limit_order_without_a_price_is_refused(self):
        with self.assertRaises(ValueError):
            make_order(order_type=PaperOrderType.LIMIT)

    def test_a_stop_order_without_a_price_is_refused(self):
        with self.assertRaises(ValueError):
            make_order(order_type=PaperOrderType.STOP)

    def test_a_negative_limit_price_is_rejected(self):
        order = self.place(order_type=PaperOrderType.LIMIT, limit_price=-5.0)
        self.assertEqual(order.reject_reason, PaperRejectReason.INVALID_PRICE)

    def test_opening_a_short_is_refused_by_default(self):
        order = self.place(side=OrderSide.SELL)
        self.assertEqual(order.reject_reason,
                         PaperRejectReason.SHORTING_DISABLED)

    def test_shorting_is_permitted_when_the_account_allows_it(self):
        self.executor = self.build(allow_shorting=True)
        order = self.place(side=OrderSide.SELL)
        self.assertEqual(order.state, PaperOrderState.ACCEPTED)

    def test_a_day_order_gets_an_expiry(self):
        order = self.place()
        self.assertIsNotNone(order.expires_at)

    def test_a_gtc_order_does_not_expire(self):
        order = self.place(time_in_force=TimeInForce.GTC)
        self.assertIsNone(order.expires_at)


class TestFilling(ExecutorTestCase):
    def test_a_market_order_fills_on_the_next_session(self):
        order = self.place()
        fills = self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertEqual(len(fills), 1)
        self.assertEqual(order.state, PaperOrderState.FILLED)

    def test_a_fill_never_uses_the_deciding_bar(self):
        order = self.place()
        self.assertEqual(self.executor.try_fill(order, self.bars[10].timestamp), [])

    def test_the_fill_updates_cash_and_position(self):
        order = self.place(quantity=10.0)
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertAlmostEqual(self.ledger.positions["i-flat"].quantity, 10.0)
        self.assertLess(self.ledger.cash, 100_000.0)
        self.assertAlmostEqual(
            self.ledger.cash,
            100_000.0 - fill.quantity * fill.price - fill.total_cost)

    def test_the_venue_is_always_paper(self):
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertEqual(fill.venue, ExecutionVenue.PAPER)

    def test_model_versions_travel_on_the_fill(self):
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertTrue(fill.execution_model_version)
        self.assertTrue(fill.slippage_model_version)
        self.assertTrue(fill.cost_model_version)

    def test_a_terminal_order_is_not_refilled(self):
        order = self.place()
        self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertEqual(self.executor.try_fill(order, self.bars[12].timestamp), [])

    def test_average_fill_price_is_recorded(self):
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertAlmostEqual(order.average_fill_price, fill.price)


class TestOrderTypes(unittest.TestCase):
    """
    Spec §16 — each type checked against a bar whose range is known.

    The series is built with an explicit price so open/high/low/close
    are exactly predictable: the helper writes open = p*0.995,
    high = p*1.01, low = p*0.99, close = p.
    """

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-x", "X", "technology")
        add_bars(self.conn, "i-x", end=END, days=40, prices=[100.0] * 30,
                 volume=1_000_000.0)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-x"])
        self.bars = self.calendar.bars("i-x")
        self.ledger = PortfolioLedger(1_000_000.0)
        self.executor = PaperExecutor(
            self.calendar, self.ledger, CostModel(),
            SlippageModel(method=SlippageMethod.NONE),
            session_id="s", max_participation=None, allow_shorting=True)
        self.executor.connect()

    def tearDown(self):
        self.conn.close()

    def place(self, **overrides):
        overrides.setdefault("at", self.bars[10].timestamp)
        overrides.setdefault("instrument_id", "i-x")
        return self.executor.place_order(make_order(**overrides),
                                         overrides["at"])

    def fill(self, order):
        return self.executor.try_fill(order, self.bars[11].timestamp)

    # --- limit ---

    def test_a_buy_limit_below_the_low_does_not_fill(self):
        order = self.place(order_type=PaperOrderType.LIMIT, limit_price=50.0)
        self.assertEqual(self.fill(order), [])
        self.assertTrue(order.state.is_working)

    def test_a_buy_limit_above_the_open_fills_at_the_open(self):
        """A real book would not charge more than the resting price."""
        order = self.place(order_type=PaperOrderType.LIMIT, limit_price=150.0)
        fills = self.fill(order)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].reference_price, 99.5)

    def test_a_buy_limit_inside_the_range_fills_at_the_limit(self):
        order = self.place(order_type=PaperOrderType.LIMIT, limit_price=99.2)
        fills = self.fill(order)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].reference_price, 99.2)

    def test_a_sell_limit_above_the_high_does_not_fill(self):
        order = self.place(side=OrderSide.SELL, order_type=PaperOrderType.LIMIT,
                           limit_price=500.0)
        self.assertEqual(self.fill(order), [])

    def test_a_sell_limit_within_reach_fills(self):
        order = self.place(side=OrderSide.SELL, order_type=PaperOrderType.LIMIT,
                           limit_price=100.5)
        self.assertEqual(len(self.fill(order)), 1)

    # --- stop ---

    def test_a_buy_stop_above_the_high_does_not_trigger(self):
        order = self.place(order_type=PaperOrderType.STOP, stop_price=500.0)
        self.assertEqual(self.fill(order), [])

    def test_a_buy_stop_within_the_range_triggers(self):
        order = self.place(order_type=PaperOrderType.STOP, stop_price=100.5)
        self.assertEqual(len(self.fill(order)), 1)

    def test_a_sell_stop_below_the_low_does_not_trigger(self):
        order = self.place(side=OrderSide.SELL, order_type=PaperOrderType.STOP,
                           stop_price=1.0)
        self.assertEqual(self.fill(order), [])

    def test_a_sell_stop_within_the_range_triggers(self):
        order = self.place(side=OrderSide.SELL, order_type=PaperOrderType.STOP,
                           stop_price=99.5)
        self.assertEqual(len(self.fill(order)), 1)

    # --- stop limit ---

    def test_a_stop_limit_needs_both_conditions(self):
        order = self.place(order_type=PaperOrderType.STOP_LIMIT,
                           stop_price=500.0, limit_price=500.0)
        self.assertEqual(self.fill(order), [])

    def test_a_stop_limit_that_fills_is_flagged_intrabar_ambiguous(self):
        """
        OHLC cannot say which came first when one bar spans both. The
        flag records that rather than inventing a sequence.
        """
        order = self.place(order_type=PaperOrderType.STOP_LIMIT,
                           stop_price=100.5, limit_price=100.9)
        fills = self.fill(order)
        self.assertEqual(len(fills), 1)
        self.assertTrue(fills[0].intrabar_ambiguous)

    def test_a_market_fill_is_never_flagged_ambiguous(self):
        order = self.place()
        self.assertFalse(self.fill(order)[0].intrabar_ambiguous)


class TestPartialFills(ExecutorTestCase):
    """Spec §19 — position and cash must update correctly after each fill."""

    def setUp(self):
        super().setUp()
        self.ledger = PortfolioLedger(10_000_000.0, run_id="sess-1")
        self.executor = self.build(max_participation=0.10)

    def test_the_participation_cap_produces_a_partial_fill(self):
        order = self.place(quantity=5_000.0)        # bar volume is 10,000
        fills = self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertAlmostEqual(fills[0].quantity, 1_000.0)
        self.assertTrue(fills[0].is_partial)
        self.assertEqual(order.state, PaperOrderState.PARTIALLY_FILLED)

    def test_the_position_reflects_only_what_filled(self):
        order = self.place(quantity=5_000.0)
        self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertAlmostEqual(self.ledger.positions["i-flat"].quantity, 1_000.0)

    def test_successive_ticks_accumulate_toward_the_full_quantity(self):
        order = self.place(quantity=2_500.0, time_in_force=TimeInForce.GTC)
        total = 0.0
        for index in (11, 12, 13):
            for fill in self.executor.try_fill(order, self.bars[index].timestamp):
                total += fill.quantity
        self.assertAlmostEqual(total, 2_500.0)
        self.assertEqual(order.state, PaperOrderState.FILLED)

    def test_the_average_fill_price_blends_partials(self):
        order = self.place(quantity=2_500.0, time_in_force=TimeInForce.GTC)
        for index in (11, 12, 13):
            self.executor.try_fill(order, self.bars[index].timestamp)
        self.assertIsNotNone(order.average_fill_price)
        self.assertGreater(order.average_fill_price, 0)

    def test_partials_can_be_disallowed(self):
        self.executor = self.build(max_participation=0.10,
                                   allow_partial_fills=False)
        order = self.place(quantity=5_000.0)
        self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertEqual(order.reject_reason, PaperRejectReason.LIQUIDITY_CAP)


class TestCostsAndSlippage(ExecutorTestCase):
    """Spec §21, §22 — the Phase 12 models, reused rather than reinvented."""

    def test_commission_is_charged(self):
        self.executor = self.build(costs=CostModel(commission_bps=10.0))
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertAlmostEqual(fill.commission,
                               fill.quantity * fill.price * 0.001)

    def test_slippage_moves_a_buy_against_the_trader(self):
        self.executor = self.build(slippage=SlippageModel(
            method=SlippageMethod.FIXED_BPS, base_bps=20.0))
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertGreater(fill.price, fill.reference_price)

    def test_the_reference_price_is_preserved_for_audit(self):
        self.executor = self.build(slippage=SlippageModel(
            method=SlippageMethod.FIXED_BPS, base_bps=20.0))
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        self.assertAlmostEqual(fill.reference_price, 99.5)
        self.assertGreater(fill.slippage_cost, 0)

    def test_the_microstructure_limitation_is_stated(self):
        self.assertIn("no bid/ask/spread", SIMPLIFIED_MICROSTRUCTURE)
        self.assertIn("microstructure", self.executor.describe())


class TestCancellationAndExpiry(ExecutorTestCase):
    def test_an_order_can_be_cancelled(self):
        order = self.place()
        cancelled = self.executor.cancel_order(order.order_id, self.bars[11].timestamp)
        self.assertEqual(cancelled.state, PaperOrderState.CANCELLED)

    def test_cancelling_an_unknown_order_returns_none(self):
        self.assertIsNone(self.executor.cancel_order("nope", END))

    def test_a_terminal_order_is_not_re_cancelled(self):
        order = self.place()
        self.executor.try_fill(order, self.bars[11].timestamp)
        result = self.executor.cancel_order(order.order_id, self.bars[12].timestamp)
        self.assertEqual(result.state, PaperOrderState.FILLED)

    def test_an_expired_order_does_not_fill(self):
        order = self.place()
        far = self.bars[10].timestamp + timedelta(days=5)
        self.assertEqual(self.executor.try_fill(order, far), [])
        self.assertEqual(order.state, PaperOrderState.EXPIRED)


class TestAdapters(ExecutorTestCase):
    def test_a_paper_fill_converts_to_the_phase_12_shape(self):
        """Spec §25 — the ledger stays single-sourced."""
        order = self.place()
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        converted = fill_to_simulated(fill)
        self.assertAlmostEqual(converted.quantity, fill.quantity)
        self.assertAlmostEqual(converted.price, fill.price)
        self.assertEqual(converted.fill_id, fill.fill_id)

    def test_the_phase_12_execute_contract_works(self):
        from src.domain.backtest_models import (
            OrderSide as BtSide, SimulatedOrder,
        )
        order = SimulatedOrder(
            order_id="sim-1", run_id="r", instrument_id="i-flat",
            side=BtSide.BUY, quantity=5.0,
            created_at=self.bars[10].timestamp)
        fills = self.executor.execute(
            order, ExecutionContext(available_cash=100_000.0))
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(order.filled_quantity, 5.0)


if __name__ == "__main__":
    unittest.main()
