"""
tests/backtest/test_backtest_models.py
-------------------------------------------
The Phase 12 domain types (spec §5, §6, §17, §18, §54, §92, §100).

The configuration fingerprint gets the most attention here. It is what
makes "same inputs produce the same run" checkable, and a field left
out of it silently allows two different backtests to claim the same
identity — which would corrupt the research history rather than merely
inconvenience it.
"""

import math
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.backtest_models import (
    BacktestConfiguration, BacktestResult, BacktestStatus, CostModel,
    DrawdownEpisode, EquityPoint, ExecutionAssumptions, ExecutionTiming,
    OrderSide, OrderState, PerformanceMetrics, QualityAssessment, RejectReason,
    RunIdentity, SimulatedFill, SimulatedOrder, SlippageMethod, SlippageModel,
    Trade, WarningCode, finite_or_none, safe_ratio,
)
from tests.backtest.helpers import END, START, make_config

AT = datetime(2026, 6, 1, tzinfo=timezone.utc)


class TestNumericGuards(unittest.TestCase):
    def test_nan_and_infinity_become_none(self):
        self.assertIsNone(finite_or_none(float("nan")))
        self.assertIsNone(finite_or_none(float("inf")))
        self.assertIsNone(finite_or_none(float("-inf")))

    def test_zero_survives(self):
        self.assertEqual(finite_or_none(0.0), 0.0)

    def test_division_by_zero_is_none(self):
        self.assertIsNone(safe_ratio(1.0, 0.0))


class TestConfiguration(unittest.TestCase):
    def test_end_must_follow_start(self):
        with self.assertRaises(ValueError):
            BacktestConfiguration(name="x", start=END, end=START)

    def test_capital_must_be_positive(self):
        with self.assertRaises(ValueError):
            BacktestConfiguration(name="x", start=START, end=END,
                                  initial_capital=0.0)

    def test_rebalance_days_must_be_at_least_one(self):
        with self.assertRaises(ValueError):
            BacktestConfiguration(name="x", start=START, end=END,
                                  rebalance_days=0)

    def test_naive_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            BacktestConfiguration(name="x", start=datetime(2026, 1, 1), end=END)

    def test_identical_configurations_share_a_fingerprint(self):
        self.assertEqual(make_config().fingerprint(), make_config().fingerprint())

    def test_the_risk_free_source_is_recorded(self):
        config = make_config()
        self.assertTrue(config.risk_free_source)


class TestFingerprintCoverage(unittest.TestCase):
    """
    Every field that can change a result must change the fingerprint.

    A gap here lets two genuinely different backtests claim the same
    identity, which silently corrupts the research history.
    """

    def setUp(self):
        self.base = make_config()

    def _assert_changes(self, **overrides):
        changed = replace(self.base, **overrides)
        self.assertNotEqual(self.base.fingerprint(), changed.fingerprint(),
                            f"fingerprint ignored {list(overrides)}")

    def test_period_changes_it(self):
        self._assert_changes(end=END - timedelta(days=10))

    def test_capital_changes_it(self):
        self._assert_changes(initial_capital=50_000.0)

    def test_universe_changes_it(self):
        self._assert_changes(universe=["i-aaa"])

    def test_benchmark_changes_it(self):
        self._assert_changes(benchmark_instrument_id="bench")

    def test_cost_model_changes_it(self):
        self._assert_changes(costs=CostModel(commission_bps=99.0))

    def test_slippage_model_changes_it(self):
        self._assert_changes(slippage=SlippageModel(base_bps=99.0))

    def test_execution_timing_changes_it(self):
        self._assert_changes(execution=ExecutionAssumptions(
            timing=ExecutionTiming.NEXT_BAR_CLOSE))

    def test_latency_changes_it(self):
        self._assert_changes(execution=ExecutionAssumptions(
            signal_to_order_seconds=3600.0))

    def test_constraint_version_changes_it(self):
        self._assert_changes(constraint_set_version="v9")

    def test_sizing_changes_it(self):
        self._assert_changes(sizing_target_weight=0.02)

    def test_rebalance_cadence_changes_it(self):
        self._assert_changes(rebalance_days=30)

    def test_event_driven_flag_changes_it(self):
        self._assert_changes(event_driven=False)

    def test_exit_rule_changes_it(self):
        self._assert_changes(exit_when_signal_expires=False)

    def test_risk_free_rate_changes_it(self):
        self._assert_changes(risk_free_rate=0.05)

    def test_seed_changes_it(self):
        self._assert_changes(random_seed=99)


class TestOrders(unittest.TestCase):
    def test_quantity_must_be_positive(self):
        with self.assertRaises(ValueError):
            SimulatedOrder(order_id="o", run_id="r", instrument_id="i",
                           side=OrderSide.BUY, quantity=-5.0)

    def test_direction_lives_on_side_not_the_sign(self):
        order = SimulatedOrder(order_id="o", run_id="r", instrument_id="i",
                               side=OrderSide.SELL, quantity=5.0)
        order.filled_quantity = 5.0
        self.assertEqual(order.signed_filled, -5.0)

    def test_remaining_tracks_partial_fills(self):
        order = SimulatedOrder(order_id="o", run_id="r", instrument_id="i",
                               side=OrderSide.BUY, quantity=10.0)
        order.filled_quantity = 4.0
        self.assertAlmostEqual(order.remaining, 6.0)

    def test_order_states_are_a_separate_type_from_signal_states(self):
        """
        Spec §17 — merging the two lifecycles is a common, destructive
        shortcut.

        The requirement is distinct TYPES, not disjoint strings:
        "rejected" and "expired" are the natural words for states in
        both lifecycles, and an order being rejected has nothing to do
        with a signal being rejected. What must not happen is one being
        assignable to the other.
        """
        from src.domain.signal_models import SignalStatus
        self.assertIsNot(OrderState, SignalStatus)
        self.assertNotIsInstance(OrderState.FILLED, SignalStatus)
        self.assertNotIsInstance(SignalStatus.ACTIVE, OrderState)
        # Each carries states the other has no concept of.
        self.assertIn("partially_filled", {s.value for s in OrderState})
        self.assertIn("suppressed", {s.value for s in SignalStatus})


