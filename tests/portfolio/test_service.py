"""
tests/portfolio/test_service.py
------------------------------------
End-to-end tests: state, measurement, proposal, decision, persistence.

The integration-level leakage tests here are the ones that matter most.
Unit tests can prove each query carries its anchor; only an end-to-end
replay can prove the ASSEMBLY does — that no accessor anywhere in the
chain quietly reaches past the anchor once the parts are wired
together.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.portfolio_repository import PortfolioRepository
from src.domain.portfolio_models import (
    PositionSource, RiskDecisionState, TradingState,
)
from src.portfolio.constraints import ConstraintRepository, default_constraint_set
from src.portfolio.service import PortfolioService
from src.portfolio.sizing import FixedFractionSizing
from tests.portfolio.helpers import (
    AS_OF, add_candles, add_instrument, make_connection, make_position, make_signal,
)


class ServiceTestCase(unittest.TestCase):
    """A funded two-position book with a year of daily candles."""

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_instrument(self.conn, "i-b", "BBB", "energy")
        add_candles(self.conn, "i-a", AS_OF, days=250, start_price=100.0, seed=1)
        add_candles(self.conn, "i-b", AS_OF, days=250, start_price=50.0, seed=2)

        self.repository = PortfolioRepository(self.conn)
        self.repository.save_portfolio("pf", "Book", cash=50_000.0)
        self.repository.save_position(make_position(
            "i-a", 100.0, entry=90.0, position_id="p-a",
            opened_at=AS_OF - timedelta(days=60)))
        self.repository.save_position(make_position(
            "i-b", 200.0, entry=45.0, position_id="p-b",
            opened_at=AS_OF - timedelta(days=40)))
        self.service = PortfolioService(self.conn)

    def tearDown(self):
        self.conn.close()


class TestSnapshot(ServiceTestCase):
    def test_snapshot_prices_every_position(self):
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertEqual(len(snapshot.valuations), 2)
        self.assertTrue(snapshot.is_complete)

    def test_equity_includes_cash(self):
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertGreater(snapshot.equity, 50_000.0)

    def test_unrealized_pnl_is_aggregated(self):
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertIsNotNone(snapshot.unrealized_pnl)

    def test_position_opened_after_the_anchor_is_invisible(self):
        """Spec §44: state must reflect the anchor, not today."""
        self.repository.save_position(make_position(
            "i-a", 999.0, position_id="p-future",
            opened_at=AS_OF + timedelta(days=1)))
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertEqual(len(snapshot.valuations), 2)

    def test_position_closed_after_the_anchor_is_still_open_at_it(self):
        self.repository.close_position("p-a", AS_OF + timedelta(days=5))
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertEqual(len(snapshot.valuations), 2)

    def test_position_closed_before_the_anchor_is_excluded(self):
        self.repository.close_position("p-a", AS_OF - timedelta(days=5))
        snapshot = self.service.build_snapshot("pf", AS_OF)
        self.assertEqual(len(snapshot.valuations), 1)

    def test_unknown_portfolio_yields_an_empty_snapshot(self):
        snapshot = self.service.build_snapshot("pf-nowhere", AS_OF)
        self.assertTrue(snapshot.is_empty)


class TestMetrics(ServiceTestCase):
    def test_volatility_is_measured_from_cached_history(self):
        snapshot = self.service.build_snapshot("pf", AS_OF)
        metrics = self.service.compute_metrics(snapshot, AS_OF)
        self.assertFalse(metrics.volatility.insufficient_data)
        self.assertGreater(metrics.volatility.value, 0)

    def test_value_at_risk_is_computed_when_history_allows(self):
        snapshot = self.service.build_snapshot("pf", AS_OF)
        metrics = self.service.compute_metrics(snapshot, AS_OF)
        self.assertFalse(metrics.value_at_risk.insufficient_data)
        self.assertIsNotNone(metrics.value_at_risk.expected_shortfall)

    def test_drawdown_is_unavailable_without_stored_snapshots(self):
        """Spec §12: never synthesized from a simulated equity curve."""
        snapshot = self.service.build_snapshot("pf", AS_OF)
        metrics = self.service.compute_metrics(snapshot, AS_OF)
        self.assertTrue(metrics.drawdown.insufficient_data)
        self.assertIn("drawdown", metrics.unavailable)

    def test_drawdown_appears_once_real_snapshots_exist(self):
        for index, (offset, equity) in enumerate(
                [(30, 100_000.0), (20, 120_000.0), (10, 90_000.0)]):
            snapshot = self.service.build_snapshot("pf", AS_OF - timedelta(days=offset))
            # Force a known equity level: equity is cash + position
            # value, so every exposure component has to be cleared, not
            # just gross.
            snapshot.cash = equity
            snapshot.valuations = []
            snapshot.gross_exposure = 0.0
            snapshot.long_exposure = 0.0
            snapshot.short_exposure = 0.0
            self.assertAlmostEqual(snapshot.equity, equity)
            self.repository.save_snapshot(f"s{index}", snapshot)

        snapshot = self.service.build_snapshot("pf", AS_OF)
        metrics = self.service.compute_metrics(snapshot, AS_OF)
        self.assertFalse(metrics.drawdown.insufficient_data)
        self.assertAlmostEqual(metrics.drawdown.max_drawdown, -0.25)

    def test_concentration_is_available_even_without_price_history(self):
        conn = make_connection()
        repository = PortfolioRepository(conn)
        repository.save_portfolio("pf2", "Book", cash=0.0)
        service = PortfolioService(conn)
        snapshot = service.build_snapshot("pf2", AS_OF)
        metrics = service.compute_metrics(snapshot, AS_OF)
        self.assertEqual(metrics.concentration.position_count, 0)
        conn.close()

    def test_unpriced_instrument_is_reported_in_unavailable(self):
        self.repository.save_position(make_position(
            "i-nowhere", 10.0, position_id="p-x",
            opened_at=AS_OF - timedelta(days=10)))
        snapshot = self.service.build_snapshot("pf", AS_OF)
        metrics = self.service.compute_metrics(snapshot, AS_OF)
        self.assertFalse(snapshot.is_complete)
        self.assertEqual(len(snapshot.unvalued_positions), 1)


class TestEvaluation(ServiceTestCase):
    def test_evaluating_the_current_book_returns_a_decision(self):
        result = self.service.evaluate("pf", AS_OF)
        self.assertIsNotNone(result.decision)
        self.assertIsNotNone(result.decision.summary)

    def test_no_signals_means_no_proposal_and_no_intents(self):
        result = self.service.evaluate("pf", AS_OF)
        self.assertIsNone(result.proposal)
        self.assertEqual(result.intents, [])

    def test_a_qualifying_signal_produces_a_proposal(self):
        signal = make_signal("i-b", confidence=0.85, signal_id="s-1")
        result = self.service.evaluate(
            "pf", AS_OF, sizing=FixedFractionSizing(0.05), signals=[signal])
        self.assertIsNotNone(result.proposal)
        self.assertEqual(len(result.proposal.changes), 1)

    def test_intents_are_built_only_from_an_approving_decision(self):
        signal = make_signal("i-b", confidence=0.85, signal_id="s-1")
        result = self.service.evaluate(
            "pf", AS_OF, sizing=FixedFractionSizing(0.05), signals=[signal])
        if result.decision.is_approved:
            self.assertTrue(all(not i.is_executable for i in result.intents))
        else:
            self.assertEqual(result.intents, [])

    def test_no_intent_is_ever_executable(self):
        signal = make_signal("i-b", confidence=0.85, signal_id="s-1")
        result = self.service.evaluate(
            "pf", AS_OF, sizing=FixedFractionSizing(0.05), signals=[signal])
        for intent in result.intents:
            self.assertFalse(intent.is_executable)

    def test_emergency_stop_blocks_every_increase(self):
        constraint_set = default_constraint_set()
        constraint_set.trading_state = TradingState.EMERGENCY_STOP
        constraint_set.version = "v-stop"
        ConstraintRepository(self.conn).save(constraint_set)

        service = PortfolioService(self.conn, constraint_version="v-stop")
        signal = make_signal("i-b", confidence=0.85, signal_id="s-1")
        result = service.evaluate(
            "pf", AS_OF, sizing=FixedFractionSizing(0.05), signals=[signal])
        self.assertEqual(result.decision.state, RiskDecisionState.REJECTED)
        self.assertEqual(result.intents, [])


class TestPointInTimeAtServiceLevel(ServiceTestCase):
    """The assembly, not just each query, must respect the anchor."""

    def _insert_future_candles(self):
        for instrument_id, price in (("i-a", 9_999.0), ("i-b", 8_888.0)):
            for offset in (1, 2, 3):
                future = (AS_OF + timedelta(days=offset)).replace(hour=4, minute=0)
                self.conn.execute(
                    "INSERT OR REPLACE INTO price_candle_cache "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (instrument_id, "1d", future.isoformat(), price, price, price,
                     price, price, 1_000_000.0, "test", AS_OF.isoformat()))
        self.conn.commit()

    def test_future_prices_do_not_change_the_snapshot(self):
        before = self.service.build_snapshot("pf", AS_OF)
        self._insert_future_candles()
        after = self.service.build_snapshot("pf", AS_OF)
        self.assertAlmostEqual(before.equity, after.equity)

    def test_future_prices_do_not_change_the_metrics(self):
        before = self.service.compute_metrics(
            self.service.build_snapshot("pf", AS_OF), AS_OF)
        self._insert_future_candles()
        after = self.service.compute_metrics(
            self.service.build_snapshot("pf", AS_OF), AS_OF)
        self.assertAlmostEqual(before.volatility.value, after.volatility.value)
        self.assertEqual(before.volatility.observations, after.volatility.observations)

    def test_future_prices_do_not_change_the_decision(self):
        before = self.service.evaluate("pf", AS_OF).decision
        self._insert_future_candles()
        after = self.service.evaluate("pf", AS_OF).decision
        self.assertEqual(before.state, after.state)
        self.assertEqual(before.decision_id, after.decision_id)

    def test_a_signal_whose_information_postdates_the_anchor_is_excluded(self):
        from src.data_access.signal_repository import SignalRepository
        repository = SignalRepository(self.conn)
        repository.save(make_signal(
            "i-a", confidence=0.9, signal_id="s-future",
            cutoff=AS_OF + timedelta(days=2)))
        usable = self.service.actionable_signals(AS_OF)
        self.assertNotIn("s-future", [s.signal_id for s in usable])

    def test_an_earlier_anchor_sees_fewer_observations(self):
        late = self.service.compute_metrics(
            self.service.build_snapshot("pf", AS_OF), AS_OF)
        # Both positions opened 40 and 60 days back, so they exist at a
        # 30-day-earlier anchor while less price history does.
        early_anchor = AS_OF - timedelta(days=30)
        early = self.service.compute_metrics(
            self.service.build_snapshot("pf", early_anchor), early_anchor)
        self.assertLess(early.volatility.observations, late.volatility.observations)

    def test_an_anchor_before_the_book_existed_shows_an_empty_portfolio(self):
        """
        Anchored before either position was opened, the portfolio is
        empty — not the current book priced at old prices. This is the
        distinction that makes a replay meaningful.
        """
        before_inception = AS_OF - timedelta(days=100)
        snapshot = self.service.build_snapshot("pf", before_inception)
        self.assertTrue(snapshot.is_empty)

        metrics = self.service.compute_metrics(snapshot, before_inception)
        self.assertIsNone(metrics.volatility.observations)
        self.assertIn("volatility", metrics.unavailable)


class TestHistoricalReplay(ServiceTestCase):
    """Spec §45: the same inputs and versions must reproduce the same verdict."""

    def test_repeating_an_evaluation_reproduces_the_decision(self):
        first = self.service.evaluate("pf", AS_OF).decision
        second = self.service.evaluate("pf", AS_OF).decision
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.summary, second.summary)

    def test_replay_through_a_fresh_service_reproduces_the_decision(self):
        first = self.service.evaluate("pf", AS_OF).decision
        replayed = PortfolioService(self.conn).evaluate("pf", AS_OF).decision
        self.assertEqual(first.decision_id, replayed.decision_id)
        self.assertEqual(first.state, replayed.state)

    def test_replay_is_unaffected_by_later_price_history(self):
        original = self.service.evaluate("pf", AS_OF).decision
        for offset in range(1, 30):
            future = (AS_OF + timedelta(days=offset)).replace(hour=4, minute=0)
            self.conn.execute(
                "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("i-a", "1d", future.isoformat(), 5_000.0, 5_000.0, 5_000.0,
                 5_000.0, 5_000.0, 1_000.0, "test", AS_OF.isoformat()))
        self.conn.commit()
        replayed = self.service.evaluate("pf", AS_OF).decision
        self.assertEqual(original.state, replayed.state)
        self.assertEqual(original.summary, replayed.summary)

    def test_a_different_constraint_version_can_change_the_verdict(self):
        """Versioning is only meaningful if the version actually matters."""
        strict = default_constraint_set()
        strict.version = "v-strict"
        for constraint in strict.constraints:
            if constraint.scope.value == "gross_exposure":
                constraint.max_value = 0.01
        ConstraintRepository(self.conn).save(strict)

        default_decision = self.service.evaluate("pf", AS_OF).decision
        strict_decision = PortfolioService(
            self.conn, constraint_version="v-strict").evaluate("pf", AS_OF).decision
        self.assertNotEqual(default_decision.state, strict_decision.state)
        self.assertEqual(strict_decision.state, RiskDecisionState.REJECTED)


class TestPersistence(ServiceTestCase):
    def test_persisting_writes_snapshot_and_decision(self):
        result = self.service.evaluate("pf", AS_OF, persist=True)
        stored = self.repository.get_decision(result.decision.decision_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.state, result.decision.state)

    def test_decision_round_trip_preserves_provenance(self):
        result = self.service.evaluate("pf", AS_OF, persist=True)
        stored = self.repository.get_decision(result.decision.decision_id)
        self.assertEqual(stored.provenance.risk_engine_version, "v1")
        self.assertEqual(stored.provenance.constraint_set_version, "v1")
        self.assertEqual(stored.provenance.portfolio_snapshot_as_of, AS_OF)

    def test_violations_survive_the_round_trip(self):
        strict = default_constraint_set()
        strict.version = "v-tight"
        for constraint in strict.constraints:
            if constraint.scope.value == "gross_exposure":
                constraint.max_value = 0.01
        ConstraintRepository(self.conn).save(strict)

        service = PortfolioService(self.conn, constraint_version="v-tight")
        result = service.evaluate("pf", AS_OF, persist=True)
        stored = self.repository.get_decision(result.decision.decision_id)
        self.assertTrue(stored.violations)
        self.assertTrue(stored.blocking_violations)

    def test_repeated_persistence_is_idempotent(self):
        self.service.evaluate("pf", AS_OF, persist=True)
        self.service.evaluate("pf", AS_OF, persist=True)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM risk_decisions WHERE portfolio_id = 'pf'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_equity_curve_excludes_incomplete_snapshots(self):
        """An incomplete snapshot's equity is understated and would fake a trough."""
        good = self.service.build_snapshot("pf", AS_OF - timedelta(days=10))
        self.repository.save_snapshot("s-good", good)

        bad = self.service.build_snapshot("pf", AS_OF - timedelta(days=5))
        bad.unvalued_positions = list(bad.valuations)      # mark it incomplete
        self.repository.save_snapshot("s-bad", bad)

        curve = self.repository.equity_curve("pf", AS_OF)
        self.assertEqual(len(curve), 1)

    def test_decisions_for_lists_recent_decisions(self):
        self.service.evaluate("pf", AS_OF, persist=True)
        rows = self.repository.decisions_for("pf")
        self.assertEqual(len(rows), 1)
        self.assertIn("state", rows[0])


class TestEmptyPortfolio(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_candles(self.conn, "i-a", AS_OF, days=250, start_price=100.0)
        PortfolioRepository(self.conn).save_portfolio("empty", "Empty", cash=10_000.0)
        self.service = PortfolioService(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_portfolio_evaluates_without_error(self):
        result = self.service.evaluate("empty", AS_OF)
        self.assertEqual(result.decision.state, RiskDecisionState.APPROVED)

    def test_empty_portfolio_reports_no_metrics_rather_than_zeros(self):
        result = self.service.evaluate("empty", AS_OF)
        self.assertIsNone(result.metrics.concentration.hhi)
        self.assertIn("volatility", result.metrics.unavailable)

    def test_a_signal_can_be_sized_into_an_empty_portfolio(self):
        signal = make_signal("i-a", confidence=0.85, signal_id="s-1")
        result = self.service.evaluate(
            "empty", AS_OF, sizing=FixedFractionSizing(0.05), signals=[signal])
        self.assertEqual(len(result.proposal.changes), 1)
        self.assertEqual(result.decision.state, RiskDecisionState.APPROVED)
        self.assertEqual(len(result.intents), 1)
        self.assertEqual(result.intents[0].side, "buy")


if __name__ == "__main__":
    unittest.main()
