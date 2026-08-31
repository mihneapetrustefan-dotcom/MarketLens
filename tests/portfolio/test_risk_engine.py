"""
tests/portfolio/test_risk_engine.py
----------------------------------------
Tests for the risk waterfall and the decisions it produces.

The behaviour under test is not "does it reject bad things" so much as
"can it ever approve something it did not actually verify". Most of
these cases are therefore about what happens when a check CANNOT run:
missing prices, stale prices, unmeasurable constraints, unclassified
sectors. In every one of them the engine must decline rather than pass.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import (
    AllocationChange, AllocationProposal, ConstraintScope, ConstraintSeverity,
    ConstraintSet, ExposureDimension, RiskConstraint, RiskDecisionState,
    RiskMetrics, TradingState, ValuationStatus, VolatilityEstimate,
)
from src.portfolio.constraints import default_constraint_set
from src.portfolio.risk_engine import EvaluationInputs, RiskEngine
from tests.portfolio.helpers import (
    AS_OF, make_signal, make_snapshot, make_valuation,
)


def build_inputs(snapshot, constraint_set=None, sectors=None, asset_classes=None,
                 metrics=None, signals=(), liquidity=None):
    return EvaluationInputs(
        snapshot=snapshot,
        constraint_set=constraint_set or default_constraint_set(),
        sector_by_instrument=sectors or {},
        asset_class_by_instrument=asset_classes or {},
        metrics=metrics if metrics is not None else RiskMetrics(as_of=AS_OF),
        signals_by_id={s.signal_id: s for s in signals},
        liquidity_participation=liquidity or {},
    )


def proposal_with(*changes, portfolio_id="pf"):
    return AllocationProposal(
        proposal_id="prop-1", portfolio_id=portfolio_id, as_of=AS_OF,
        changes=list(changes), sizing_strategy_id="test", sizing_version="v1")


class TestDataSufficiency(unittest.TestCase):
    """Spec §40, §56: approval must be earned; missing data never means safe."""

    def test_unpriced_position_yields_insufficient_data(self):
        snapshot = make_snapshot(
            [make_valuation("a", 10.0, 10.0)], cash=900.0,
            unvalued=[make_valuation("b", 5.0, None,
                                     status=ValuationStatus.MISSING_PRICE)])
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)
        self.assertFalse(decision.is_approved)

    def test_stale_price_yields_insufficient_data(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0, status=ValuationStatus.STALE_PRICE,
                           age_days=45.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)

    def test_multi_currency_without_fx_yields_insufficient_data(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0, currency="USD"),
            make_valuation("b", 10.0, 10.0, currency="EUR"),
        ], cash=800.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)
        self.assertIn("currenc", decision.summary.lower())

    def test_zero_equity_yields_insufficient_data(self):
        snapshot = make_snapshot([
            make_valuation("a", 10.0, 10.0),
            make_valuation("b", -10.0, 10.0),
        ], cash=0.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)

    def test_empty_portfolio_is_measurable_and_approved(self):
        """Empty is a known state, not an unknown one."""
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(make_snapshot([], cash=10_000.0)), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)

    def test_insufficient_data_records_why(self):
        snapshot = make_snapshot(
            [], cash=900.0,
            unvalued=[make_valuation("b", 5.0, None,
                                     status=ValuationStatus.MISSING_PRICE)])
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot), None, AS_OF)
        self.assertTrue(decision.reasons)
        self.assertTrue(decision.skipped_scopes)


class TestTradingState(unittest.TestCase):
    """Spec §38, §39: the kill switch, disconnected from any broker."""

    def _decide(self, state, changes):
        constraint_set = default_constraint_set()
        constraint_set.trading_state = state
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        return RiskEngine(constraint_set).evaluate(
            build_inputs(snapshot, constraint_set, sectors={"a": "technology"}),
            proposal_with(*changes), AS_OF)

    def test_emergency_stop_rejects_any_change(self):
        decision = self._decide(TradingState.EMERGENCY_STOP,
                                [AllocationChange("a", 0.10, 0.15)])
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)

    def test_paused_rejects_any_change(self):
        decision = self._decide(TradingState.PAUSED,
                                [AllocationChange("a", 0.10, 0.15)])
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)

    def test_reduce_only_rejects_an_increase(self):
        decision = self._decide(TradingState.REDUCE_ONLY,
                                [AllocationChange("a", 0.10, 0.15)])
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)
        self.assertIn("reduce_only", decision.summary)

    def test_reduce_only_allows_a_reduction(self):
        decision = self._decide(TradingState.REDUCE_ONLY,
                                [AllocationChange("a", 0.15, 0.05)])
        self.assertNotEqual(decision.state, RiskDecisionState.REJECTED)

    def test_emergency_stop_with_no_changes_is_not_a_rejection(self):
        decision = self._decide(TradingState.EMERGENCY_STOP, [])
        self.assertNotEqual(decision.state, RiskDecisionState.REJECTED)


class TestPositionLimits(unittest.TestCase):
    """Spec §52: below, exactly at, and above each limit."""

    def _decide(self, target_weight):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("a", confidence=0.8)
        return RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, signals=[signal]),
            proposal_with(AllocationChange(
                "a", 0.10, target_weight, signal_id=signal.signal_id)),
            AS_OF)

    def test_below_the_position_cap_is_approved(self):
        self.assertEqual(self._decide(0.15).state, RiskDecisionState.APPROVED)

    def test_exactly_at_the_position_cap_is_approved(self):
        self.assertEqual(self._decide(0.20).state, RiskDecisionState.APPROVED)

    def test_above_the_position_cap_is_trimmed_to_reduced(self):
        decision = self._decide(0.35)
        self.assertEqual(decision.state, RiskDecisionState.REDUCED)
        self.assertEqual(len(decision.approved_changes), 1)
        self.assertAlmostEqual(decision.approved_changes[0].target_weight, 0.20)

    def test_trimming_scales_the_quantity_proportionally(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("a", confidence=0.8)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, signals=[signal]),
            proposal_with(AllocationChange(
                "a", 0.10, 0.40, target_quantity=100.0, signal_id=signal.signal_id)),
            AS_OF)
        # 0.40 -> 0.20 halves the target, so the quantity halves too.
        self.assertAlmostEqual(decision.approved_changes[0].target_quantity, 50.0)

    def test_a_short_beyond_the_cap_is_trimmed_keeping_its_sign(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("a", confidence=0.8)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, signals=[signal]),
            proposal_with(AllocationChange(
                "a", 0.0, -0.35, signal_id=signal.signal_id)),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REDUCED)
        self.assertAlmostEqual(decision.approved_changes[0].target_weight, -0.20)


class TestSectorLimits(unittest.TestCase):
    def _decide(self, target_weight, sector="technology"):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("b", confidence=0.8)
        return RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": sector, "b": sector},
                         signals=[signal]),
            proposal_with(AllocationChange(
                "b", 0.0, target_weight, signal_id=signal.signal_id)),
            AS_OF)

    def test_projected_sector_below_the_cap_is_approved(self):
        # existing a=10%, proposed b=15% -> 25% technology
        self.assertEqual(self._decide(0.15).state, RiskDecisionState.APPROVED)

    def _crowded_sector_snapshot(self):
        """
        Two existing holdings in one sector, each comfortably under the
        20% position cap. Sized this way on purpose: a single oversized
        position would trip the position limit first, and the test
        would no longer be about sector aggregation at all.
        """
        snapshot = make_snapshot([
            make_valuation("a", 15.0, 10.0),      # 150
            make_valuation("b", 15.0, 10.0),      # 150
        ], cash=600.0)                             # equity = 900
        self.assertAlmostEqual(snapshot.weight_of("a"), 150.0 / 900.0)
        return snapshot

    def test_projected_sector_above_the_cap_is_rejected(self):
        # a and b are ~16.7% each in technology; adding c at 20% takes
        # the sector to ~53%, while no single position exceeds 20%.
        snapshot = self._crowded_sector_snapshot()
        signal = make_signal("c", confidence=0.8)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "technology",
                                            "c": "technology"},
                         signals=[signal]),
            proposal_with(AllocationChange("c", 0.0, 0.20, signal_id=signal.signal_id)),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)
        self.assertTrue(decision.blocking_violations)

    def test_the_rejection_explains_itself_with_numbers(self):
        """Spec §22: the arithmetic must be checkable, not asserted."""
        snapshot = self._crowded_sector_snapshot()
        signal = make_signal("c", confidence=0.8)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "technology",
                                            "c": "technology"},
                         signals=[signal]),
            proposal_with(AllocationChange("c", 0.0, 0.20, signal_id=signal.signal_id)),
            AS_OF)
        violation = next(v for v in decision.blocking_violations
                         if v.scope == ConstraintScope.SECTOR_WEIGHT)
        self.assertEqual(violation.applies_to, "technology")
        self.assertIsNotNone(violation.observed_value)
        self.assertEqual(violation.limit_value, 0.40)
        self.assertIn("exceeds maximum", violation.message)

    def test_different_sectors_do_not_aggregate(self):
        snapshot = self._crowded_sector_snapshot()
        signal = make_signal("c", confidence=0.8)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "technology",
                                            "c": "energy"},
                         signals=[signal]),
            proposal_with(AllocationChange("c", 0.0, 0.20, signal_id=signal.signal_id)),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)

    def test_unclassified_sector_exposure_blocks_approval(self):
        """A sector cap checked over a partial map is not a real cap."""
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": None}), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.INSUFFICIENT_DATA)
        self.assertIn("sector_weight[unclassified]", decision.skipped_scopes)


class TestExposureLimits(unittest.TestCase):
    def test_gross_exposure_above_the_cap_is_rejected(self):
        snapshot = make_snapshot([
            make_valuation("a", 100.0, 10.0),      # 1000
            make_valuation("b", -100.0, 10.0),     # 1000 short
        ], cash=1200.0)
        # equity = 1200 + (1000 - 1000) = 1200; gross = 2000 -> 1.67x
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy"}),
            None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)
        scopes = {v.scope for v in decision.hard_violations}
        self.assertIn(ConstraintScope.GROSS_EXPOSURE, scopes)

    def test_net_short_exposure_is_measured_in_absolute_terms(self):
        """A -1.4x net short is as exposed as +1.4x net long."""
        constraint_set = ConstraintSet(constraints=[
            RiskConstraint("net", ConstraintScope.NET_EXPOSURE,
                           ConstraintSeverity.HARD, max_value=1.0)])
        snapshot = make_snapshot([make_valuation("a", -140.0, 10.0)], cash=2400.0)
        # equity = 2400 - 1400 = 1000; net = -1400 -> -1.4x
        decision = RiskEngine(constraint_set).evaluate(
            build_inputs(snapshot, constraint_set, sectors={"a": "technology"}),
            None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)

    def test_modest_book_passes_all_exposure_limits(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}), None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)


class TestSignalEligibility(unittest.TestCase):
    def test_low_confidence_signal_is_dropped_not_used_to_reject_everything(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        weak = make_signal("b", confidence=0.10, signal_id="sig-weak")
        strong = make_signal("c", confidence=0.90, signal_id="sig-strong")
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy",
                                            "c": "energy"},
                         signals=[weak, strong]),
            proposal_with(
                AllocationChange("b", 0.0, 0.05, signal_id="sig-weak"),
                AllocationChange("c", 0.0, 0.05, signal_id="sig-strong")),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)
        approved = {c.instrument_id for c in decision.approved_changes}
        self.assertEqual(approved, {"c"})
        self.assertTrue(any("dropped" in r for r in decision.reasons))

    def test_signal_with_unknown_confidence_is_dropped(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        unknown = make_signal("b", confidence=None, signal_id="sig-unknown")
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy"},
                         signals=[unknown]),
            proposal_with(AllocationChange("b", 0.0, 0.05, signal_id="sig-unknown")),
            AS_OF)
        self.assertEqual(decision.approved_changes, [])

    def test_confidence_exactly_at_the_floor_is_eligible(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        floor = default_constraint_set().first(
            ConstraintScope.MIN_SIGNAL_CONFIDENCE).min_value
        signal = make_signal("b", confidence=floor, signal_id="sig-at")
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy"},
                         signals=[signal]),
            proposal_with(AllocationChange("b", 0.0, 0.05, signal_id="sig-at")),
            AS_OF)
        self.assertEqual(len(decision.approved_changes), 1)


class TestSoftConstraints(unittest.TestCase):
    def test_soft_breach_requires_review_rather_than_rejecting(self):
        metrics = RiskMetrics(as_of=AS_OF)
        metrics.volatility = VolatilityEstimate(value=0.95, observations=200,
                                                insufficient_data=False)
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, metrics=metrics),
            None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REQUIRES_REVIEW)
        self.assertTrue(decision.soft_violations)
        self.assertFalse(decision.hard_violations)
        self.assertFalse(decision.is_approved)

    def test_a_hard_breach_outranks_a_soft_one(self):
        metrics = RiskMetrics(as_of=AS_OF)
        metrics.volatility = VolatilityEstimate(value=0.95, observations=200)
        snapshot = make_snapshot([make_valuation("a", 300.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, metrics=metrics),
            None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)

    def test_unmeasurable_soft_constraint_is_skipped_without_blocking(self):
        metrics = RiskMetrics(as_of=AS_OF)
        metrics.volatility = VolatilityEstimate(insufficient_data=True,
                                                note="only 3 observations")
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}, metrics=metrics),
            None, AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)
        self.assertIn("portfolio_volatility", decision.skipped_scopes)


class TestDecisionRecord(unittest.TestCase):
    def test_evaluated_scopes_are_recorded(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}), None, AS_OF)
        self.assertIn("gross_exposure", decision.evaluated_scopes)
        self.assertIn("sector_weight[technology]", decision.evaluated_scopes)

    def test_provenance_records_every_version(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}),
            proposal_with(), AS_OF)
        self.assertEqual(decision.provenance.risk_engine_version, "v1")
        self.assertEqual(decision.provenance.constraint_set_version, "v1")
        self.assertEqual(decision.provenance.sizing_version, "v1")
        self.assertEqual(decision.provenance.portfolio_snapshot_as_of, AS_OF)

    def test_decision_id_is_deterministic_for_the_same_inputs(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        engine = RiskEngine(default_constraint_set())
        inputs = build_inputs(snapshot, sectors={"a": "technology"})
        first = engine.evaluate(inputs, proposal_with(), AS_OF)
        second = engine.evaluate(inputs, proposal_with(), AS_OF)
        self.assertEqual(first.decision_id, second.decision_id)

    def test_a_different_constraint_version_yields_a_different_decision_id(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        inputs = build_inputs(snapshot, sectors={"a": "technology"})
        first = RiskEngine(default_constraint_set()).evaluate(inputs, None, AS_OF)

        other = default_constraint_set()
        other.version = "v9"
        second = RiskEngine(other).evaluate(
            build_inputs(snapshot, other, sectors={"a": "technology"}), None, AS_OF)
        self.assertNotEqual(first.decision_id, second.decision_id)

    def test_every_non_approval_carries_a_reason(self):
        snapshot = make_snapshot([make_valuation("a", 300.0, 10.0)], cash=900.0)
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology"}), None, AS_OF)
        self.assertNotEqual(decision.state, RiskDecisionState.APPROVED)
        self.assertTrue(decision.reasons)
        self.assertTrue(decision.summary)


class TestMultiSignalPortfolio(unittest.TestCase):
    """Spec §24: aggregate risk, not each signal judged in isolation."""

    def test_individually_acceptable_signals_can_breach_together(self):
        snapshot = make_snapshot([], cash=1000.0)
        signals = [make_signal(f"i{n}", confidence=0.8, signal_id=f"sig-{n}")
                   for n in range(4)]
        # Four 15% technology positions: each fine alone, 60% together.
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot,
                         sectors={f"i{n}": "technology" for n in range(4)},
                         signals=signals),
            proposal_with(*[
                AllocationChange(f"i{n}", 0.0, 0.15, signal_id=f"sig-{n}")
                for n in range(4)]),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REJECTED)
        self.assertEqual(decision.hard_violations[0].scope,
                         ConstraintScope.SECTOR_WEIGHT)

    def test_the_same_signals_spread_across_sectors_are_approved(self):
        snapshot = make_snapshot([], cash=1000.0)
        signals = [make_signal(f"i{n}", confidence=0.8, signal_id=f"sig-{n}")
                   for n in range(4)]
        sectors = {"i0": "technology", "i1": "energy",
                   "i2": "healthcare", "i3": "utilities"}
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors=sectors, signals=signals),
            proposal_with(*[
                AllocationChange(f"i{n}", 0.0, 0.15, signal_id=f"sig-{n}")
                for n in range(4)]),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)


class TestLiquidity(unittest.TestCase):
    def test_illiquid_increase_is_flagged_for_review(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("b", confidence=0.8, signal_id="sig-b")
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy"},
                         signals=[signal], liquidity={"b": 0.55}),
            proposal_with(AllocationChange("b", 0.0, 0.05, signal_id="sig-b")),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.REQUIRES_REVIEW)
        self.assertEqual(decision.soft_violations[0].scope, ConstraintScope.MIN_LIQUIDITY)

    def test_liquid_increase_passes(self):
        snapshot = make_snapshot([make_valuation("a", 10.0, 10.0)], cash=900.0)
        signal = make_signal("b", confidence=0.8, signal_id="sig-b")
        decision = RiskEngine(default_constraint_set()).evaluate(
            build_inputs(snapshot, sectors={"a": "technology", "b": "energy"},
                         signals=[signal], liquidity={"b": 0.01}),
            proposal_with(AllocationChange("b", 0.0, 0.05, signal_id="sig-b")),
            AS_OF)
        self.assertEqual(decision.state, RiskDecisionState.APPROVED)


if __name__ == "__main__":
    unittest.main()
