"""
tests/paper/test_session.py
--------------------------------
End-to-end paper session behaviour
(spec §10, §11, §12, §29, §31, §33, §34, §36, §55).

The claim these have to establish is the one the whole phase rests on:
that the REAL Phase 11 risk engine gates every order, and that there is
no path from a signal to a fill that avoids it.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.paper_models import (
    DataFreshness, HealthState, PaperAccountStatus, PaperEventKind,
    PaperOrderState, PaperSessionStatus,
)
from src.domain.portfolio_models import ConstraintScope
from src.paper.clock import FixedClock
from src.paper.health import DECISION_COMPONENTS, PIPELINE_COMPONENTS
from src.paper.session import PAPER_PORTFOLIO_ID, PaperTradingSession
from src.portfolio.constraints import ConstraintRepository, default_constraint_set
from tests.paper.helpers import (
    END, anchors_for, make_account, make_config, make_connection, make_session,
    signals_for, standard_universe,
)


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        self.universe = standard_universe(self.conn)
        self.anchors = anchors_for(self.conn, self.universe, days=40)
        self.signals = signals_for(self.universe, count=4)

    def tearDown(self):
        self.conn.close()

    def build(self, **config_overrides):
        account = make_account()
        config = make_config(self.universe, **config_overrides)
        session = make_session(config)
        return PaperTradingSession(
            self.conn, account, session, clock=FixedClock(self.anchors[0]),
            signals=self.signals)

    def run_all(self, runner):
        results = []
        for anchor in self.anchors:
            runner.clock.set(anchor)
            results.append(runner.tick(anchor))
        return results


class TestTicking(SessionTestCase):
    def test_a_tick_returns_a_result(self):
        runner = self.build()
        result = runner.tick(self.anchors[-1])
        self.assertEqual(result.session_id, runner.session.session_id)
        self.assertIsNotNone(result.snapshot)

    def test_ticking_advances_the_session(self):
        runner = self.build()
        self.run_all(runner)
        self.assertEqual(runner.session.ticks_processed, len(self.anchors))
        self.assertEqual(runner.session.status, PaperSessionStatus.RUNNING)

    def test_the_first_tick_starts_the_session(self):
        runner = self.build()
        runner.tick(self.anchors[0])
        self.assertIsNotNone(runner.session.started_at)

    def test_last_tick_is_recorded_for_recovery(self):
        runner = self.build()
        self.run_all(runner)
        self.assertEqual(runner.session.last_tick_at, self.anchors[-1])

    def test_snapshots_accumulate(self):
        runner = self.build()
        self.run_all(runner)
        self.assertEqual(len(runner.snapshots), len(self.anchors))

    def test_the_session_trades(self):
        runner = self.build()
        self.run_all(runner)
        self.assertGreater(len(runner.fills), 0)


class TestRiskCannotBeBypassed(SessionTestCase):
    """Spec §31 — the load-bearing guarantee of this phase."""

    def test_a_tight_constraint_actually_stops_trading(self):
        """
        If the paper path bypassed risk, this would trade regardless.
        A near-zero gross exposure cap must stop it.
        """
        strict = default_constraint_set()
        strict.version = "v-block"
        for constraint in strict.constraints:
            if constraint.scope == ConstraintScope.GROSS_EXPOSURE:
                constraint.max_value = 0.0001
        ConstraintRepository(self.conn).save(strict)

        permissive = self.build()
        self.run_all(permissive)
        blocked = self.build(constraint_set_version="v-block")
        self.run_all(blocked)

        self.assertGreater(len(permissive.fills), 0)
        self.assertEqual(len(blocked.fills), 0)

    def test_orders_only_come_from_risk_approved_intents(self):
        runner = self.build()
        self.run_all(runner)
        for order in runner.executor.get_orders():
            self.assertIsNotNone(order.decision_id)

    def test_the_paper_book_is_evaluated_not_the_live_portfolio(self):
        runner = self.build()
        self.run_all(runner)
        # Nothing was written to the Phase 11 tables.
        for table in ("positions", "portfolios", "risk_decisions"):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)

    def test_a_blocked_tick_explains_itself(self):
        strict = default_constraint_set()
        strict.version = "v-block2"
        for constraint in strict.constraints:
            if constraint.scope == ConstraintScope.GROSS_EXPOSURE:
                constraint.max_value = 0.0001
        ConstraintRepository(self.conn).save(strict)

        runner = self.build(constraint_set_version="v-block2")
        results = self.run_all(runner)
        blocked = [r for r in results if r.was_blocked]
        self.assertTrue(blocked)
        self.assertIn("risk", blocked[-1].blocked_reason)


class TestProvenance(SessionTestCase):
    """Spec §29 — signal to fill must stay a queryable chain."""

    def test_orders_carry_their_signal_and_decision(self):
        runner = self.build()
        self.run_all(runner)
        orders = [o for o in runner.executor.get_orders() if o.signal_id]
        self.assertTrue(orders)
        for order in orders:
            self.assertIsNotNone(order.decision_id)

    def test_fills_link_back_to_their_order(self):
        runner = self.build()
        self.run_all(runner)
        order_ids = {o.order_id for o in runner.executor.get_orders()}
        for fill in runner.fills:
            self.assertIn(fill.order_id, order_ids)

    def test_the_chain_reaches_a_real_signal(self):
        runner = self.build()
        self.run_all(runner)
        signal_ids = {s.signal_id for s in self.signals}
        linked = [o for o in runner.executor.get_orders() if o.signal_id]
        self.assertTrue(linked)
        for order in linked:
            self.assertIn(order.signal_id, signal_ids)


class TestIdempotency(SessionTestCase):
    """Spec §12 — a re-run must recognise its own previous work."""

    def test_re_ticking_the_same_moment_creates_no_duplicate_orders(self):
        runner = self.build()
        runner.tick(self.anchors[20])
        first = len(runner.executor.get_orders())
        runner.tick(self.anchors[20])
        self.assertEqual(len(runner.executor.get_orders()), first)

    def test_re_ticking_does_not_double_the_position(self):
        runner = self.build()
        runner.tick(self.anchors[20])
        before = {p.instrument_id: p.quantity
                  for p in runner.ledger.open_positions()}
        runner.tick(self.anchors[20])
        after = {p.instrument_id: p.quantity
                 for p in runner.ledger.open_positions()}
        self.assertEqual(before, after)

    def test_every_order_carries_an_idempotency_key(self):
        runner = self.build()
        self.run_all(runner)
        for order in runner.executor.get_orders():
            self.assertTrue(order.idempotency_key)


class TestControls(SessionTestCase):
    def test_emergency_stop_blocks_new_orders(self):
        runner = self.build()
        runner.tick(self.anchors[10])
        runner.emergency_stop(at=self.anchors[11], reason="test")
        result = runner.tick(self.anchors[12])
        self.assertEqual(result.orders_created, 0)
        self.assertEqual(runner.account.status,
                         PaperAccountStatus.EMERGENCY_STOP)

    def test_emergency_stop_does_not_liquidate(self):
        """
        A safeguard that force-sold would take a decision with its own
        risks. The position stays observable and can be exited
        deliberately.
        """
        runner = self.build()
        for anchor in self.anchors[:25]:
            runner.clock.set(anchor)
            runner.tick(anchor)
        held = len(runner.ledger.open_positions())
        runner.emergency_stop(at=self.anchors[25], reason="test")
        runner.tick(self.anchors[26])
        self.assertEqual(len(runner.ledger.open_positions()), held)

    def test_pause_and_resume_are_audited(self):
        runner = self.build()
        runner.pause(at=self.anchors[5], reason="maintenance")
        runner.resume(at=self.anchors[6], reason="done")
        actions = runner.controls.audit_trail()
        self.assertGreaterEqual(len(actions), 2)
        self.assertTrue(all(a.reason for a in actions))

    def test_a_paused_session_creates_no_orders(self):
        runner = self.build()
        runner.pause(at=self.anchors[5])
        result = runner.tick(self.anchors[10])
        self.assertEqual(result.orders_created, 0)

    def test_stopping_ends_the_session(self):
        runner = self.build()
        runner.stop(at=self.anchors[-1], reason="done")
        self.assertEqual(runner.session.status, PaperSessionStatus.COMPLETED)
        self.assertIsNotNone(runner.session.ended_at)


class TestHealthAndHeartbeats(SessionTestCase):
    """Spec §34, §35 — silence must be detectable, not just errors."""

    def test_every_pipeline_component_reports(self):
        runner = self.build()
        self.run_all(runner)
        health = runner.health_monitor.evaluate(self.anchors[-1])
        for component in PIPELINE_COMPONENTS:
            self.assertIn(component, health.components)

    def test_a_healthy_session_reports_healthy(self):
        runner = self.build()
        results = self.run_all(runner)
        self.assertEqual(results[-1].health, HealthState.HEALTHY)

    def test_decision_components_exclude_post_decision_stages(self):
        """
        Ledger and persistence run after orders are placed; gating on
        them would block every order on the first tick.
        """
        self.assertNotIn("ledger", DECISION_COMPONENTS)
        self.assertNotIn("persistence", DECISION_COMPONENTS)
        self.assertIn("risk", DECISION_COMPONENTS)

    def test_overall_health_is_the_worst_component(self):
        runner = self.build()
        runner.tick(self.anchors[-1])
        runner.health_monitor.fail("risk", "down")
        health = runner.health_monitor.evaluate(self.anchors[-1])
        self.assertEqual(health.overall, HealthState.FAILED)

    def test_a_failed_pipeline_blocks_new_orders(self):
        runner = self.build()
        runner.health_monitor.fail("risk", "down")
        health = runner.health_monitor.evaluate(self.anchors[-1])
        self.assertFalse(health.allows_new_orders)

    def test_safe_mode_blocks_new_orders(self):
        runner = self.build()
        runner.health_monitor.enter_safe_mode("reconciliation failed")
        health = runner.health_monitor.evaluate(self.anchors[-1])
        self.assertFalse(health.allows_new_orders)


class TestLatency(SessionTestCase):
    """Spec §36, §77 — each stage measured separately, honestly."""

    def test_every_stage_is_timed(self):
        runner = self.build()
        result = runner.tick(self.anchors[-1])
        stages = {s.stage for s in result.latencies}
        for expected in ("market_data", "signals", "risk", "ledger"):
            self.assertIn(expected, stages)

    def test_latencies_are_non_negative(self):
        runner = self.build()
        result = runner.tick(self.anchors[-1])
        for sample in result.latencies:
            self.assertGreaterEqual(sample.milliseconds, 0.0)


class TestEventLog(SessionTestCase):
    """Spec §33 — the chronological record debugging depends on."""

    def test_significant_events_are_logged(self):
        runner = self.build()
        self.run_all(runner)
        kinds = {e.kind for e in runner.events}
        for expected in (PaperEventKind.TICK, PaperEventKind.RISK_EVALUATED,
                         PaperEventKind.SNAPSHOT,
                         PaperEventKind.RECONCILIATION):
            self.assertIn(expected, kinds)

    def test_fills_are_logged(self):
        runner = self.build()
        self.run_all(runner)
        if runner.fills:
            self.assertIn(PaperEventKind.FILL, {e.kind for e in runner.events})

    def test_events_are_sequenced(self):
        runner = self.build()
        self.run_all(runner)
        sequences = [e.sequence for e in runner.events]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))


class TestFreshnessGating(SessionTestCase):
    def test_freshness_is_reported_on_every_tick(self):
        runner = self.build()
        results = self.run_all(runner)
        for result in results:
            self.assertIsInstance(result.freshness, DataFreshness)

    def test_a_tick_far_past_the_data_is_stale_and_blocks(self):
        runner = self.build()
        far = self.anchors[-1] + timedelta(days=60)
        result = runner.tick(far)
        self.assertFalse(result.freshness.is_tradeable)
        self.assertEqual(result.orders_created, 0)


class TestDescription(SessionTestCase):
    def test_the_session_declares_itself_paper(self):
        described = self.build().describe()
        self.assertTrue(described["is_paper"])
        self.assertFalse(described["connects_to_broker"])

    def test_the_configuration_fingerprint_is_recorded(self):
        self.assertTrue(self.build().describe()["config_fingerprint"])

    def test_the_paper_portfolio_id_is_not_a_real_portfolio(self):
        self.assertTrue(PAPER_PORTFOLIO_ID.startswith("__"))


if __name__ == "__main__":
    unittest.main()
