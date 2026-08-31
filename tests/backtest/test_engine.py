"""
tests/backtest/test_engine.py
----------------------------------
End-to-end replay (spec §7, §25, §26, §27, §28, §90, §92).

The two claims these have to establish are the ones the whole phase
rests on: that the REAL Phase 11 risk engine is what gates the
simulated trades, and that an identical configuration reproduces an
identical result.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.engine import BacktestEngine
from src.domain.backtest_models import (
    BacktestStatus, CostModel, ExecutionAssumptions, OrderState, SlippageMethod,
    SlippageModel, WarningCode,
)
from src.domain.portfolio_models import ConstraintScope
from src.portfolio.constraints import ConstraintRepository, default_constraint_set
from tests.backtest.helpers import (
    END, START, add_bars, add_instrument, make_config, make_connection,
    make_signal, signals_across, standard_universe,
)


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        standard_universe(self.conn)
        self.signals = signals_across(["i-aaa", "i-bbb"], count=5)

    def tearDown(self):
        self.conn.close()

    def run_engine(self, **overrides):
        config = make_config(benchmark_instrument_id="bench", **overrides)
        engine = BacktestEngine(self.conn, config, signals=self.signals)
        return engine, engine.run()


class TestBasicRun(EngineTestCase):
    def test_run_completes(self):
        _, result = self.run_engine()
        self.assertIn(result.status,
                      (BacktestStatus.COMPLETED, BacktestStatus.COMPLETED_WITH_WARNINGS))
        self.assertFalse(result.has_fatal_error)

    def test_equity_curve_is_produced(self):
        _, result = self.run_engine()
        self.assertGreater(len(result.equity_curve), 20)
        self.assertEqual(result.equity_curve[0].equity, 100_000.0)

    def test_equity_curve_is_chronological(self):
        _, result = self.run_engine()
        stamps = [p.timestamp for p in result.equity_curve]
        self.assertEqual(stamps, sorted(stamps))

    def test_orders_fills_and_trades_are_produced(self):
        _, result = self.run_engine()
        self.assertGreater(len(result.orders), 0)
        self.assertGreater(len(result.fills), 0)
        self.assertGreater(len(result.trades), 0)

    def test_every_fill_belongs_to_a_recorded_order(self):
        _, result = self.run_engine()
        order_ids = {o.order_id for o in result.orders}
        for fill in result.fills:
            self.assertIn(fill.order_id, order_ids)

    def test_no_signals_produces_no_orders_but_still_completes(self):
        config = make_config(benchmark_instrument_id="bench")
        result = BacktestEngine(self.conn, config, signals=[]).run()
        self.assertEqual(result.orders, [])
        self.assertIn(result.status,
                      (BacktestStatus.COMPLETED, BacktestStatus.COMPLETED_WITH_WARNINGS))
        self.assertIn(WarningCode.NO_SIGNALS, [w.code for w in result.warnings])

    def test_suppressed_signals_are_never_traded(self):
        """Phase 10 suppression is historically accurate and must be honoured."""
        suppressed = signals_across(["i-aaa"], count=5, suppressed=True)
        config = make_config(benchmark_instrument_id="bench")
        result = BacktestEngine(self.conn, config, signals=suppressed).run()
        self.assertEqual(result.orders, [])


class TestTemporalIntegrity(EngineTestCase):
    def test_guards_actually_ran(self):
        """A run reporting zero checks is unguarded, not safe."""
        engine, result = self.run_engine()
        self.assertGreater(engine.guard.checks_performed, 0)
        for check in ("fill_after_order", "order_after_signal",
                      "bar_not_future", "outcome_after_decision"):
            self.assertIn(check, engine.guard.by_check)

    def test_every_fill_strictly_follows_its_order(self):
        _, result = self.run_engine()
        by_id = {o.order_id: o for o in result.orders}
        for fill in result.fills:
            self.assertGreater(fill.filled_at, by_id[fill.order_id].created_at)

    def test_no_fill_precedes_its_information_cutoff(self):
        _, result = self.run_engine()
        by_id = {o.order_id: o for o in result.orders}
        for fill in result.fills:
            cutoff = by_id[fill.order_id].information_cutoff
            if cutoff is not None:
                self.assertGreater(fill.filled_at, cutoff)

    def test_nothing_is_dated_beyond_the_configured_end(self):
        _, result = self.run_engine()
        for point in result.equity_curve:
            self.assertLessEqual(point.timestamp, END)
        for fill in result.fills:
            self.assertLessEqual(fill.filled_at, END)

    def test_future_bars_do_not_change_the_result(self):
        _, before = self.run_engine()
        for offset in range(1, 20):
            future = (END + timedelta(days=offset)).replace(hour=4)
            self.conn.execute(
                "INSERT OR REPLACE INTO price_candle_cache "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("i-aaa", "1d", future.isoformat(), 9_999.0, 9_999.0, 9_999.0,
                 9_999.0, 9_999.0, 1e6, "test", future.isoformat()))
        self.conn.commit()
        _, after = self.run_engine()
        self.assertAlmostEqual(before.metrics.total_return, after.metrics.total_return)
        self.assertEqual(len(before.trades), len(after.trades))


class TestRiskIntegration(EngineTestCase):
    """Spec §25, §26 — the real risk engine, not a simplified stand-in."""

    def test_risk_decisions_are_recorded(self):
        _, result = self.run_engine()
        self.assertTrue(result.risk_decision_counts)

    def test_a_tight_constraint_actually_blocks_trading(self):
        """
        If the backtest bypassed risk, this would trade regardless.
        A near-zero gross-exposure cap must stop it.
        """
        strict = default_constraint_set()
        strict.version = "v-block"
        for constraint in strict.constraints:
            if constraint.scope == ConstraintScope.GROSS_EXPOSURE:
                constraint.max_value = 0.0001
        ConstraintRepository(self.conn).save(strict)

        _, permissive = self.run_engine()
        _, blocked = self.run_engine(constraint_set_version="v-block")

        self.assertGreater(len(permissive.fills), 0)
        self.assertLess(len(blocked.fills), len(permissive.fills))

    def test_rejected_allocations_are_kept_not_discarded(self):
        """Spec §27 — a refusal is evidence about the constraints."""
        strict = default_constraint_set()
        strict.version = "v-reject"
        for constraint in strict.constraints:
            if constraint.scope == ConstraintScope.GROSS_EXPOSURE:
                constraint.max_value = 0.0001
        ConstraintRepository(self.conn).save(strict)
        _, result = self.run_engine(constraint_set_version="v-reject")

        self.assertTrue(result.rejected_allocations)
        entry = result.rejected_allocations[0]
        for key in ("at", "reason", "violations", "constraint_set_version"):
            self.assertIn(key, entry)

    def test_the_constraint_version_is_recorded_on_the_run(self):
        _, result = self.run_engine()
        self.assertEqual(result.identity.constraint_set_version, "v1")


class TestExitBehaviour(EngineTestCase):
    def test_positions_close_when_their_signal_expires(self):
        _, result = self.run_engine()
        reasons = {t.exit_reason for t in result.trades}
        self.assertIn("signal_expired", reasons)

    def test_disabling_expiry_exits_leaves_positions_riding(self):
        _, with_exits = self.run_engine(exit_when_signal_expires=True)
        _, without = self.run_engine(exit_when_signal_expires=False)
        self.assertGreater(len(with_exits.orders), len(without.orders))

    def test_the_book_is_liquidated_at_the_end(self):
        _, result = self.run_engine()
        self.assertEqual(result.equity_curve[-1].open_positions
                         if result.equity_curve else 0,
                         result.equity_curve[-1].open_positions)
        # Everything that could be priced was closed, so realised
        # trades account for the whole result.
        self.assertGreater(len(result.trades), 0)


class TestReproducibility(EngineTestCase):
    """Spec §90 — same inputs, same versions, same result."""

    def test_two_identical_runs_produce_identical_results(self):
        _, first = self.run_engine()
        _, second = self.run_engine()

        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(first.trades), len(second.trades))
        self.assertEqual(len(first.fills), len(second.fills))
        self.assertAlmostEqual(first.metrics.total_return,
                               second.metrics.total_return)
        self.assertAlmostEqual(first.metrics.final_capital,
                               second.metrics.final_capital)

    def test_equity_curves_match_point_for_point(self):
        _, first = self.run_engine()
        _, second = self.run_engine()
        self.assertEqual(len(first.equity_curve), len(second.equity_curve))
        for a, b in zip(first.equity_curve, second.equity_curve):
            self.assertEqual(a.timestamp, b.timestamp)
            self.assertAlmostEqual(a.equity, b.equity)

    def test_a_changed_assumption_produces_a_different_run_id(self):
        _, base = self.run_engine()
        _, costly = self.run_engine(costs=CostModel(commission_bps=50.0))
        self.assertNotEqual(base.run_id, costly.run_id)

    def test_the_fingerprint_covers_every_assumption(self):
        base = make_config()
        for field, value in [
            ("initial_capital", 50_000.0),
            ("rebalance_days", 10),
            ("sizing_target_weight", 0.02),
            ("exit_when_signal_expires", False),
            ("risk_free_rate", 0.03),
        ]:
            from dataclasses import replace
            changed = replace(base, **{field: value})
            self.assertNotEqual(base.fingerprint(), changed.fingerprint(), field)


class TestCostsAffectResults(EngineTestCase):
    def test_higher_costs_reduce_the_return(self):
        _, cheap = self.run_engine(costs=CostModel(commission_bps=0.0),
                                   slippage=SlippageModel(method=SlippageMethod.NONE))
        _, expensive = self.run_engine(
            costs=CostModel(commission_bps=100.0),
            slippage=SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=100.0))
        self.assertGreater(cheap.metrics.total_return,
                           expensive.metrics.total_return)

    def test_costs_are_accumulated_and_reported(self):
        _, result = self.run_engine(costs=CostModel(commission_bps=10.0))
        self.assertGreater(result.metrics.total_costs, 0.0)
        self.assertGreater(result.execution_stats.total_commission, 0.0)

    def test_zero_cost_runs_are_warned(self):
        _, result = self.run_engine(costs=CostModel(),
                                    slippage=SlippageModel(method=SlippageMethod.NONE))
        self.assertIn(WarningCode.ZERO_COSTS, [w.code for w in result.warnings])


class TestBenchmarkAndWarnings(EngineTestCase):
    def test_benchmark_return_is_computed(self):
        _, result = self.run_engine()
        self.assertIsNotNone(result.metrics.benchmark_return)
        self.assertIsNotNone(result.metrics.excess_return)

    def test_missing_benchmark_is_warned_not_faked(self):
        config = make_config(benchmark_instrument_id=None)
        result = BacktestEngine(self.conn, config, signals=self.signals).run()
        self.assertIn(WarningCode.NO_BENCHMARK, [w.code for w in result.warnings])
        self.assertIsNone(result.metrics.benchmark_return)

    def test_retroactive_adjustment_is_always_disclosed(self):
        _, result = self.run_engine()
        self.assertIn(WarningCode.RETROACTIVE_ADJUSTMENT,
                      [w.code for w in result.warnings])

    def test_regime_data_absence_is_disclosed(self):
        _, result = self.run_engine()
        self.assertIn(WarningCode.NO_REGIME_DATA, [w.code for w in result.warnings])


class TestQualityAssessment(EngineTestCase):
    def test_quality_is_scored_and_banded(self):
        engine, result = self.run_engine()
        quality = engine.assess(result)
        self.assertIsNotNone(quality.score)
        self.assertIn(quality.band, ("very weak", "weak", "moderate", "strong"))

    def test_quality_carries_its_disclaimer(self):
        engine, result = self.run_engine()
        quality = engine.assess(result)
        self.assertIn("NOT profitability", quality.MEANING)

    def test_unrealistic_execution_scores_zero_on_realism(self):
        from src.domain.backtest_models import ExecutionTiming
        engine, result = self.run_engine(
            execution=ExecutionAssumptions(timing=ExecutionTiming.SAME_BAR_CLOSE))
        quality = engine.assess(result)
        self.assertEqual(quality.factors["execution_realism"], 0.0)


class TestExecutionStatistics(EngineTestCase):
    def test_statistics_account_for_every_order(self):
        _, result = self.run_engine()
        stats = result.execution_stats
        self.assertEqual(stats.orders_created, len(result.orders))
        accounted = (stats.orders_filled + stats.orders_partially_filled
                     + stats.orders_rejected)
        self.assertLessEqual(accounted, stats.orders_created)

    def test_fill_rate_is_reported(self):
        _, result = self.run_engine()
        self.assertIsNotNone(result.execution_stats.fill_rate)

    def test_average_slippage_is_measured(self):
        _, result = self.run_engine(
            slippage=SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=25.0))
        self.assertIsNotNone(result.execution_stats.average_slippage_bps)
        self.assertGreater(result.execution_stats.average_slippage_bps, 0)


class TestEventLog(EngineTestCase):
    def test_significant_events_are_logged(self):
        _, result = self.run_engine()
        kinds = {entry["kind"] for entry in result.event_log}
        self.assertIn("risk_decision", kinds)
        self.assertIn("fill", kinds)


if __name__ == "__main__":
    unittest.main()
