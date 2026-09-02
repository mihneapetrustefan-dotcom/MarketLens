"""
tests/paper/test_adversarial.py
------------------------------------
The ten adversarial scenarios (spec §76), each written as an attempt to
corrupt the paper account that must be caught.

These differ from the rest of the suite in the same way Phase 12's did:
the others ask "does the code do what it says", these ask "what happens
when the world misbehaves". Every one of them is an ordinary occurrence
in a scheduled system — duplicate deliveries, restarts mid-tick, stale
caches, missing components — not an exotic edge case.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import MarketCalendar
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from src.domain.paper_models import (
    DataFreshness, HealthState, OrderSide, PaperAccountStatus, PaperOrderState,
    PaperOrderType, PaperRejectReason, PaperSessionStatus,
)
from src.paper.clock import FixedClock
from src.paper.controls import ControlLedger
from src.paper.executor import PaperExecutor
from src.paper.freshness import FreshnessMonitor
from src.paper.health import HealthMonitor
from src.paper.reconciliation import Reconciler
from src.paper.session import PaperTradingSession
from tests.paper.helpers import (
    END, START, anchors_for, flat_universe, make_account, make_config,
    make_connection, make_fill, make_order, make_session, signals_for,
    standard_universe,
)


class ExecutorCase(unittest.TestCase):
    """Shared fixture: one flat-priced instrument and a connected executor."""

    def setUp(self):
        self.conn = make_connection()
        flat_universe(self.conn)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-flat"])
        self.bars = self.calendar.bars("i-flat")
        self.ledger = PortfolioLedger(100_000.0, run_id="sess-1")
        self.executor = PaperExecutor(
            self.calendar, self.ledger, CostModel(commission_bps=2.0),
            SlippageModel(method=SlippageMethod.NONE),
            account_id="acct-1", session_id="sess-1", max_participation=None)
        self.executor.connect()

    def tearDown(self):
        self.conn.close()


class TestCase1_DuplicateMarketUpdate(ExecutorCase):
    """
    CASE 1 — the same market observation arrives twice.
    EXPECTED: no duplicate state mutation.
    """

    def test_re_evaluating_the_same_moment_is_stable(self):
        monitor = FreshnessMonitor(self.calendar)
        first = monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        second = monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        self.assertEqual(first.worst, second.worst)
        self.assertEqual(first.prices(), second.prices())

    def test_reading_a_price_twice_does_not_move_the_ledger(self):
        before = (self.ledger.cash, len(self.ledger.positions))
        self.calendar.bar_at_or_before("i-flat", self.bars[10].timestamp)
        self.calendar.bar_at_or_before("i-flat", self.bars[10].timestamp)
        self.assertEqual((self.ledger.cash, len(self.ledger.positions)), before)


class TestCase2_DuplicateOrderIntent(ExecutorCase):
    """
    CASE 2 — the same order intent is processed twice.
    EXPECTED: no duplicate paper order.
    """

    def test_the_same_idempotency_key_is_refused(self):
        first = self.executor.place_order(
            make_order(order_id="oA", idempotency_key="K1",
                       at=self.bars[10].timestamp), self.bars[10].timestamp)
        second = self.executor.place_order(
            make_order(order_id="oB", idempotency_key="K1",
                       at=self.bars[10].timestamp), self.bars[10].timestamp)
        self.assertEqual(first.state, PaperOrderState.ACCEPTED)
        self.assertEqual(second.state, PaperOrderState.REJECTED)
        self.assertEqual(second.reject_reason, PaperRejectReason.DUPLICATE)

    def test_the_key_is_derived_from_the_decision_not_wall_time(self):
        """A restart must reproduce the key, so it cannot include 'now'."""
        decided = self.bars[10].timestamp
        first = PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-1", 0.05)
        second = PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-1", 0.05)
        self.assertEqual(first, second)

    def test_a_different_decision_produces_a_different_key(self):
        decided = self.bars[10].timestamp
        base = PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-1", 0.05)
        for changed in (
            PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-1", 0.10),
            PaperExecutor.idempotency_key("s", "i-other", decided, "sig-1", 0.05),
            PaperExecutor.idempotency_key("s2", "i-flat", decided, "sig-1", 0.05),
            PaperExecutor.idempotency_key(
                "s", "i-flat", decided + timedelta(days=1), "sig-1", 0.05),
        ):
            self.assertNotEqual(base, changed)

    def test_two_signals_asking_for_the_same_target_are_one_order(self):
        """
        Several signals can be live for one instrument at the same
        moment, and the sizing strategy proposes a change for each. They
        all ask for the same target, so they are ONE order — keying on
        the signal would let each add a little more exposure, which is
        the duplicate this guard exists to prevent.
        """
        decided = self.bars[10].timestamp
        first = PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-1", 0.05)
        second = PaperExecutor.idempotency_key("s", "i-flat", decided, "sig-2", 0.05)
        self.assertEqual(first, second)

    def test_only_one_order_survives_a_duplicate(self):
        self.executor.place_order(
            make_order(order_id="oA", idempotency_key="K"), self.bars[10].timestamp)
        self.executor.place_order(
            make_order(order_id="oB", idempotency_key="K"), self.bars[10].timestamp)
        working = self.executor.get_orders(open_only=True)
        self.assertEqual(len(working), 1)


class TestCase3_StaleMarketData(ExecutorCase):
    """
    CASE 3 — market data goes stale.
    EXPECTED: new orders blocked.
    """

    def test_stale_data_rejects_the_order(self):
        order = self.executor.place_order(
            make_order(at=self.bars[10].timestamp), self.bars[10].timestamp,
            freshness=DataFreshness.STALE)
        self.assertEqual(order.state, PaperOrderState.REJECTED)
        self.assertEqual(order.reject_reason, PaperRejectReason.STALE_DATA)

    def test_invalid_and_unavailable_are_also_blocked(self):
        for freshness in (DataFreshness.INVALID, DataFreshness.UNAVAILABLE):
            order = self.executor.place_order(
                make_order(order_id=f"o-{freshness.value}",
                           at=self.bars[10].timestamp),
                self.bars[10].timestamp, freshness=freshness)
            self.assertEqual(order.state, PaperOrderState.REJECTED, freshness)

    def test_fresh_and_aging_are_permitted(self):
        for freshness in (DataFreshness.FRESH, DataFreshness.AGING):
            order = self.executor.place_order(
                make_order(order_id=f"ok-{freshness.value}",
                           idempotency_key=f"k-{freshness.value}",
                           at=self.bars[10].timestamp),
                self.bars[10].timestamp, freshness=freshness)
            self.assertEqual(order.state, PaperOrderState.ACCEPTED, freshness)

    def test_a_stale_price_still_values_the_book(self):
        """
        Blocking new orders is not the same as refusing to value what is
        already held — an operator must still see the position.
        """
        self.ledger.apply_fill(
            __import__("src.paper.executor", fromlist=["fill_to_simulated"])
            .fill_to_simulated(make_fill(at=self.bars[5].timestamp)))
        snapshot = self.ledger.mark_to_market(
            self.bars[20].timestamp, {"i-flat": 100.0})
        self.assertGreater(snapshot.equity, 0)


class TestCase4_RiskEngineUnavailable(unittest.TestCase):
    """
    CASE 4 — the risk engine fails.
    EXPECTED: no new orders.
    """

    def setUp(self):
        self.conn = make_connection()
        self.universe = standard_universe(self.conn)
        self.anchors = anchors_for(self.conn, self.universe, days=30)
        account = make_account()
        config = make_config(self.universe)
        self.runner = PaperTradingSession(
            self.conn, account, make_session(config),
            clock=FixedClock(self.anchors[0]),
            signals=signals_for(self.universe))

    def tearDown(self):
        self.conn.close()

    def test_a_raising_risk_engine_creates_no_orders(self):
        def explode(*args, **kwargs):
            raise RuntimeError("risk engine is down")
        self.runner.service.evaluate = explode

        result = self.runner.tick(self.anchors[-1])
        self.assertEqual(result.orders_created, 0)
        self.assertTrue(result.was_blocked)
        self.assertIn("risk", result.blocked_reason.lower())

    def test_the_failure_is_recorded_as_a_component_failure(self):
        def explode(*args, **kwargs):
            raise RuntimeError("down")
        self.runner.service.evaluate = explode
        self.runner.tick(self.anchors[-1])
        self.assertIn("risk", self.runner.health_monitor.failures)

    def test_the_tick_still_snapshots_and_reconciles(self):
        """A broken risk engine must not stop the book being observable."""
        def explode(*args, **kwargs):
            raise RuntimeError("down")
        self.runner.service.evaluate = explode
        result = self.runner.tick(self.anchors[-1])
        self.assertIsNotNone(result.snapshot)
        self.assertIsNotNone(result.reconciliation)


class TestCase5_ModelArtifactUnavailable(unittest.TestCase):
    """
    CASE 5 — no model is available, so no signal exists.
    EXPECTED: no signal, therefore no order.
    """

    def setUp(self):
        self.conn = make_connection()
        self.universe = standard_universe(self.conn)
        self.anchors = anchors_for(self.conn, self.universe, days=30)

    def tearDown(self):
        self.conn.close()

    def test_no_signals_produces_no_orders(self):
        runner = PaperTradingSession(
            self.conn, make_account(), make_session(make_config(self.universe)),
            clock=FixedClock(self.anchors[0]), signals=[])
        result = runner.tick(self.anchors[-1])
        self.assertEqual(result.signals_observed, 0)
        self.assertEqual(result.orders_created, 0)

    def test_an_empty_signal_table_does_not_raise(self):
        """The database has no signals at all — the tick must still run."""
        runner = PaperTradingSession(
            self.conn, make_account(), make_session(make_config(self.universe)),
            clock=FixedClock(self.anchors[0]))
        result = runner.tick(self.anchors[-1])
        self.assertEqual(result.orders_created, 0)
        self.assertIsNotNone(result.snapshot)


class TestCase6_IncompatibleSignal(unittest.TestCase):
    """
    CASE 6 — a signal that the pipeline cannot act on.
    EXPECTED: no order.

    Phase 10 already encodes incompatibility as suppression, so the
    honest test is that a suppressed signal never becomes exposure.
    """

    def setUp(self):
        self.conn = make_connection()
        self.universe = standard_universe(self.conn)
        self.anchors = anchors_for(self.conn, self.universe, days=30)

    def tearDown(self):
        self.conn.close()

    def test_suppressed_signals_never_trade(self):
        suppressed = signals_for(self.universe, suppressed=True)
        runner = PaperTradingSession(
            self.conn, make_account(), make_session(make_config(self.universe)),
            clock=FixedClock(self.anchors[0]), signals=suppressed)
        for anchor in self.anchors:
            result = runner.tick(anchor)
            self.assertEqual(result.orders_created, 0)

    def test_a_signal_for_an_unknown_instrument_is_rejected(self):
        conn = self.conn
        calendar = MarketCalendar(conn)
        calendar.load(self.universe)
        ledger = PortfolioLedger(100_000.0)
        executor = PaperExecutor(calendar, ledger, CostModel(),
                                 SlippageModel(method=SlippageMethod.NONE))
        executor.connect()
        order = executor.place_order(
            make_order(instrument_id="i-nowhere", at=self.anchors[-1]),
            self.anchors[-1])
        self.assertEqual(order.reject_reason,
                         PaperRejectReason.UNKNOWN_INSTRUMENT)


class TestCase7_MarketClosesWithSignalActive(ExecutorCase):
    """
    CASE 7 — the market closes while a signal is still active.
    EXPECTED: handling that matches the order type and validity.
    """

    def test_a_day_order_expires_rather_than_filling_later(self):
        order = self.executor.place_order(
            make_order(at=self.bars[10].timestamp), self.bars[10].timestamp)
        expired = self.executor.expire_stale_orders(
            self.bars[10].timestamp + timedelta(days=3))
        self.assertIn(order, expired)
        self.assertEqual(order.state, PaperOrderState.EXPIRED)

    def test_a_gtc_order_survives_the_close(self):
        from src.domain.paper_models import TimeInForce
        order = self.executor.place_order(
            make_order(time_in_force=TimeInForce.GTC, at=self.bars[10].timestamp),
            self.bars[10].timestamp)
        self.assertIsNone(order.expires_at)
        self.executor.expire_stale_orders(
            self.bars[10].timestamp + timedelta(days=30))
        self.assertTrue(order.state.is_working)

    def test_no_session_after_the_order_means_no_fill(self):
        order = self.executor.place_order(
            make_order(at=self.bars[-1].timestamp), self.bars[-1].timestamp)
        self.assertEqual(self.executor.try_fill(order, self.bars[-1].timestamp), [])


class TestCase8_DuplicateFillMessage(ExecutorCase):
    """
    CASE 8 — a fill message is delivered more than once.
    EXPECTED: idempotent processing.
    """

    def test_replaying_a_fill_does_not_double_the_position(self):
        order = self.executor.place_order(
            make_order(idempotency_key="K", at=self.bars[10].timestamp),
            self.bars[10].timestamp)
        fills = self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertEqual(len(fills), 1)

        quantity_before = self.ledger.positions["i-flat"].quantity
        cash_before = self.ledger.cash
        applied = self.executor.apply_fill(order, fills[0])

        self.assertFalse(applied)
        self.assertAlmostEqual(self.ledger.positions["i-flat"].quantity,
                               quantity_before)
        self.assertAlmostEqual(self.ledger.cash, cash_before)

    def test_the_order_filled_quantity_is_not_doubled(self):
        order = self.executor.place_order(
            make_order(idempotency_key="K", at=self.bars[10].timestamp),
            self.bars[10].timestamp)
        fill = self.executor.try_fill(order, self.bars[11].timestamp)[0]
        filled_before = order.filled_quantity
        self.executor.apply_fill(order, fill)
        self.assertAlmostEqual(order.filled_quantity, filled_before)

    def test_a_genuinely_new_fill_is_still_applied(self):
        """The guard must not block legitimate subsequent fills."""
        order = self.executor.place_order(
            make_order(quantity=20.0, idempotency_key="K",
                       at=self.bars[10].timestamp), self.bars[10].timestamp)
        first = self.executor.try_fill(order, self.bars[11].timestamp)
        self.assertTrue(first)
        self.assertEqual(order.state, PaperOrderState.FILLED)


class TestCase9_ExecutionPriceUnavailable(ExecutorCase):
    """
    CASE 9 — no execution price is available.
    EXPECTED: safe rejection or deferral, never a guessed price.
    """

    def test_an_instrument_with_no_bars_is_rejected(self):
        order = self.executor.place_order(
            make_order(instrument_id="i-missing", at=self.bars[10].timestamp),
            self.bars[10].timestamp)
        self.assertEqual(order.state, PaperOrderState.REJECTED)
        self.assertIn(order.reject_reason,
                      (PaperRejectReason.UNKNOWN_INSTRUMENT,
                       PaperRejectReason.NO_PRICE,
                       PaperRejectReason.MARKET_CLOSED))

    def test_a_resting_order_defers_rather_than_guessing(self):
        """A limit that has not been reached waits; it never invents a fill."""
        order = self.executor.place_order(
            make_order(order_type=PaperOrderType.LIMIT, limit_price=1.0,
                       at=self.bars[10].timestamp), self.bars[10].timestamp)
        self.assertEqual(self.executor.try_fill(order, self.bars[11].timestamp), [])
        self.assertTrue(order.state.is_working)

    def test_no_fill_is_produced_without_a_bar(self):
        order = self.executor.place_order(
            make_order(at=self.bars[10].timestamp), self.bars[10].timestamp)
        before = len(self.ledger.positions)
        self.executor.try_fill(order, self.bars[10].timestamp)   # deciding bar
        self.assertEqual(len(self.ledger.positions), before)


class TestCase10_ImpossibleNegativeCash(ExecutorCase):
    """
    CASE 10 — the account reaches impossible negative cash.
    EXPECTED: reconciliation raises it rather than hiding it.
    """

    def test_reconciliation_reports_negative_cash(self):
        reconciler = Reconciler(100_000.0)
        self.ledger.cash = -5_000.0        # corrupt the state directly
        result = reconciler.reconcile("sess-1", END, [], [], self.ledger)

        self.assertFalse(result.is_clean)
        self.assertIn("negative_cash", [d.kind for d in result.discrepancies])

    def test_the_discrepancy_is_not_silently_repaired(self):
        reconciler = Reconciler(100_000.0)
        self.ledger.cash = -5_000.0
        reconciler.reconcile("sess-1", END, [], [], self.ledger)
        self.assertAlmostEqual(self.ledger.cash, -5_000.0)

    def test_a_buy_beyond_cash_is_refused_before_it_can_happen(self):
        """The first defence: the executor will not fill what it cannot fund."""
        order = self.executor.place_order(
            make_order(quantity=100_000.0, at=self.bars[10].timestamp),
            self.bars[10].timestamp)
        self.executor.try_fill(order, self.bars[11].timestamp,
                               available_cash=10.0)
        self.assertGreaterEqual(self.ledger.cash, -1e-6)

    def test_a_session_enters_safe_mode_on_a_dirty_reconciliation(self):
        conn = make_connection()
        universe = standard_universe(conn)
        anchors = anchors_for(conn, universe, days=20)
        runner = PaperTradingSession(
            conn, make_account(), make_session(make_config(universe)),
            clock=FixedClock(anchors[0]), signals=[])
        runner.prepare()
        runner.ledger.cash = -1_000.0      # corrupt before the tick

        result = runner.tick(anchors[-1])
        self.assertFalse(result.reconciliation.is_clean)
        self.assertTrue(runner.health_monitor.safe_mode)
        conn.close()


if __name__ == "__main__":
    unittest.main()
