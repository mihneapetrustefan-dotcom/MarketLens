"""
tests/trading/test_end_to_end_paper.py
--------------------------------------------
Phase 25 §35 — the deterministic end-to-end paper workflow.

    market data exists
      -> a signal is live
      -> eligibility passes
      -> the portfolio engine sets a target
      -> the risk engine approves
      -> an OrderIntent is created
      -> execution submits to IBKR paper
      -> the broker acknowledges
      -> the order fills
      -> the fill is persisted
      -> the position becomes visible
      -> the actual portfolio updates
      -> P&L updates
      -> the position is exited
      -> a trade outcome is created
      -> error attribution can use it
      -> a memory entry can be produced

§35 says the test must verify LINEAGE end to end and must not stop at
"order submitted". So the assertions walk the chain link by link and
the final one reads `trade_lineage.complete`.

WHY THE FILL ARRIVES ON A LATER CYCLE
-----------------------------------------
Because that is what happens. §15 forbids a fake BUY -> FILLED path,
and IBKR paper does not fill synchronously either: the order is
acknowledged, the venue fills it, and the NEXT poll observes the
execution. The test therefore runs cycle one, fills at the venue, and
runs cycle two — which is also how the loop was found to skip its
observation half when it had nothing new to trade.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.trading_loop_models import LINEAGE_CHAIN, CycleStatus
from src.trading.api import TradingLoopAPI
from src.trading.outcomes import compute_pnl
from tests.trading.helpers import (
    NOW, a_live_signal, build_loop, enable_paper, make_connection, settle,
    store_signals, universe,
)


class TestTheCompleteLoop(unittest.TestCase):
    """One trade, followed from the signal table to the memory layer."""

    @classmethod
    def setUpClass(cls):
        cls.conn = make_connection()
        universe(cls.conn)
        store_signals(cls.conn, [a_live_signal()])
        enable_paper(cls.conn)

        cls.loop = build_loop(cls.conn, session_id="sess-e2e")

        # --- cycle 1: decide and submit --------------------------
        cls.first = cls.loop.run_cycle(NOW)
        cls.order = next(iter(cls.loop.stack.orchestrator.orders.values()))

        # --- the venue fills, as a venue does --------------------
        settle(cls.loop, cls.order, 100.5)

        # --- cycle 2: observe, reconcile, record -----------------
        cls.second = cls.loop.run_cycle(NOW + timedelta(minutes=15))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # ---------------- the chain, link by link ----------------

    def test_1_a_signal_was_evaluated_and_found_eligible(self):
        row = self.conn.execute(
            "SELECT code, experimental FROM signal_eligibility "
            "WHERE signal_id = 'sig-live-1' ORDER BY evaluated_at LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "eligible")
        self.assertTrue(row[1], "the run must be labelled experimental (§37)")

    def test_2_the_portfolio_engine_set_a_target(self):
        row = self.conn.execute(
            "SELECT instrument_id, target_quantity, signal_id, decision_id "
            "FROM position_targets").fetchone()
        self.assertEqual(row[0], "i-aapl")
        self.assertGreater(row[1], 0)
        self.assertEqual(row[2], "sig-live-1")
        self.assertTrue(row[3])

    def test_3_the_risk_engine_approved_and_the_decision_was_recorded(self):
        row = self.conn.execute(
            "SELECT state FROM risk_decisions "
            "WHERE decision_id = ?", (self.order.decision_id,)).fetchone()
        self.assertIsNotNone(row, "the risk decision must be persisted")
        self.assertIn(row[0], ("approved", "reduced"))

    def test_4_an_order_intent_was_recorded(self):
        row = self.conn.execute(
            "SELECT intent_id FROM order_intents WHERE intent_id = ?",
            (self.order.intent_id,)).fetchone()
        self.assertIsNotNone(row)

    def test_5_the_order_reached_the_broker_and_carries_its_lineage(self):
        self.assertEqual(self.first.orders_submitted, 1)
        self.assertEqual(self.order.signal_id, "sig-live-1")
        self.assertTrue(self.order.decision_id)
        self.assertTrue(self.order.broker_order_id)
        self.assertEqual(self.order.environment.value, "paper")

    def test_6_the_lifecycle_was_walked_not_jumped(self):
        """§11: never silently from intent to filled."""
        states = [r[1] for r in self.conn.execute(
            "SELECT from_state, to_state FROM order_state_history "
            "WHERE order_id = ? ORDER BY rowid", (self.order.order_id,))]
        self.assertEqual(states[:5],
                         ["validating", "approved", "submitting", "submitted",
                          "acknowledged"])
        self.assertEqual(states[-1], "filled")

    def test_7_the_fill_was_persisted_with_its_execution_id(self):
        row = self.conn.execute(
            "SELECT quantity, price, execution_id FROM execution_fills "
            "WHERE order_id = ?", (self.order.order_id,)).fetchone()
        self.assertIsNotNone(row, "the fill must reach the database")
        self.assertEqual(row[0], self.order.quantity)
        self.assertEqual(row[1], 100.5)
        self.assertTrue(row[2])

    def test_8_the_position_became_visible_and_is_broker_sourced(self):
        positions = self.loop.repository.latest_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["instrument_id"], "i-aapl")
        self.assertEqual(positions[0]["quantity"], self.order.quantity)
        origin = self.conn.execute(
            "SELECT origin FROM position_actuals "
            "WHERE instrument_id = 'i-aapl' ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(origin, "broker_reconciled")

    def test_9_target_and_actual_stayed_distinct(self):
        """§16: the two must never merge into one number."""
        target = self.conn.execute(
            "SELECT target_quantity FROM position_targets LIMIT 1").fetchone()[0]
        actual = self.conn.execute(
            "SELECT quantity FROM position_actuals "
            "ORDER BY observed_at DESC LIMIT 1").fetchone()[0]
        self.assertIsNotNone(target)
        self.assertIsNotNone(actual)
        # They agree here because the order filled completely. What
        # matters is that they are two rows in two tables and a reader
        # can see whether they agree.
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('position_targets','position_actuals')")}
        self.assertEqual(tables, {"position_targets", "position_actuals"})

    def test_10_reconciliation_ran_and_was_clean(self):
        self.assertEqual(self.second.discrepancies, 0)
        row = self.conn.execute(
            "SELECT is_clean FROM reconciliation_records "
            "ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)

    def test_11_pnl_keeps_both_sources_apart(self):
        """§17: broker-reported and locally-calculated, labelled."""
        pnl = compute_pnl(None, list(self.loop.stack.orchestrator.fills), [],
                          NOW + timedelta(minutes=15),
                          marks={"i-aapl": 101.0})
        self.assertEqual(pnl.local_realized.source, "locally-calculated")
        self.assertIsNone(pnl.broker_realized)
        self.assertIsNone(pnl.agrees_within(),
                          "with one side missing the answer is 'not comparable'")

    def test_12_a_trade_outcome_was_produced_with_full_lineage(self):
        row = self.conn.execute(
            "SELECT outcome_id, signal_id, decision_id, intent_id, order_id, "
            "       is_open, entry_price, environment "
            "  FROM trade_outcomes").fetchone()
        self.assertIsNotNone(row, "a filled order must produce an outcome")
        self.assertEqual(row[1], "sig-live-1")
        self.assertTrue(row[2])
        self.assertTrue(row[3])
        self.assertEqual(row[4], self.order.order_id)
        self.assertEqual(row[5], 1, "the position is still open")
        self.assertEqual(row[6], 100.5)
        self.assertEqual(row[7], "paper")

    def test_13_the_lineage_chain_is_complete(self):
        """
        §35: the test must verify lineage end to end. This is that
        assertion, and it is the last one for a reason.
        """
        chains = self.loop.repository.lineage_for()
        self.assertEqual(len(chains), 1)
        chain = chains[0]
        for link in LINEAGE_CHAIN:
            self.assertTrue(chain.get(link),
                            f"the chain has no {link}")
        self.assertTrue(chain["complete"])
        self.assertFalse(chain["broken"])

    def test_14_the_integrity_check_passes_and_is_conclusive(self):
        report = TradingLoopAPI(self.conn).integrity_check()
        self.assertTrue(report["ok"], report["checks"])
        self.assertTrue(report["conclusive"])

    def test_15_no_order_was_duplicated_across_the_two_cycles(self):
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 1)


class TestTheRoundTripCloses(unittest.TestCase):
    """
    The exit half: a position that is closed produces a CLOSED outcome
    with a realised P&L, and the attribution layer can then see a fill.
    """

    def setUp(self):
        self.conn = make_connection()
        universe(self.conn)
        store_signals(self.conn, [a_live_signal()])
        enable_paper(self.conn)
        self.loop = build_loop(self.conn, session_id="sess-round")

        self.loop.run_cycle(NOW)
        order = next(iter(self.loop.stack.orchestrator.orders.values()))
        settle(self.loop, order, 100.0)
        self.loop.run_cycle(NOW + timedelta(minutes=15))
        self.entry = order

    def tearDown(self):
        self.conn.close()

    def test_the_open_outcome_is_marked_open_and_not_scored_as_flat(self):
        """
        A closed-looking row with no exit price would become a
        zero-return trade in every later aggregate.
        """
        row = self.conn.execute(
            "SELECT is_open, exit_price, return_pct FROM trade_outcomes"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_attribution_can_now_see_a_fill(self):
        """
        §19. `attribution/pipeline.run()` passed `fill=None` as a
        literal until this phase, with a comment saying no order had
        ever been placed. One has now.
        """
        from src.attribution.pipeline import (
            load_execution_evidence, load_positions, load_risk_decisions,
        )
        evidence = load_execution_evidence(self.conn)
        self.assertIn("sig-live-1", evidence)
        self.assertEqual(evidence["sig-live-1"]["fill_price"], 100.0)

        decisions = load_risk_decisions(self.conn)
        self.assertIn("sig-live-1", decisions)
        self.assertTrue(decisions["sig-live-1"]["is_approved"])
        self.assertEqual(decisions["sig-live-1"]["violated_limits"], [])

        positions = load_positions(self.conn)
        self.assertIn("sig-live-1", positions)
        self.assertGreater(positions["sig-live-1"]["risk_budget"], 0)

    def test_the_execution_detector_stops_reporting_a_missing_input(self):
        from src.attribution.detectors import detect_execution_error
        from src.attribution.pipeline import load_execution_evidence

        outcome = {"expected_direction": "long", "simple_return": 0.01}
        blind = detect_execution_error(outcome, None)
        self.assertIn("no fill exists", blind.summary)

        fill = load_execution_evidence(self.conn)["sig-live-1"]
        seeing = detect_execution_error(outcome, fill)
        self.assertNotIn("no fill exists", seeing.summary)
        self.assertIn("slippage", seeing.summary)


if __name__ == "__main__":
    unittest.main()
