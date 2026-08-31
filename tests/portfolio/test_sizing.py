"""
tests/portfolio/test_sizing.py
-----------------------------------
Tests for the position-sizing strategies.

The rule these defend is spec §16: confidence GATES a signal, it never
scales the position. A test that pins this matters more than it looks —
multiplying by confidence is the obvious shortcut, it would pass every
other test in this suite, and it would quietly hand more capital to
whichever model is most overconfident.
"""

import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import ConstraintScope, RiskConstraint, ConstraintSet
from src.domain.signal_models import SignalDirection, SignalStatus, SuppressionReason
from src.portfolio.constraints import default_constraint_set
from src.portfolio.sizing import (
    DEFAULT_TARGET_WEIGHT, FixedFractionSizing, SizingContext, VolatilityTargetSizing,
)
from tests.portfolio.helpers import AS_OF, make_signal, make_snapshot, make_valuation


def context(signals, snapshot=None, prices=None, volatility=None,
            constraint_set=None):
    # `prices is None` rather than `or`: an explicitly EMPTY price map
    # is a meaningful fixture (nothing is priceable) and must not be
    # silently replaced by the default.
    if prices is None:
        prices = {s.instrument_id: 100.0 for s in signals}
    return SizingContext(
        as_of=AS_OF,
        snapshot=snapshot or make_snapshot([], cash=10_000.0),
        signals=signals,
        constraint_set=constraint_set or default_constraint_set(),
        volatility_by_instrument=volatility or {},
        price_by_instrument=prices,
    )


def loose_constraints(position_cap=1.0):
    """A set whose position cap is wide enough not to mask what a test measures."""
    constraint_set = default_constraint_set()
    for constraint in constraint_set.constraints:
        if constraint.scope == ConstraintScope.POSITION_WEIGHT:
            constraint.max_value = position_cap
    return constraint_set


class TestEligibility(unittest.TestCase):
    def setUp(self):
        self.sizing = FixedFractionSizing()

    def test_actionable_signal_above_the_floor_is_eligible(self):
        signal = make_signal("a", confidence=0.8)
        self.assertEqual(len(self.sizing.eligible_signals(context([signal]))), 1)

    def test_signal_below_the_confidence_floor_is_excluded(self):
        signal = make_signal("a", confidence=0.10)
        self.assertEqual(self.sizing.eligible_signals(context([signal])), [])

    def test_suppressed_signal_is_excluded(self):
        signal = make_signal("a", confidence=0.9)
        signal.suppress(SuppressionReason.LOW_CONFIDENCE)
        self.assertEqual(self.sizing.eligible_signals(context([signal])), [])

    def test_expired_signal_is_excluded(self):
        signal = make_signal("a", confidence=0.9,
                             valid_until=AS_OF - timedelta(days=1))
        self.assertEqual(self.sizing.eligible_signals(context([signal])), [])

    def test_neutral_signal_is_excluded(self):
        signal = make_signal("a", confidence=0.9, direction=SignalDirection.NEUTRAL)
        self.assertEqual(self.sizing.eligible_signals(context([signal])), [])

    def test_unpriceable_signal_is_excluded(self):
        """A weight that cannot become a quantity is not a usable proposal."""
        signal = make_signal("a", confidence=0.9)
        self.assertEqual(self.sizing.eligible_signals(context([signal], prices={})), [])

    def test_short_signal_is_eligible(self):
        signal = make_signal("a", confidence=0.9, direction=SignalDirection.SHORT)
        self.assertEqual(len(self.sizing.eligible_signals(context([signal]))), 1)