class TestFills(unittest.TestCase):
    def _fill(self, side=OrderSide.BUY, price=101.0, reference=100.0):
        return SimulatedFill(
            fill_id="f", run_id="r", order_id="o", instrument_id="i",
            side=side, quantity=10.0, price=price, reference_price=reference,
            filled_at=AT, commission=2.0, slippage_cost=10.0)

    def test_notional_uses_the_charged_price(self):
        self.assertAlmostEqual(self._fill().notional, 1010.0)

    def test_total_cost_sums_commission_and_slippage(self):
        self.assertAlmostEqual(self._fill().total_cost, 12.0)

    def test_a_sell_has_negative_signed_quantity(self):
        self.assertAlmostEqual(self._fill(side=OrderSide.SELL).signed_quantity, -10.0)

    def test_the_reference_price_is_kept_alongside_the_charged_one(self):
        fill = self._fill()
        self.assertNotAlmostEqual(fill.price, fill.reference_price)


class TestTrades(unittest.TestCase):
    def _trade(self, gross=100.0, costs=10.0, days=5):
        return Trade(
            trade_id="t", run_id="r", instrument_id="i", side=OrderSide.BUY,
            quantity=10.0, entry_price=100.0, exit_price=110.0,
            entry_at=AT, exit_at=AT + timedelta(days=days),
            gross_pnl=gross, costs=costs)

    def test_net_pnl_subtracts_costs(self):
        self.assertAlmostEqual(self._trade().net_pnl, 90.0)

    def test_a_trade_whose_costs_exceed_its_gain_is_a_loss(self):
        self.assertFalse(self._trade(gross=5.0, costs=10.0).is_win)

    def test_holding_period(self):
        self.assertAlmostEqual(self._trade(days=7).holding_days, 7.0)

    def test_return_is_relative_to_committed_capital(self):
        self.assertAlmostEqual(self._trade().return_pct, 90.0 / 1000.0)


class TestResultAccounting(unittest.TestCase):
    def setUp(self):
        self.result = BacktestResult(
            run_id="r", backtest_id="b", status=BacktestStatus.RUNNING,
            configuration=make_config(),
            identity=RunIdentity(backtest_id="b", run_id="r",
                                 config_fingerprint="fp"))

    def test_warnings_are_deduplicated(self):
        self.result.add_warning(WarningCode.SMALL_SAMPLE, "a")
        self.result.add_warning(WarningCode.SMALL_SAMPLE, "b")
        self.assertEqual(len(self.result.warnings), 1)

    def test_a_fatal_error_is_flagged(self):
        self.assertFalse(self.result.has_fatal_error)
        self.result.add_error("x", "boom", fatal=True)
        self.assertTrue(self.result.has_fatal_error)

    def test_the_event_log_records_entries(self):
        self.result.log(AT, "fill", instrument="i")
        self.assertEqual(self.result.event_log[0]["kind"], "fill")
        self.assertEqual(self.result.event_log[0]["instrument"], "i")


class TestDrawdownEpisode(unittest.TestCase):
    def test_recovery_is_none_while_underwater(self):
        episode = DrawdownEpisode(
            peak_at=AT, peak_equity=100.0, trough_at=AT + timedelta(days=5),
            trough_equity=80.0, depth=-0.2)
        self.assertFalse(episode.is_recovered)
        self.assertIsNone(episode.recovery_days)

    def test_duration_and_recovery_are_measured(self):
        episode = DrawdownEpisode(
            peak_at=AT, peak_equity=100.0, trough_at=AT + timedelta(days=5),
            trough_equity=80.0, depth=-0.2,
            recovered_at=AT + timedelta(days=12))
        self.assertAlmostEqual(episode.duration_days, 5.0)
        self.assertAlmostEqual(episode.recovery_days, 7.0)


class TestQualityAssessment(unittest.TestCase):
    def test_the_disclaimer_is_part_of_the_type(self):
        self.assertIn("NOT profitability", QualityAssessment().MEANING)

    def test_bands_map_from_the_score(self):
        self.assertEqual(QualityAssessment(score=None).band, "unrated")
        self.assertEqual(QualityAssessment(score=0.9).band, "strong")
        self.assertEqual(QualityAssessment(score=0.6).band, "moderate")
        self.assertEqual(QualityAssessment(score=0.3).band, "weak")
        self.assertEqual(QualityAssessment(score=0.1).band, "very weak")


class TestExecutionAssumptions(unittest.TestCase):
    def test_shorting_is_off_by_default(self):
        """No borrow-cost data exists, so it must be opted into."""
        self.assertFalse(ExecutionAssumptions().allow_shorting)

    def test_the_default_timing_is_realistic(self):
        self.assertEqual(ExecutionAssumptions().timing,
                         ExecutionTiming.NEXT_BAR_OPEN)

    def test_latency_is_non_zero_by_default(self):
        self.assertGreater(ExecutionAssumptions().signal_to_order_seconds, 0)

    def test_describe_states_the_assumptions(self):
        described = ExecutionAssumptions().describe()
        self.assertIn("next_bar_open", described)
        self.assertIn("participation", described)


if __name__ == "__main__":
    unittest.main()
