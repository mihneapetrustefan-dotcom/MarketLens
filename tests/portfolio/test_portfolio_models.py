"""
tests/portfolio/test_portfolio_models.py
---------------------------------------------
Tests for the Phase 11 domain model.

These defend the invariants the rest of the phase relies on: that a
short is arithmetically a short everywhere, that an unknown quantity
stays None instead of becoming zero, that NaN and Infinity can never
reach a caller as a valid number, and that weights against unusable
equity are undefined rather than silently wrong.
"""

import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import (
    AllocationChange, ConstraintScope, ConstraintSeverity, ConstraintSet,
    OrderIntent, PortfolioSnapshot, Position, PositionValuation, RiskConstraint,
    RiskDecision, RiskDecisionState, TradingState, ValuationStatus,
    finite_or_none, safe_ratio,
)
from tests.portfolio.helpers import AS_OF, make_snapshot, make_valuation


class TestNumericGuards(unittest.TestCase):
    """Spec §51: NaN and Infinity must never present as valid risk numbers."""

    def test_nan_becomes_none(self):
        self.assertIsNone(finite_or_none(float("nan")))

    def test_infinity_becomes_none(self):
        self.assertIsNone(finite_or_none(float("inf")))
        self.assertIsNone(finite_or_none(float("-inf")))

    def test_ordinary_value_passes_through(self):
        self.assertEqual(finite_or_none(0.25), 0.25)

    def test_zero_is_preserved_not_treated_as_missing(self):
        self.assertEqual(finite_or_none(0.0), 0.0)

    def test_none_stays_none(self):
        self.assertIsNone(finite_or_none(None))

    def test_division_by_zero_is_none_not_infinity(self):
        self.assertIsNone(safe_ratio(1.0, 0.0))

    def test_ratio_with_missing_operand_is_none(self):
        self.assertIsNone(safe_ratio(None, 2.0))
        self.assertIsNone(safe_ratio(2.0, None))


class TestPosition(unittest.TestCase):
    def test_negative_quantity_is_a_short(self):
        position = Position("p", "pf", "i", -10.0)
        self.assertTrue(position.is_short)
        self.assertFalse(position.is_long)

    def test_non_finite_quantity_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Position("p", "pf", "i", float("nan"))

    def test_cost_basis_is_none_without_an_entry_price(self):
        self.assertIsNone(Position("p", "pf", "i", 10.0).cost_basis)

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            Position("p", "pf", "i", 1.0, opened_at=datetime(2026, 1, 1))


class TestPositionValuation(unittest.TestCase):
    def test_long_market_value_is_positive(self):
        valuation = make_valuation("i", 10.0, 20.0)
        self.assertEqual(valuation.market_value, 200.0)

    def test_short_market_value_is_negative_but_exposure_is_positive(self):
        valuation = make_valuation("i", -10.0, 20.0)
        self.assertEqual(valuation.market_value, -200.0)
        self.assertEqual(valuation.exposure, 200.0)

    def test_unpriced_position_has_no_value_rather_than_zero(self):
        valuation = make_valuation("i", 10.0, None,
                                   status=ValuationStatus.MISSING_PRICE)
        self.assertIsNone(valuation.market_value)
        self.assertIsNone(valuation.exposure)
        self.assertFalse(valuation.is_valued)

    def test_unrealized_pnl_is_correct_for_a_short(self):
        # Entered short at 100, price rose to 120: a short loses here.
        valuation = make_valuation("i", -10.0, 120.0, entry=100.0)
        self.assertEqual(valuation.unrealized_pnl, -200.0)

    def test_unrealized_pnl_is_correct_for_a_long(self):
        valuation = make_valuation("i", 10.0, 120.0, entry=100.0)
        self.assertEqual(valuation.unrealized_pnl, 200.0)