class TestFixedFractionSizing(unittest.TestCase):
    def test_every_eligible_signal_gets_the_same_target(self):
        signals = [make_signal("a", confidence=0.55, signal_id="s-a"),
                   make_signal("b", confidence=0.95, signal_id="s-b")]
        proposal = FixedFractionSizing(0.05).propose(context(signals))
        self.assertEqual(len(proposal.changes), 2)
        self.assertEqual({c.target_weight for c in proposal.changes}, {0.05})

    def test_confidence_does_not_scale_the_position(self):
        """Spec §16, pinned deliberately: confidence gates, it never sizes."""
        low = FixedFractionSizing(0.05).propose(
            context([make_signal("a", confidence=0.45)]))
        high = FixedFractionSizing(0.05).propose(
            context([make_signal("a", confidence=0.99)]))
        self.assertEqual(low.changes[0].target_weight,
                         high.changes[0].target_weight)

    def test_target_is_capped_by_the_position_constraint(self):
        proposal = FixedFractionSizing(0.90).propose(
            context([make_signal("a", confidence=0.9)]))
        self.assertEqual(proposal.changes[0].target_weight, 0.20)

    def test_quantity_follows_from_weight_equity_and_price(self):
        snapshot = make_snapshot([], cash=10_000.0)
        proposal = FixedFractionSizing(0.05).propose(
            context([make_signal("a", confidence=0.9)], snapshot=snapshot,
                    prices={"a": 50.0}))
        # 5% of 10,000 = 500; at 50/share = 10 shares.
        self.assertAlmostEqual(proposal.changes[0].target_quantity, 10.0)

    def test_short_signal_produces_a_negative_quantity(self):
        proposal = FixedFractionSizing(0.05).propose(
            context([make_signal("a", confidence=0.9,
                                 direction=SignalDirection.SHORT)],
                    prices={"a": 50.0}))
        self.assertLess(proposal.changes[0].target_quantity, 0)

    def test_no_eligible_signals_yields_an_empty_proposal(self):
        proposal = FixedFractionSizing().propose(
            context([make_signal("a", confidence=0.05)]))
        self.assertTrue(proposal.is_empty)

    def test_proposal_id_is_deterministic(self):
        signals = [make_signal("a", confidence=0.9)]
        first = FixedFractionSizing().propose(context(signals))
        second = FixedFractionSizing().propose(context(signals))
        self.assertEqual(first.proposal_id, second.proposal_id)

    def test_proposal_records_its_strategy_and_version(self):
        proposal = FixedFractionSizing().propose(
            context([make_signal("a", confidence=0.9)]))
        self.assertEqual(proposal.sizing_strategy_id, "fixed_fraction")
        self.assertEqual(proposal.sizing_version, "v1")

    def test_invalid_target_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            FixedFractionSizing(0.0)
        with self.assertRaises(ValueError):
            FixedFractionSizing(1.5)

    def test_current_weight_is_carried_from_the_snapshot(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 100.0)], cash=9_000.0)
        proposal = FixedFractionSizing(0.05).propose(
            context([make_signal("a", confidence=0.9)], snapshot=snapshot,
                    prices={"a": 100.0}))
        self.assertAlmostEqual(proposal.changes[0].current_weight, 0.10)


class TestVolatilityTargetSizing(unittest.TestCase):
    def test_lower_volatility_earns_a_larger_weight(self):
        signals = [make_signal("calm", confidence=0.9, signal_id="s-calm"),
                   make_signal("wild", confidence=0.9, signal_id="s-wild")]
        proposal = VolatilityTargetSizing(0.15, max_weight=1.0).propose(
            context(signals, volatility={"calm": 0.10, "wild": 0.60},
                    constraint_set=loose_constraints()))
        weights = {c.instrument_id: c.target_weight for c in proposal.changes}
        self.assertGreater(weights["calm"], weights["wild"])

    def test_weight_is_the_target_over_measured_volatility(self):
        proposal = VolatilityTargetSizing(0.15, max_weight=1.0).propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": 0.30},
                    constraint_set=loose_constraints()))
        self.assertAlmostEqual(proposal.changes[0].target_weight, 0.50)

    def test_the_position_cap_still_wins_over_a_volatility_target(self):
        """The strategy proposes; the configured limit still binds."""
        proposal = VolatilityTargetSizing(0.15, max_weight=1.0).propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": 0.30}))
        self.assertAlmostEqual(proposal.changes[0].target_weight, 0.20)

    def test_unmeasurable_volatility_skips_rather_than_assuming_a_default(self):
        """Spec §41: a size derived from an assumed number is a fabricated size."""
        proposal = VolatilityTargetSizing().propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": None}))
        self.assertTrue(proposal.is_empty)
        self.assertIn("volatility not measurable", proposal.note)

    def test_zero_volatility_is_skipped(self):
        proposal = VolatilityTargetSizing().propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": 0.0}))
        self.assertTrue(proposal.is_empty)

    def test_result_is_capped_by_the_position_constraint(self):
        proposal = VolatilityTargetSizing(0.15).propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": 0.01}))
        self.assertLessEqual(proposal.changes[0].target_weight, 0.20)

    def test_invalid_target_is_rejected(self):
        with self.assertRaises(ValueError):
            VolatilityTargetSizing(0.0)

    def test_records_its_own_strategy_id(self):
        proposal = VolatilityTargetSizing().propose(
            context([make_signal("a", confidence=0.9)], volatility={"a": 0.2}))
        self.assertEqual(proposal.sizing_strategy_id, "volatility_target")


class TestSizingWithoutConstraints(unittest.TestCase):
    def test_absent_constraint_set_disables_the_confidence_gate(self):
        """Without configured limits the strategy sizes what it is given."""
        signal = make_signal("a", confidence=0.01)
        sizing = FixedFractionSizing(0.05)
        proposal = sizing.propose(SizingContext(
            as_of=AS_OF, snapshot=make_snapshot([], cash=1000.0),
            signals=[signal], constraint_set=None,
            price_by_instrument={"a": 10.0}))
        self.assertEqual(len(proposal.changes), 1)

    def test_absent_constraint_set_leaves_the_target_uncapped(self):
        proposal = FixedFractionSizing(0.90).propose(SizingContext(
            as_of=AS_OF, snapshot=make_snapshot([], cash=1000.0),
            signals=[make_signal("a", confidence=0.9)], constraint_set=None,
            price_by_instrument={"a": 10.0}))
        self.assertEqual(proposal.changes[0].target_weight, 0.90)


if __name__ == "__main__":
    unittest.main()
