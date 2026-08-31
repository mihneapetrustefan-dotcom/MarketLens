"""
tests/backtest/test_repository.py
--------------------------------------
Persistence and reproducibility metadata (spec §54, §55, §76, §79, §83).

The thing worth defending here is that a stored run carries enough to
be reproduced and enough to be distrusted: the full configuration, the
version identity, and the metrics that could NOT be computed. A store
that kept only the numbers that worked would make every run look
complete.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.engine import BacktestEngine
from src.data_access.backtest_repository import BacktestRepository
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from tests.backtest.helpers import (
    END, START, make_config, make_connection, signals_across, standard_universe,
)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        standard_universe(self.conn)
        self.repository = BacktestRepository(self.conn)
        self.signals = signals_across(["i-aaa", "i-bbb"], count=4)
        config = make_config(benchmark_instrument_id="bench")
        self.engine = BacktestEngine(self.conn, config, signals=self.signals)
        self.result = self.engine.run()
        self.quality = self.engine.assess(self.result)
        self.run_id = self.repository.save_result(self.result, self.quality)

    def tearDown(self):
        self.conn.close()


class TestRunRecord(RepositoryTestCase):
    def test_the_run_is_retrievable(self):
        run = self.repository.get_run(self.run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["run_id"], self.run_id)

    def test_the_full_configuration_is_stored(self):
        run = self.repository.get_run(self.run_id)
        config = run["configuration"]
        self.assertEqual(config["name"], "test")
        self.assertIn("execution", config)
        self.assertIn("costs", config)
        self.assertIn("slippage", config)

    def test_every_version_is_stored(self):
        identity = self.repository.get_run(self.run_id)["identity"]
        for key in ("risk_engine_version", "constraint_set_version",
                    "execution_model_version", "cost_model_version",
                    "slippage_model_version", "calendar_version", "code_version"):
            self.assertIn(key, identity)
            self.assertIsNotNone(identity[key])

    def test_the_quality_assessment_is_stored(self):
        quality = self.repository.get_run(self.run_id)["quality"]
        self.assertIn("score", quality)
        self.assertIn("factors", quality)

    def test_listing_finds_the_run(self):
        runs = self.repository.list_runs()
        self.assertIn(self.run_id, [r["run_id"] for r in runs])

    def test_saving_twice_does_not_duplicate(self):
        self.repository.save_result(self.result, self.quality)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM backtest_runs WHERE run_id = ?",
            (self.run_id,)).fetchone()[0]
        self.assertEqual(count, 1)


class TestArtefacts(RepositoryTestCase):
    def test_orders_fills_and_trades_are_all_stored(self):
        for table, expected in (
            ("simulated_orders", len(self.result.orders)),
            ("simulated_fills", len(self.result.fills)),
            ("backtest_trades", len(self.result.trades)),
        ):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                (self.run_id,)).fetchone()[0]
            self.assertEqual(count, expected, table)

    def test_the_equity_curve_round_trips(self):
        stored = self.repository.equity_curve(self.run_id)
        self.assertEqual(len(stored), len(self.result.equity_curve))
        self.assertAlmostEqual(stored[0]["equity"],
                               self.result.equity_curve[0].equity)

    def test_the_equity_curve_is_returned_in_order(self):
        stored = self.repository.equity_curve(self.run_id)
        stamps = [row["timestamp"] for row in stored]
        self.assertEqual(stamps, sorted(stamps))

    def test_trades_are_retrievable(self):
        trades = self.repository.trades_for(self.run_id)
        self.assertEqual(len(trades), len(self.result.trades))
        if trades:
            for key in ("instrument_id", "net_pnl", "entry_at", "exit_at"):
                self.assertIn(key, trades[0])

    def test_warnings_are_stored(self):
        warnings = self.repository.warnings_for(self.run_id)
        self.assertEqual(len(warnings), len(self.result.warnings))

    def test_drawdown_episodes_are_stored(self):
        count = self.conn.execute(
            "SELECT COUNT(*) FROM backtest_drawdowns WHERE run_id = ?",
            (self.run_id,)).fetchone()[0]
        self.assertEqual(count, len(self.result.drawdowns))


class TestMetrics(RepositoryTestCase):
    def test_computed_metrics_are_stored(self):
        metrics = self.repository.metrics_for(self.run_id)
        self.assertIn("total_return", metrics["values"])
        self.assertIn("total_trades", metrics["values"])

    def test_unavailable_metrics_are_stored_as_explicit_rows(self):
        """
        A missing row would be indistinguishable from a metric nobody
        tried to compute. "We could not measure this" is information.

        Uses a run with no benchmark, which guarantees at least one
        genuinely unmeasurable metric — the richer fixture happens to
        compute everything, which would leave this asserting nothing.
        """
        config = make_config(benchmark_instrument_id=None)
        result = BacktestEngine(self.conn, config, signals=self.signals).run()
        run_id = self.repository.save_result(result)

        metrics = self.repository.metrics_for(run_id)
        self.assertIn("benchmark", metrics["unavailable"])
        for reason in metrics["unavailable"].values():
            self.assertTrue(reason)

    def test_an_unavailable_metric_is_distinguishable_from_a_zero(self):
        config = make_config(benchmark_instrument_id=None)
        result = BacktestEngine(self.conn, config, signals=self.signals).run()
        run_id = self.repository.save_result(result)

        metrics = self.repository.metrics_for(run_id)
        self.assertIsNone(metrics["values"].get("benchmark_return"))
        self.assertIn("benchmark", metrics["unavailable"])

    def test_stored_metrics_match_the_result(self):
        metrics = self.repository.metrics_for(self.run_id)
        self.assertAlmostEqual(metrics["values"]["total_return"],
                               self.result.metrics.total_return)


class TestRiskEvents(RepositoryTestCase):
    def test_risk_events_table_is_queryable(self):
        events = self.repository.risk_events_for(self.run_id)
        self.assertEqual(
            len(events),
            len(self.result.rejected_allocations) + len(self.result.modified_allocations))

    def test_rejected_and_modified_are_distinguishable(self):
        for event in self.repository.risk_events_for(self.run_id):
            self.assertIn(event["kind"], ("rejected", "modified"))


class TestComparison(RepositoryTestCase):
    """Spec §83, §103 — runs must be comparable AND their differences visible."""

    def test_two_runs_can_be_compared(self):
        other_config = make_config(
            benchmark_instrument_id="bench",
            costs=CostModel(commission_bps=50.0),
            slippage=SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=50.0))
        other = BacktestEngine(self.conn, other_config, signals=self.signals).run()
        other_id = self.repository.save_result(other)

        comparison = self.repository.compare_runs([self.run_id, other_id])
        self.assertEqual(len(comparison), 2)
        self.assertNotEqual(comparison[0]["fingerprint"],
                            comparison[1]["fingerprint"])

    def test_comparison_exposes_the_differing_assumptions(self):
        other_config = make_config(
            benchmark_instrument_id="bench",
            costs=CostModel(version="cost-heavy", commission_bps=50.0))
        other = BacktestEngine(self.conn, other_config, signals=self.signals).run()
        other_id = self.repository.save_result(other)

        comparison = self.repository.compare_runs([self.run_id, other_id])
        versions = {row["cost_version"] for row in comparison}
        self.assertEqual(len(versions), 2)

    def test_an_unknown_run_is_skipped(self):
        self.assertEqual(self.repository.compare_runs(["nope"]), [])


class TestSchemaIsolation(RepositoryTestCase):
    def test_phase_12_does_not_write_to_the_live_portfolio_tables(self):
        """
        A backtest must never mutate the portfolio it is testing
        against — two runs in parallel would otherwise see each other.
        """
        for table in ("positions", "portfolios", "portfolio_state_snapshots"):
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)

    def test_no_risk_decisions_are_persisted_by_a_backtest(self):
        count = self.conn.execute(
            "SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
