"""
tests/trading/test_audit_25_5.py
--------------------------------------
Phase 25.5 — regression tests for the defects the audit found.

Every test here corresponds to a finding. They are kept together
rather than filed into the other suites because the point of the file
is the list: these are the things that were wrong, and this is what
stops them coming back.

Each was found by DOING something the existing tests never did — half
filling an order, breaking the model gate, asking what a lineage row
actually contained. The lesson those three share is that a suite which
only exercises the happy path proves the happy path.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.trading_loop_models import (
    LINEAGE_CHAIN, PROVENANCE_LINKS, BlockReason, CycleStatus, LoopStage,
    StageOutcome, TradeLineage, TradingMode,
)
from src.trading.api import TradingLoopAPI
from src.trading.mode import TradingModeStore
from tests.trading.helpers import (
    NOW, a_live_signal, build_loop, enable_paper, make_connection, settle,
    store_signals, universe,
)


def a_ready_database():
    conn = make_connection()
    universe(conn)
    store_signals(conn, [a_live_signal()])
    enable_paper(conn)
    return conn


# ======================================================================
# F-1 — the partial-fill path had never executed
# ======================================================================

class TestPartialFills(unittest.TestCase):
    """
    `_record_unpaired` called `apply_fill_to_order(order, fill,
    self.machine, at=now)`, which is not that function's signature. It
    raised TypeError inside a generator inside the broker-poll stage,
    so the ONLY path that can apply a partial fill had never once
    completed. No test noticed: every test filled its order completely
    and took the paired path.
    """

    def setUp(self):
        self.conn = a_ready_database()
        self.loop = build_loop(self.conn, session_id="partial")
        self.loop.run_cycle(NOW)
        self.order = next(iter(self.loop.stack.orchestrator.orders.values()))

    def tearDown(self):
        self.conn.close()

    def test_a_half_fill_is_applied_and_the_state_moves(self):
        self.loop.stack.transport.fill(
            self.order.broker_order_id, self.order.quantity / 2, 100.0)
        result = self.loop.run_cycle(NOW + timedelta(minutes=15))

        self.assertIs(result.status, CycleStatus.COMPLETED)
        self.assertEqual(result.fills_recorded, 1)
        self.assertEqual(self.order.state.value, "partially_filled")
        self.assertAlmostEqual(self.order.filled_quantity,
                               self.order.quantity / 2)

    def test_a_half_fill_becomes_a_half_position(self):
        self.loop.stack.transport.fill(
            self.order.broker_order_id, self.order.quantity / 2, 100.0)
        self.loop.run_cycle(NOW + timedelta(minutes=15))
        positions = self.loop.repository.latest_positions()
        self.assertEqual(positions[0]["quantity"], self.order.quantity / 2)

    def test_the_remainder_is_not_re_ordered(self):
        """The unfilled half is pending, not an opportunity."""
        self.loop.stack.transport.fill(
            self.order.broker_order_id, self.order.quantity / 2, 100.0)
        self.loop.run_cycle(NOW + timedelta(minutes=15))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 1)

    def test_two_partials_average_correctly_and_close_the_order(self):
        half = self.order.quantity / 2
        self.loop.stack.transport.fill(self.order.broker_order_id, half, 100.0)
        self.loop.run_cycle(NOW + timedelta(minutes=15))
        self.loop.stack.transport.fill(self.order.broker_order_id, half, 100.6)
        self.loop.run_cycle(NOW + timedelta(minutes=30))

        self.assertEqual(self.order.state.value, "filled")
        self.assertAlmostEqual(self.order.filled_quantity, self.order.quantity)
        # Recomputed from the running notional, not averaged with the
        # previous average -- which would weight unequal fills wrongly.
        self.assertAlmostEqual(self.order.average_fill_price, 100.3, places=6)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_fills")
            .fetchone()[0], 2)

    def test_an_overfill_is_recorded_and_never_applied(self):
        """A venue reporting more than we ordered is a discrepancy."""
        self.loop.stack.transport.fill(
            self.order.broker_order_id, self.order.quantity, 100.0)
        self.loop.run_cycle(NOW + timedelta(minutes=15))
        before = self.order.filled_quantity

        # A second execution for an already-complete order.
        self.loop.stack.transport.orders[
            self.order.broker_order_id].filled = 0.0
        self.loop.stack.transport.fill(
            self.order.broker_order_id, self.order.quantity, 100.0,
            execution_id="ex-duplicate")
        self.loop.run_cycle(NOW + timedelta(minutes=30))
        self.assertEqual(self.order.filled_quantity, before)


# ======================================================================
# F-2 — the model was never recorded on anything
# ======================================================================

class TestProvenanceReachesTheRecord(unittest.TestCase):
    """
    Orders, lineage rows and trade outcomes all carried
    `model_version = None`, `prediction_id = None`,
    `trained_model_id = None` and `strategy_id = None`. Phase 16
    recorded `lineage_complete = 0` on every paper trade while Phase 25
    recorded its own chain as complete — because Phase 25's chain did
    not include the model.

    The fixture hid it: `make_signal` builds a signal with no
    `ModelContribution`, so there was no model to lose.
    """

    def setUp(self):
        self.conn = a_ready_database()
        self.loop = build_loop(self.conn, session_id="prov")
        self.loop.run_cycle(NOW)
        self.order = next(iter(self.loop.stack.orchestrator.orders.values()))
        settle(self.loop, self.order, 100.5)
        self.loop.run_cycle(NOW + timedelta(minutes=15))

    def tearDown(self):
        self.conn.close()

    def test_the_order_names_its_model_and_prediction(self):
        self.assertEqual(self.order.model_version, "ridge_abnormal_return:v1")
        self.assertEqual(self.order.prediction_id, "pred-sig-live-1")

    def test_the_strategy_comes_from_the_signal_not_the_configuration(self):
        """
        The loop was started with no `--strategy`. The value on the
        order is the one Phase 10 recorded on the signal, which is the
        truthful answer to "which strategy produced this".
        """
        self.assertEqual(self.order.strategy_id, "test")

    def test_the_lineage_row_names_the_trained_model(self):
        row = self.conn.execute(
            "SELECT trained_model_id, model_version, strategy_id "
            "FROM trade_lineage").fetchone()
        self.assertEqual(row[0], "tm-fixture-1")
        self.assertEqual(row[1], "ridge_abnormal_return:v1")
        self.assertEqual(row[2], "test")

    def test_phase_16_now_agrees_the_lineage_is_complete(self):
        row = self.conn.execute(
            "SELECT model_id, model_version, prediction_id, strategy_id, "
            "       lineage_complete FROM trade_outcomes").fetchone()
        self.assertEqual(row[0], "tm-fixture-1")
        self.assertEqual(row[1], "ridge_abnormal_return:v1")
        self.assertEqual(row[2], "pred-sig-live-1")
        self.assertEqual(row[3], "test")
        self.assertEqual(row[4], 1)

    def test_the_two_lineage_models_are_compared_not_assumed(self):
        report = TradingLoopAPI(self.conn).integrity_check()
        names = {c["name"]: c for c in report["checks"]}
        self.assertIn("lineage_models_agree", names)
        self.assertTrue(names["lineage_models_agree"]["ok"])
        self.assertEqual(names["lineage_models_agree"]["counted"], 1)

    def test_that_comparison_would_actually_fire(self):
        """The negative control: make the two disagree and watch it fail."""
        self.conn.execute("UPDATE trade_outcomes SET lineage_complete = 0")
        self.conn.commit()
        report = TradingLoopAPI(self.conn).integrity_check()
        failed = [c["name"] for c in report["checks"] if c["ok"] is False]
        self.assertIn("lineage_models_agree", failed)

    def test_a_signal_with_no_model_reports_the_gap_rather_than_hiding_it(self):
        chain = TradeLineage(cycle_id="c", signal_id="s", decision_id="d",
                             intent_id="i", order_id="o", fill_id="f",
                             position_instrument_id="x", outcome_id="y",
                             strategy_id="rule-based")
        # The execution spine is complete...
        self.assertTrue(chain.is_complete)
        # ...and the provenance gap is still reported.
        self.assertEqual(chain.missing_provenance(),
                         ["trained_model_id", "model_version"])
        self.assertFalse(chain.has_provenance)

    def test_the_spine_and_the_provenance_are_disjoint(self):
        self.assertEqual(set(LINEAGE_CHAIN) & set(PROVENANCE_LINKS), set())


# ======================================================================
# F-4 — an environment variable alone could enable trading
# ======================================================================

class TestTheEnvironmentMayOnlyRestrict(unittest.TestCase):
    """
    `MARKETLENS_TRADING_MODE=paper` on a database that had never
    recorded a mode was enough to trade — with no actor, no reason and
    no history row, which is precisely the durability and auditability
    the kill switch was made durable for.
    """

    def setUp(self):
        self.conn = make_connection()
        self.previous = os.environ.get("MARKETLENS_TRADING_MODE")

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("MARKETLENS_TRADING_MODE", None)
        else:
            os.environ["MARKETLENS_TRADING_MODE"] = self.previous
        self.conn.close()

    def test_the_environment_cannot_grant_paper(self):
        os.environ["MARKETLENS_TRADING_MODE"] = "paper"
        resolution = TradingModeStore(self.conn).resolve(NOW)
        self.assertIs(resolution.mode, TradingMode.OFF)
        self.assertIn("cannot grant", resolution.reason)

    def test_the_environment_can_switch_a_stored_paper_mode_off(self):
        TradingModeStore(self.conn).set_mode(
            TradingMode.PAPER, actor="a", reason="r", at=NOW)
        os.environ["MARKETLENS_TRADING_MODE"] = "off"
        resolution = TradingModeStore(self.conn).resolve(NOW)
        self.assertIs(resolution.mode, TradingMode.OFF)
        self.assertIn("restrict", resolution.reason)

    def test_the_environment_saying_live_switches_trading_off(self):
        TradingModeStore(self.conn).set_mode(
            TradingMode.PAPER, actor="a", reason="r", at=NOW)
        os.environ["MARKETLENS_TRADING_MODE"] = "live"
        self.assertIs(TradingModeStore(self.conn).resolve(NOW).mode,
                      TradingMode.OFF)

    def test_a_stored_paper_mode_still_trades_with_no_environment(self):
        os.environ.pop("MARKETLENS_TRADING_MODE", None)
        TradingModeStore(self.conn).set_mode(
            TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.assertTrue(TradingModeStore(self.conn).resolve(NOW).may_trade)


# ======================================================================
# F-5 — a replay could trade historical signals at the live venue
# ======================================================================

class TestTheAnchorMustDescribeNow(unittest.TestCase):

    def setUp(self):
        self.conn = a_ready_database()

    def tearDown(self):
        self.conn.close()

    def test_an_anchor_far_behind_the_wall_clock_does_not_trade(self):
        """
        Point-in-time correctness protects the DECISION. Nothing
        protected the EXECUTION: `run_cycle(now=...)` takes its moment
        as an argument, so a backfill would decide on month-old signals
        and send the orders to today's venue.
        """
        loop = build_loop(self.conn, session_id="replay",
                          max_anchor_drift_seconds=3600.0)
        result = loop.run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 0)
        self.assertIn(BlockReason.STALE_SIGNAL,
                      [b.reason for b in result.blocks])

    def test_a_blocked_replay_still_observes(self):
        """A replay that cannot trade is still a useful read."""
        loop = build_loop(self.conn, session_id="replay2",
                          max_anchor_drift_seconds=3600.0)
        result = loop.run_cycle(NOW)
        reached = {s.stage for s in result.stages}
        for stage in (LoopStage.BROKER_POLL, LoopStage.POSITIONS,
                      LoopStage.RECONCILIATION):
            self.assertIn(stage, reached)

    def test_the_limit_is_part_of_the_session_fingerprint(self):
        """A threshold nobody can see is a threshold nobody reviews."""
        loop = build_loop(self.conn, session_id="fp",
                          max_anchor_drift_seconds=1234.0)
        self.assertIn("max_anchor_drift_seconds", loop.config.as_dict())


# ======================================================================
# F-6 / F-10 — an unreadable gate, and a block that stopped nothing
# ======================================================================

class TestABlockActuallyStopsTrading(unittest.TestCase):
    """
    `result.block()` recorded a reason and stopped nothing. The only
    thing that stopped a cycle trading was `health is BLOCKED`, so any
    block recorded after the health verdict was written into the record
    and then ignored — and the cycle submitted anyway.
    """

    def setUp(self):
        self.conn = a_ready_database()

    def tearDown(self):
        self.conn.close()

    def broken_gate(self):
        import src.modeling.selection as selection

        def explode(*args, **kwargs):
            raise RuntimeError("the model gate fell over")

        return selection, selection.candidates, explode

    def test_an_unreadable_model_gate_is_reported_not_silent(self):
        """
        The two helpers each swallowed every exception and returned an
        empty dict, which is indistinguishable from "nothing has been
        promoted".
        """
        selection, original, explode = self.broken_gate()
        loop = build_loop(self.conn, session_id="gate")
        selection.candidates = explode
        try:
            result = loop.run_cycle(NOW)
        finally:
            selection.candidates = original

        self.assertIn(BlockReason.MODEL_NOT_DEPLOYABLE,
                      [b.reason for b in result.blocks])
        self.assertIn("the model gate raised RuntimeError",
                      result.blocks[0].detail)

    def test_an_unreadable_model_gate_places_no_order(self):
        selection, original, explode = self.broken_gate()
        loop = build_loop(self.conn, session_id="gate2")
        selection.candidates = explode
        try:
            result = loop.run_cycle(NOW)
        finally:
            selection.candidates = original

        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 0)
        self.assertIs(result.status, CycleStatus.BLOCKED)

    def test_the_submission_stage_names_why_it_refused(self):
        selection, original, explode = self.broken_gate()
        loop = build_loop(self.conn, session_id="gate3")
        selection.candidates = explode
        try:
            result = loop.run_cycle(NOW)
        finally:
            selection.candidates = original

        stage = result.stage(LoopStage.SUBMISSION)
        self.assertIs(stage.outcome, StageOutcome.BLOCKED)
        self.assertIn("not submitted", stage.detail)

    def test_an_absent_model_table_is_an_answer_not_a_failure(self):
        """
        Nothing trained here means nothing promoted, which is a
        verdict. It must not read as a broken gate.
        """
        loop = build_loop(self.conn, session_id="empty")
        result = loop.run_cycle(NOW)
        self.assertNotIn(BlockReason.MODEL_NOT_DEPLOYABLE,
                         [b.reason for b in result.blocks])
        self.assertEqual(result.orders_submitted, 1)


# ======================================================================
# §18 — an unresolved discrepancy must block new trading
# ======================================================================

class TestReconciliationBlocksTrading(unittest.TestCase):

    def setUp(self):
        self.conn = a_ready_database()

    def tearDown(self):
        self.conn.close()

    def test_a_position_the_broker_reports_and_we_did_not_order_blocks(self):
        """
        A ghost position is a genuine discrepancy: the account holds
        something no local order explains.
        """
        loop = build_loop(self.conn, session_id="ghost")
        loop.run_cycle(NOW)
        order = next(iter(loop.stack.orchestrator.orders.values()))
        settle(loop, order, 100.0)

        # The venue now reports an order we have no record of.
        loop.stack.transport.place_order(
            loop.stack.account_id,
            {"conid": "265598", "side": "BUY", "quantity": 10,
             "orderType": "MKT", "tif": "DAY", "cOID": "not-ours"})

        result = loop.run_cycle(NOW + timedelta(minutes=15))
        self.assertGreater(result.discrepancies, 0)
        self.assertIn(BlockReason.RECONCILIATION_UNRESOLVED,
                      [b.reason for b in result.blocks])

    def test_an_unresolved_discrepancy_places_no_new_order(self):
        loop = build_loop(self.conn, session_id="ghost2")
        loop.run_cycle(NOW)
        order = next(iter(loop.stack.orchestrator.orders.values()))
        settle(loop, order, 100.0)
        loop.stack.transport.place_order(
            loop.stack.account_id,
            {"conid": "265598", "side": "BUY", "quantity": 10,
             "orderType": "MKT", "tif": "DAY", "cOID": "not-ours"})

        before = self.conn.execute(
            "SELECT COUNT(*) FROM execution_orders").fetchone()[0]
        loop.run_cycle(NOW + timedelta(minutes=15))
        after = self.conn.execute(
            "SELECT COUNT(*) FROM execution_orders").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