class TestPortfolioSnapshot(unittest.TestCase):
    def test_equity_is_cash_plus_signed_position_value(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 20.0)], cash=100.0)
        self.assertEqual(snapshot.equity, 300.0)

    def test_short_reduces_equity_but_increases_gross_exposure(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 20.0),
            make_valuation("b", -5.0, 20.0),
        ], cash=100.0)
        self.assertEqual(snapshot.gross_exposure, 300.0)
        self.assertEqual(snapshot.net_exposure, 100.0)
        self.assertEqual(snapshot.equity, 200.0)

    def test_leverage_is_none_at_zero_equity_not_infinity(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 20.0),
                                  make_valuation("b", -10.0, 20.0)], cash=0.0)
        self.assertEqual(snapshot.equity, 0.0)
        self.assertIsNone(snapshot.leverage)

    def test_weight_is_none_at_zero_equity(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 20.0),
                                  make_valuation("b", -10.0, 20.0)], cash=0.0)
        self.assertIsNone(snapshot.weight_of("a"))

    def test_empty_portfolio_is_empty_and_complete(self):
        snapshot = make_snapshot([], cash=1000.0)
        self.assertTrue(snapshot.is_empty)
        self.assertTrue(snapshot.is_complete)
        self.assertEqual(snapshot.equity, 1000.0)

    def test_unpriced_position_makes_the_snapshot_incomplete(self):
        snapshot = make_snapshot(
            [make_valuation("a", 10.0, 20.0)], cash=100.0,
            unvalued=[make_valuation("b", 5.0, None,
                                     status=ValuationStatus.MISSING_PRICE)])
        self.assertFalse(snapshot.is_complete)

    def test_stale_price_is_counted_but_does_not_make_it_incomplete(self):
        """Stale and missing are different problems and must stay distinguishable."""
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 20.0, status=ValuationStatus.STALE_PRICE,
                           age_days=30.0)], cash=100.0)
        self.assertTrue(snapshot.is_complete)
        self.assertTrue(snapshot.has_stale_prices)
        self.assertEqual(len(snapshot.stale_valuations), 1)
        # It still contributes to equity — dropping it would understate
        # the denominator of every weight.
        self.assertEqual(snapshot.equity, 300.0)

    def test_multi_currency_is_flagged(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 20.0, currency="USD"),
            make_valuation("b", 10.0, 20.0, currency="EUR"),
        ])
        self.assertTrue(snapshot.is_multi_currency)

    def test_single_currency_is_not_flagged(self):
        self.assertFalse(make_snapshot([make_valuation("a", 10.0, 20.0)]).is_multi_currency)

    def test_weight_uses_absolute_exposure_so_a_short_counts(self):
        snapshot = make_snapshot([make_valuation("a", -10.0, 20.0)], cash=1000.0)
        self.assertEqual(snapshot.equity, 800.0)
        self.assertAlmostEqual(snapshot.weight_of("a"), 200.0 / 800.0)


class TestRiskConstraint(unittest.TestCase):
    def test_constraint_without_any_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            RiskConstraint("c", ConstraintScope.LEVERAGE)

    def test_inverted_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            RiskConstraint("c", ConstraintScope.LEVERAGE, min_value=2.0, max_value=1.0)

    def test_value_below_maximum_holds(self):
        constraint = RiskConstraint("c", ConstraintScope.LEVERAGE, max_value=1.5)
        self.assertIsNone(constraint.evaluate(1.2))

    def test_value_exactly_at_maximum_holds(self):
        """Spec §52: a value exactly at the limit is inside it, not a breach."""
        constraint = RiskConstraint("c", ConstraintScope.LEVERAGE, max_value=1.5)
        self.assertIsNone(constraint.evaluate(1.5))

    def test_value_above_maximum_breaches(self):
        constraint = RiskConstraint("c", ConstraintScope.LEVERAGE, max_value=1.5)
        self.assertIsNotNone(constraint.evaluate(1.51))

    def test_value_exactly_at_minimum_holds(self):
        constraint = RiskConstraint("c", ConstraintScope.MIN_SIGNAL_CONFIDENCE,
                                    min_value=0.4)
        self.assertIsNone(constraint.evaluate(0.4))

    def test_value_below_minimum_breaches(self):
        constraint = RiskConstraint("c", ConstraintScope.MIN_SIGNAL_CONFIDENCE,
                                    min_value=0.4)
        self.assertIsNotNone(constraint.evaluate(0.39))

    def test_unmeasured_value_is_not_reported_as_a_breach(self):
        """None means 'not measured'; the engine, not the constraint, decides what that costs."""
        constraint = RiskConstraint("c", ConstraintScope.LEVERAGE, max_value=1.5)
        self.assertIsNone(constraint.evaluate(None))

    def test_nan_is_not_reported_as_a_breach(self):
        constraint = RiskConstraint("c", ConstraintScope.LEVERAGE, max_value=1.5)
        self.assertIsNone(constraint.evaluate(float("nan")))


