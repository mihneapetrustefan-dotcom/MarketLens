"""
tests/portfolio/test_edge_cases.py
---------------------------------------
Numerical and structural edge cases (spec §51, §52).

These are the inputs that make a risk system produce a confident,
meaningless answer: an empty book, a single position, perfectly
correlated holdings, zero volume, negative equity, extreme values. In
every case the requirement is the same — either a correct number or an
explicit "cannot measure", and never NaN, never Infinity, and never a
zero standing in for an unknown.
"""

import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import (
    RiskDecisionState, ValuationStatus, finite_or_none,
)
from src.portfolio import analytics
from src.portfolio.constraints import default_constraint_set
from src.portfolio.risk_engine import RiskEngine
from tests.portfolio.helpers import (
    AS_OF, make_snapshot, make_valuation,
)
from tests.portfolio.test_risk_engine import build_inputs


class TestExtremeValues(unittest.TestCase):
    def test_enormous_position_does_not_overflow_into_infinity(self):
        snapshot = make_snapshot(
            [make_valuation("a", 1e12, 1e12)], cash=1e6)
        metrics = analytics.compute_concentration(snapshot)
        self.assertIsNotNone(metrics.hhi)
        self.assertTrue(math.isfinite(metrics.hhi))

    def test_tiny_position_does_not_underflow_to_a_wrong_zero(self):
        snapshot = make_snapshot(
            [make_valuation("a", 1e-9, 1.0)], cash=1000.0)
        weight = snapshot.weight_of("a")
        self.assertIsNotNone(weight)
        self.assertGreaterEqual(weight, 0.0)

    def test_extreme_returns_still_produce_finite_volatility(self):
        returns = [5.0, -0.9] * 40
        estimate = analytics.compute_volatility(returns, 365)
        self.assertTrue(estimate.insufficient_data or math.isfinite(estimate.value))

    def test_extreme_returns_still_produce_finite_var(self):
        returns = [-0.99] * 30 + [10.0] * 70
        result = analytics.compute_value_at_risk(returns)
        if not result.insufficient_data:
            self.assertTrue(math.isfinite(result.value))
            self.assertTrue(math.isfinite(result.expected_shortfall))


class TestDegenerateBooks(unittest.TestCase):
    def test_single_position_book_is_fully_concentrated(self):
        snapshot = make_snapshot([make_valuation("a", 1.0, 100.0)], cash=0.0)
        metrics = analytics.compute_concentration(snapshot)
        self.assertAlmostEqual(metrics.hhi, 1.0)
        self.assertAlmostEqual(metrics.effective_positions, 1.0)

    def test_perfectly_correlated_holdings_are_flagged(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        timestamps = [base + timedelta(days=i) for i in range(60)]
        values = [0.01 if i % 2 else -0.01 for i in range(60)]
        summary = analytics.compute_correlation_summary({
            "a": list(zip(timestamps, values)),
            "b": list(zip(timestamps, values)),
            "c": list(zip(timestamps, values)),
        })
        self.assertEqual(summary.computed_pairs, 3)
        self.assertEqual(len(summary.highly_correlated_pairs), 3)
        self.assertAlmostEqual(summary.average_correlation, 1.0, places=6)

    def test_long_and_short_of_equal_size_nets_to_zero_equity(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 100.0),
            make_valuation("b", -10.0, 100.0),
        ], cash=0.0)
        self.assertEqual(snapshot.equity, 0.0)
        self.assertEqual(snapshot.gross_exposure, 2000.0)
        self.assertIsNone(snapshot.leverage)

    def test_negative_equity_book_is_refused_not_approved(self):
        snapshot = make_snapshot([make_valuation("a", -100.0, 100.0)], cash=1000.0)
        self.assertLess(snapshot.equity, 0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)

    def test_empty_book_with_negative_cash_is_refused(self):
        """
        No positions but a negative balance is a debt, not a clean
        slate — it must not slip through the "nothing to measure" path
        and come back approved.
        """
        snapshot = make_snapshot([], cash=-500.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)
        self.assertIn("negative", decision.summary)

    def test_empty_book_with_zero_cash_is_measurable(self):
        """Zero is a knowable state; there is simply nothing to weigh against it."""
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(make_snapshot([], cash=0.0)), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)


