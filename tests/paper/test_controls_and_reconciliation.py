"""
tests/paper/test_controls_and_reconciliation.py
----------------------------------------------------
Safety controls and ledger reconciliation
(spec §32, §38, §39, §40, §62, §72, §74).

The controls tests defend one asymmetry above all: a safeguard must
never trap a position it was trying to protect. Reduce-only and the
circuit breakers block INCREASES and let reductions through, and that
distinction is checked from several directions because getting it
backwards turns a safety feature into a hazard.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.accounting import PortfolioLedger
from src.domain.paper_models import (
    OrderSide, PaperAccountStatus, PaperRejectReason, PaperSessionStatus,
)
from src.paper.controls import (
    CircuitBreakers, ControlDecision, ControlLedger, RateLimits,
)
from src.paper.executor import fill_to_simulated
from src.paper.reconciliation import CASH_TOLERANCE, Reconciler
from tests.paper.helpers import END, make_fill, make_order

T = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def allow_args(**overrides):
    defaults = dict(
        at=T, account_status=PaperAccountStatus.ACTIVE,
        session_status=PaperSessionStatus.RUNNING, is_increase=True,
        health_allows=True, equity=100_000.0, day_start_equity=100_000.0,
        peak_equity=100_000.0, gross_exposure=10_000.0)
    defaults.update(overrides)
    return defaults


class TestRateLimits(unittest.TestCase):
    def test_under_the_tick_limit_passes(self):
        self.assertTrue(RateLimits(max_orders_per_tick=5).check_tick(3).allowed)

    def test_at_the_tick_limit_blocks(self):
        decision = RateLimits(max_orders_per_tick=5).check_tick(5)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, PaperRejectReason.RATE_LIMITED)

    def test_the_blocking_control_names_itself(self):
        """Spec §40 forbids hiding the rejection reason."""
        decision = RateLimits(max_orders_per_tick=2).check_tick(2)
        self.assertEqual(decision.control, "max_orders_per_tick")
        self.assertIn("limit", decision.detail)

    def test_the_daily_limit_is_separate_from_the_tick_limit(self):
        limits = RateLimits(max_orders_per_tick=100, max_orders_per_day=10)
        self.assertTrue(limits.check_tick(50).allowed)
        self.assertFalse(limits.check_day(10).allowed)


class TestCircuitBreakers(unittest.TestCase):
    def test_within_the_daily_loss_limit_passes(self):
        breakers = CircuitBreakers(daily_loss_limit_pct=0.05)
        self.assertTrue(breakers.check(98_000.0, 100_000.0, 100_000.0, 0).allowed)

    def test_breaching_the_daily_loss_limit_blocks(self):
        breakers = CircuitBreakers(daily_loss_limit_pct=0.05)
        decision = breakers.check(94_000.0, 100_000.0, 100_000.0, 0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.control, "daily_loss_limit")

    def test_breaching_max_drawdown_blocks(self):
        breakers = CircuitBreakers(daily_loss_limit_pct=None, max_drawdown_pct=0.20)
        decision = breakers.check(75_000.0, 75_000.0, 100_000.0, 0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.control, "max_drawdown")

    def test_breaching_gross_exposure_blocks(self):
        breakers = CircuitBreakers(daily_loss_limit_pct=None,
                                   max_drawdown_pct=None,
                                   max_gross_exposure_pct=1.0)
        decision = breakers.check(100_000.0, 100_000.0, 100_000.0, 150_000.0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.control, "max_gross_exposure")

    def test_zero_equity_does_not_divide(self):
        self.assertTrue(CircuitBreakers().check(0.0, 100.0, 100.0, 0.0).allowed)

    def test_disabled_limits_never_fire(self):
        breakers = CircuitBreakers(daily_loss_limit_pct=None,
                                   max_drawdown_pct=None,
                                   max_gross_exposure_pct=None)
        self.assertTrue(breakers.check(1.0, 100_000.0, 100_000.0, 1e9).allowed)


class TestControlGate(unittest.TestCase):
    def setUp(self):
        self.controls = ControlLedger()

    def test_a_healthy_active_session_allows_orders(self):
        self.assertTrue(self.controls.may_create_order(**allow_args()).allowed)

    def test_a_paused_account_blocks(self):
        decision = self.controls.may_create_order(
            **allow_args(account_status=PaperAccountStatus.PAUSED))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.control, "account_status")

    def test_emergency_stop_blocks(self):
        decision = self.controls.may_create_order(
            **allow_args(account_status=PaperAccountStatus.EMERGENCY_STOP))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.control, "emergency_stop")

    def test_a_stopped_session_blocks(self):
        decision = self.controls.may_create_order(
            **allow_args(session_status=PaperSessionStatus.COMPLETED))
        self.assertFalse(decision.allowed)

    def test_unhealthy_pipeline_blocks(self):
        """Spec §61 — fail safe, not open."""
        decision = self.controls.may_create_order(
            **allow_args(health_allows=False, health_detail="model failed"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, PaperRejectReason.SAFE_MODE)


class TestReductionsAreAlwaysPermitted(unittest.TestCase):
    """
    The asymmetry that matters: a safeguard must not trap the position
    it is protecting.
    """

    def setUp(self):
        self.controls = ControlLedger(
            breakers=CircuitBreakers(daily_loss_limit_pct=0.01))

    def test_reduce_only_blocks_increases(self):
        decision = self.controls.may_create_order(
            **allow_args(account_status=PaperAccountStatus.REDUCE_ONLY,
                         is_increase=True))
        self.assertFalse(decision.allowed)

    def test_reduce_only_permits_reductions(self):
        decision = self.controls.may_create_order(
            **allow_args(account_status=PaperAccountStatus.REDUCE_ONLY,
                         is_increase=False))
        self.assertTrue(decision.allowed)

    def test_a_tripped_breaker_blocks_increases(self):
        decision = self.controls.may_create_order(
            **allow_args(equity=50_000.0, day_start_equity=100_000.0,
                         is_increase=True))
        self.assertFalse(decision.allowed)

    def test_a_tripped_breaker_still_permits_exits(self):
        decision = self.controls.may_create_order(
            **allow_args(equity=50_000.0, day_start_equity=100_000.0,
                         is_increase=False))
        self.assertTrue(decision.allowed)

    def test_a_paused_instrument_blocks_increases_but_not_exits(self):
        self.controls.pause_instrument("s", "i-x", T)
        self.assertFalse(self.controls.may_create_order(
            **allow_args(instrument_id="i-x", is_increase=True)).allowed)
        self.assertTrue(self.controls.may_create_order(
            **allow_args(instrument_id="i-x", is_increase=False)).allowed)


class TestTargetedPauses(unittest.TestCase):
    def setUp(self):
        self.controls = ControlLedger()

    def test_pausing_and_resuming_an_instrument(self):
        self.controls.pause_instrument("s", "i-x", T)
        self.assertIn("i-x", self.controls.paused_instruments)
        self.controls.resume_instrument("s", "i-x", T)
        self.assertNotIn("i-x", self.controls.paused_instruments)

    def test_pausing_a_strategy_blocks_only_that_strategy(self):
        self.controls.pause_strategy("s", "momentum", T)
        self.assertFalse(self.controls.may_create_order(
            **allow_args(strategy_id="momentum")).allowed)
        self.assertTrue(self.controls.may_create_order(
            **allow_args(strategy_id="event_driven")).allowed)


class TestAuditTrail(unittest.TestCase):
    """Spec §72, §74 — five facts per change, and no way to rewrite them."""

    def setUp(self):
        self.controls = ControlLedger()

    def test_a_configuration_change_records_all_five_facts(self):
        action = self.controls.record_configuration_change(
            "s", "max_orders_per_day", "200", "50", T, "operator",
            "reducing after a noisy session")
        self.assertEqual(action.previous_value, "200")
        self.assertEqual(action.new_value, "50")
        self.assertEqual(action.actor, "operator")
        self.assertTrue(action.reason)
        self.assertEqual(action.at, T)

    def test_the_trail_accumulates(self):
        self.controls.pause_instrument("s", "a", T)
        self.controls.resume_instrument("s", "a", T)
        self.assertEqual(len(self.controls.audit_trail()), 2)

    def test_the_returned_trail_is_a_copy(self):
        """A caller must not be able to rewrite history by mutating the list."""
        self.controls.pause_instrument("s", "a", T)
        trail = self.controls.audit_trail()
        trail.clear()
        self.assertEqual(len(self.controls.audit_trail()), 1)


class TestOrderCounting(unittest.TestCase):
    def setUp(self):
        self.controls = ControlLedger(RateLimits(max_orders_per_tick=2))

    def test_the_tick_counter_resets(self):
        self.controls.record_order(T)
        self.controls.record_order(T)
        self.assertFalse(self.controls.may_create_order(**allow_args()).allowed)
        self.controls.begin_tick()
        self.assertTrue(self.controls.may_create_order(**allow_args()).allowed)

    def test_the_daily_counter_does_not_reset_with_the_tick(self):
        self.controls.record_order(T)
        self.controls.begin_tick()
        self.assertEqual(self.controls.orders_today(T), 1)

    def test_orders_are_counted_per_calendar_day(self):
        self.controls.record_order(T)
        self.controls.record_order(T + timedelta(days=1))
        self.assertEqual(self.controls.orders_today(T), 1)


class TestReconciliation(unittest.TestCase):
    def setUp(self):
        self.reconciler = Reconciler(100_000.0)
        self.ledger = PortfolioLedger(100_000.0, run_id="s")

    def _apply(self, fill):
        self.ledger.apply_fill(fill_to_simulated(fill))

    def test_a_clean_book_reconciles(self):
        fill = make_fill(quantity=10.0, price=100.0, commission=1.0,
                         slippage_cost=0.5)
        self._apply(fill)
        order = make_order(quantity=10.0)
        order.filled_quantity = 10.0

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        self.assertTrue(result.is_clean)
        self.assertEqual(result.checks_performed, 5)

    def test_a_clean_result_records_that_it_checked(self):
        """"We verified and it balanced" must not look like "we never verified"."""
        result = self.reconciler.reconcile("s", END, [], [], self.ledger)
        self.assertTrue(result.is_clean)
        self.assertGreater(result.checks_performed, 0)

    def test_an_orphan_fill_is_reported(self):
        fill = make_fill(order_id="unknown")
        self._apply(fill)
        result = self.reconciler.reconcile("s", END, [], [fill], self.ledger)
        self.assertIn("orphan_fill", [d.kind for d in result.discrepancies])

    def test_a_position_mismatch_is_reported(self):
        """A duplicate application leaves the position larger than its fills."""
        fill = make_fill(quantity=10.0)
        self._apply(fill)
        self._apply(fill)          # applied twice, deliberately
        order = make_order(quantity=10.0)
        order.filled_quantity = 10.0

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        self.assertFalse(result.is_clean)
        self.assertIn("position_mismatch", [d.kind for d in result.discrepancies])

    def test_an_order_fill_mismatch_is_reported(self):
        fill = make_fill(quantity=10.0)
        self._apply(fill)
        order = make_order(quantity=10.0)
        order.filled_quantity = 4.0        # disagrees with its fills

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        self.assertIn("order_fill_mismatch", [d.kind for d in result.discrepancies])

    def test_a_cash_mismatch_is_reported(self):
        fill = make_fill(quantity=10.0, price=100.0, commission=1.0)
        self._apply(fill)
        order = make_order(quantity=10.0)
        order.filled_quantity = 10.0
        self.ledger.cash += 500.0          # corrupt the balance

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        self.assertIn("cash_mismatch", [d.kind for d in result.discrepancies])

    def test_negative_cash_is_reported_as_impossible(self):
        self.ledger.cash = -1.0
        result = self.reconciler.reconcile("s", END, [], [], self.ledger)
        self.assertIn("negative_cash", [d.kind for d in result.discrepancies])

    def test_an_overfill_is_reported(self):
        fill = make_fill(quantity=20.0)
        self._apply(fill)
        order = make_order(quantity=10.0)
        order.filled_quantity = 20.0

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        self.assertIn("overfill", [d.kind for d in result.discrepancies])

    def test_discrepancies_are_never_repaired(self):
        """Spec §32 — surface, do not silently fix."""
        self.ledger.cash = -500.0
        self.reconciler.reconcile("s", END, [], [], self.ledger)
        self.assertAlmostEqual(self.ledger.cash, -500.0)

    def test_the_difference_is_quantified(self):
        fill = make_fill(quantity=10.0, price=100.0)
        self._apply(fill)
        order = make_order(quantity=10.0)
        order.filled_quantity = 10.0
        self.ledger.cash += 250.0

        result = self.reconciler.reconcile("s", END, [order], [fill], self.ledger)
        cash = next(d for d in result.discrepancies if d.kind == "cash_mismatch")
        self.assertAlmostEqual(cash.difference, 250.0, places=4)

    def test_the_description_summarises_by_kind(self):
        self.ledger.cash = -1.0
        result = self.reconciler.reconcile("s", END, [], [], self.ledger)
        described = self.reconciler.describe(result)
        self.assertFalse(described["clean"])
        self.assertIn("negative_cash", described["by_kind"])


if __name__ == "__main__":
    unittest.main()