class TestConstraintSet(unittest.TestCase):
    def setUp(self):
        self.constraint_set = ConstraintSet(constraints=[
            RiskConstraint("general", ConstraintScope.SECTOR_WEIGHT, max_value=0.40),
            RiskConstraint("tech", ConstraintScope.SECTOR_WEIGHT, max_value=0.45,
                           applies_to="technology"),
            RiskConstraint("off", ConstraintScope.LEVERAGE, max_value=1.0, enabled=False),
        ])

    def test_specific_constraint_wins_over_general(self):
        found = self.constraint_set.first(ConstraintScope.SECTOR_WEIGHT, "technology")
        self.assertEqual(found.constraint_id, "tech")

    def test_general_constraint_applies_to_other_keys(self):
        found = self.constraint_set.first(ConstraintScope.SECTOR_WEIGHT, "energy")
        self.assertEqual(found.constraint_id, "general")

    def test_disabled_constraint_is_not_returned(self):
        self.assertIsNone(self.constraint_set.first(ConstraintScope.LEVERAGE))

    def test_absent_scope_returns_none(self):
        self.assertIsNone(self.constraint_set.first(ConstraintScope.DRAWDOWN))


class TestAllocationChange(unittest.TestCase):
    def test_increase_is_detected(self):
        self.assertTrue(AllocationChange("i", current_weight=0.1,
                                         target_weight=0.2).is_increase)

    def test_reduction_is_detected(self):
        self.assertTrue(AllocationChange("i", current_weight=0.2,
                                         target_weight=0.1).is_reduction)

    def test_unknown_weights_are_neither(self):
        change = AllocationChange("i", current_weight=None, target_weight=0.2)
        self.assertFalse(change.is_increase)
        self.assertFalse(change.is_reduction)
        self.assertIsNone(change.weight_delta)


class TestRiskDecision(unittest.TestCase):
    def test_only_approved_and_reduced_permit_exposure(self):
        for state, expected in [
            (RiskDecisionState.APPROVED, True),
            (RiskDecisionState.REDUCED, True),
            (RiskDecisionState.REJECTED, False),
            (RiskDecisionState.REQUIRES_REVIEW, False),
            (RiskDecisionState.INSUFFICIENT_DATA, False),
        ]:
            decision = RiskDecision("d", "pf", state, AS_OF)
            self.assertEqual(decision.is_approved, expected, state.value)


class TestOrderIntent(unittest.TestCase):
    def test_intent_cannot_be_built_from_a_rejected_decision(self):
        decision = RiskDecision("d", "pf", RiskDecisionState.REJECTED, AS_OF)
        with self.assertRaises(ValueError):
            OrderIntent.require_approval(decision)

    def test_intent_is_allowed_from_an_approved_decision(self):
        decision = RiskDecision("d", "pf", RiskDecisionState.APPROVED, AS_OF)
        OrderIntent.require_approval(decision)      # must not raise

    def test_intent_is_never_executable(self):
        intent = OrderIntent("i", "pf", "inst", "buy")
        self.assertFalse(intent.is_executable)

    def test_invalid_side_is_rejected(self):
        with self.assertRaises(ValueError):
            OrderIntent("i", "pf", "inst", "short")

    def test_intent_carries_no_broker_fields(self):
        """
        Structural guard for spec §57: if a later change adds an account,
        venue or order id here, this test fails and the decision to cross
        the execution boundary has to be made deliberately.
        """
        forbidden = {"account", "account_id", "venue", "broker", "broker_id",
                     "order_id", "credentials", "api_key", "fill", "executed_at"}
        fields = set(OrderIntent("i", "pf", "inst", "buy").__dict__)
        self.assertEqual(fields & forbidden, set())


if __name__ == "__main__":
    unittest.main()