class TestNoValueEscapesAsNaN(unittest.TestCase):
    """The invariant behind spec §51, asserted across every metric at once."""

    def _assert_clean(self, value, label):
        if value is None:
            return
        self.assertTrue(math.isfinite(value), f"{label} was not finite: {value}")

    def test_metrics_over_a_pathological_book_are_all_finite_or_none(self):
        snapshot = make_snapshot([
            make_valuation("a", 1e9, 1e-9),
            make_valuation("b", -1e9, 1e-9),
            make_valuation("c", 0.0, 100.0),
        ], cash=1e-9)

        concentration = analytics.compute_concentration(snapshot)
        for label in ("hhi", "effective_positions", "largest_weight",
                      "top_5_weight", "top_10_weight", "invested_weight"):
            self._assert_clean(getattr(concentration, label), label)

        self._assert_clean(snapshot.leverage, "leverage")
        self._assert_clean(snapshot.net_leverage, "net_leverage")
        self._assert_clean(snapshot.weight_of("a"), "weight_of(a)")

    def test_zero_quantity_position_contributes_no_exposure(self):
        valuation = make_valuation("a", 0.0, 100.0)
        self.assertEqual(valuation.market_value, 0.0)
        self.assertEqual(valuation.exposure, 0.0)

    def test_drawdown_over_a_zero_equity_curve_is_finite_or_none(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        curve = [(base, 0.0), (base + timedelta(days=1), 0.0)]
        metrics = analytics.compute_drawdown(curve)
        self._assert_clean(metrics.max_drawdown, "max_drawdown")
        self._assert_clean(metrics.current_drawdown, "current_drawdown")

    def test_drawdown_recovering_from_zero_does_not_divide_by_zero(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        curve = [(base, 0.0), (base + timedelta(days=1), 100.0),
                 (base + timedelta(days=2), 50.0)]
        metrics = analytics.compute_drawdown(curve)
        self.assertAlmostEqual(metrics.max_drawdown, -0.5)


class TestConstraintBoundaries(unittest.TestCase):
    """Spec §52: every limit tested below, exactly at, and above."""

    def _leverage_decision(self, gross_multiple):
        """
        A levered book that isolates the GROSS limit.

        Spread over 8 positions across 4 sectors so neither the 20%
        position cap nor the 40% sector cap binds first — otherwise
        this would be testing those limits instead, which is exactly
        what an earlier version of this fixture did by accident.
        """
        equity = 1000.0
        per_position = gross_multiple * equity / 8.0
        sectors = {}
        valuations = []
        for index in range(8):
            instrument_id = f"i{index}"
            sectors[instrument_id] = f"sector{index % 4}"
            # Alternating long/short so NET nets to zero. Without this
            # an all-long book hits the 1.0x net cap before the 1.5x
            # gross cap, and the test would silently be about net.
            sign = 1.0 if index % 2 == 0 else -1.0
            valuations.append(make_valuation(
                instrument_id, sign * per_position / 100.0, 100.0))
        snapshot = make_snapshot(valuations, cash=equity)
        self.assertAlmostEqual(snapshot.equity, equity, places=6)
        self.assertAlmostEqual(snapshot.gross_exposure / equity,
                               gross_multiple, places=6)
        return RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors=sectors), None, AS_OF)

    def test_gross_exposure_just_below_the_cap_passes(self):
        decision = self._leverage_decision(1.49)
        self.assertNotEqual(decision.state, RiskDecisionState.REJECTED)

    def test_gross_exposure_exactly_at_the_cap_passes(self):
        decision = self._leverage_decision(1.50)
        self.assertNotEqual(decision.state, RiskDecisionState.REJECTED)

    def test_gross_exposure_just_above_the_cap_is_rejected(self):
        decision = self._leverage_decision(1.51)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)

    def test_an_existing_oversized_position_is_caught_without_any_proposal(self):
        """
        The engine judges the projected STATE. A position already above
        the cap must be flagged even when nothing is proposed for it —
        screening alone would never look at it.
        """
        snapshot = make_snapshot([make_valuation("a", 5.0, 100.0)], cash=1000.0)
        self.assertAlmostEqual(snapshot.weight_of("a"), 500.0 / 1500.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)
        self.assertEqual(decision.blocking_violations[0].constraint_id,
                         "max_position_weight")


if __name__ == "__main__":
    unittest.main()
