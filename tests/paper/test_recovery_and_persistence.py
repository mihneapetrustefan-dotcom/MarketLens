"""
tests/paper/test_recovery_and_persistence.py
-------------------------------------------------
Durability, checkpoints, recovery and export
(spec §52, §63, §64, §78, §79, §80).

A backtest that crashes is re-run. A paper session cannot be — time has
moved on, and the ticks it processed really happened. So these tests
check the property that distinguishes the two: that a session's state
survives the process that produced it.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.paper_repository import PaperRepository
from src.domain.paper_models import (
    PaperAccountStatus, PaperOrderState, PaperSessionStatus,
)
from src.paper.clock import FixedClock
from src.paper.session import PaperTradingSession
from tests.paper.helpers import (
    END, anchors_for, make_account, make_config, make_connection, make_session,
    signals_for, standard_universe,
)


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        self.universe = standard_universe(self.conn)
        self.anchors = anchors_for(self.conn, self.universe, days=40)
        self.signals = signals_for(self.universe, count=4)
        self.repository = PaperRepository(self.conn)
        self.account = make_account()
        self.repository.save_account(self.account)

    def tearDown(self):
        self.conn.close()

    def build(self, ledger=None, session=None, signals=None):
        session = session or make_session(make_config(self.universe))
        return PaperTradingSession(
            self.conn, self.account, session,
            clock=FixedClock(self.anchors[0]), ledger=ledger,
            signals=self.signals if signals is None else signals)

    def run_and_save(self, runner, anchors, checkpoint_every=None):
        for index, anchor in enumerate(anchors, start=1):
            runner.clock.set(anchor)
            result = runner.tick(anchor)
            self.repository.save_tick(
                runner.session, result, runner.executor.get_orders(),
                runner.fills, runner.events,
                health=runner.health_monitor.evaluate(anchor),
                alerts=runner.alerts, controls=runner.controls.audit_trail(),
                ledger=runner.ledger)
            if checkpoint_every and index % checkpoint_every == 0:
                self.repository.save_checkpoint(runner.session, runner.ledger, anchor)
        return runner


class TestRecordedClockKind(RecoveryTestCase):
    """
    Which clock a session ran on is part of what its numbers mean.

    A replay walking 2026-04 and a system-clock session running today
    produce identically-shaped records, and the only thing separating
    them is this field. Losing it makes a replay look like a live run.
    """

    def stored_kind(self, session_id):
        return self.conn.execute(
            "SELECT clock_kind FROM paper_sessions WHERE session_id = ?",
            (session_id,)).fetchone()[0]

    def test_an_explicit_kind_is_recorded(self):
        runner = self.build()
        self.repository.save_session(runner.session, clock_kind="replay")
        self.assertEqual(self.stored_kind(runner.session.session_id), "replay")

    def test_ticking_does_not_rewrite_it_as_a_system_clock(self):
        """
        `save_tick` re-saves the session every tick. Before this was
        fixed, the first tick silently turned every replay into a
        system-clock run.
        """
        runner = self.build()
        self.repository.save_session(runner.session, clock_kind="replay")
        self.run_and_save(runner, self.anchors[:3])
        self.assertEqual(self.stored_kind(runner.session.session_id), "replay")

    def test_a_session_saved_without_a_kind_defaults_to_system_once(self):
        runner = self.build()
        self.repository.save_session(runner.session)
        self.assertEqual(self.stored_kind(runner.session.session_id), "system")


class TestPositionTable(RecoveryTestCase):
    """
    The queryable mirror of the ledger book.

    Recovery does not read this table — checkpoints and fills cover
    that. What reads it is everything that asks what a session holds
    without loading a ledger, and the failure it guards against is a
    snapshot reporting open positions while the positions table sits
    empty: two persisted answers to one question, disagreeing.
    """

    def test_the_positions_table_matches_the_ledger(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20])

        expected = {p.instrument_id: p for p in runner.ledger.open_positions()}
        rows = {r[0]: r for r in self.conn.execute(
            "SELECT instrument_id, quantity, average_cost FROM paper_positions "
            "WHERE session_id = ?", (runner.session.session_id,))}

        self.assertEqual(set(rows), set(expected))
        for instrument_id, position in expected.items():
            self.assertAlmostEqual(rows[instrument_id][1], position.quantity, places=8)
            self.assertAlmostEqual(rows[instrument_id][2], position.average_cost,
                                   places=8)

    def test_the_snapshot_and_the_table_agree_on_how_many_are_open(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20])

        counted = self.conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE session_id = ? "
            "AND quantity != 0", (runner.session.session_id,)).fetchone()[0]
        latest = self.conn.execute(
            "SELECT open_positions FROM paper_snapshots WHERE session_id = ? "
            "ORDER BY at DESC LIMIT 1", (runner.session.session_id,)).fetchone()
        self.assertIsNotNone(latest)
        self.assertEqual(counted, latest[0])

    def test_the_table_is_rewritten_not_appended(self):
        """
        This is current state, not history. If rows accumulated, a
        session that opened and closed the same instrument repeatedly
        would report a book far larger than it holds.
        """
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20])
        session_id = runner.session.session_id

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE session_id = ?",
            (session_id,)).fetchone()[0]
        ever_traded = self.conn.execute(
            "SELECT COUNT(DISTINCT instrument_id) FROM paper_fills "
            "WHERE session_id = ?", (session_id,)).fetchone()[0]

        self.assertEqual(rows, len(list(runner.ledger.open_positions())))
        self.assertLessEqual(rows, max(ever_traded, 1))

    def test_no_row_is_written_for_a_session_holding_nothing(self):
        runner = self.build(signals=[])
        self.run_and_save(runner, self.anchors[:5])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE session_id = ?",
            (runner.session.session_id,)).fetchone()[0], 0)


class TestAccountPersistence(RecoveryTestCase):
    def test_an_account_round_trips(self):
        loaded = self.repository.get_account(self.account.account_id)
        self.assertEqual(loaded.account_id, self.account.account_id)
        self.assertAlmostEqual(loaded.initial_capital,
                               self.account.initial_capital)

    def test_an_account_is_always_paper(self):
        self.assertTrue(self.repository.get_account(
            self.account.account_id).is_paper)

    def test_reset_preserves_history(self):
        """Spec §63 — a reset must not wipe the research it produced."""
        runner = self.build()
        self.run_and_save(runner, self.anchors[:10])
        fills_before = len(self.repository.fills_for(runner.session.session_id))

        reset = self.repository.reset_account(self.account.account_id, END)
        self.assertEqual(reset.generation, 2)
        self.assertEqual(
            len(self.repository.fills_for(runner.session.session_id)),
            fills_before)

    def test_reset_is_audited(self):
        self.repository.reset_account(self.account.account_id, END)
        actions = self.conn.execute(
            "SELECT action, previous_value, new_value FROM paper_control_actions "
            "WHERE action = 'account_reset'").fetchall()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][1], "1")
        self.assertEqual(actions[0][2], "2")


class TestSessionPersistence(RecoveryTestCase):
    def test_a_session_round_trips_with_its_configuration(self):
        session = make_session(make_config(self.universe, sizing_target_weight=0.07))
        self.repository.save_session(session)
        loaded = self.repository.get_session(session.session_id)

        self.assertEqual(loaded.session_id, session.session_id)
        self.assertAlmostEqual(loaded.config.sizing_target_weight, 0.07)
        self.assertEqual(loaded.config.universe, self.universe)

    def test_the_configuration_fingerprint_survives(self):
        session = make_session(make_config(self.universe))
        self.repository.save_session(session)
        loaded = self.repository.get_session(session.session_id)
        self.assertEqual(loaded.config.fingerprint(), session.config.fingerprint())

    def test_version_identity_is_stored(self):
        session = make_session(make_config(self.universe))
        self.repository.save_session(session)
        row = self.conn.execute(
            "SELECT risk_engine_version, cost_model_version, code_version "
            "FROM paper_sessions WHERE session_id = ?",
            (session.session_id,)).fetchone()
        self.assertTrue(all(row))

    def test_ticks_and_position_in_time_are_stored(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:5])
        loaded = self.repository.get_session(runner.session.session_id)
        self.assertEqual(loaded.ticks_processed, 5)
        self.assertEqual(loaded.last_tick_at, self.anchors[4])


class TestTickPersistence(RecoveryTestCase):
    def test_orders_fills_and_snapshots_are_all_written(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:15])
        session_id = runner.session.session_id

        self.assertEqual(len(self.repository.orders_for(session_id)),
                         len(runner.executor.get_orders()))
        self.assertEqual(len(self.repository.fills_for(session_id)),
                         len(runner.fills))
        self.assertEqual(len(self.repository.snapshots_for(session_id)), 15)

    def test_events_are_written(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:5])
        stored = self.repository.events_for(runner.session.session_id,
                                            limit=100_000)
        self.assertEqual(len(stored), len(runner.events))

    def test_health_is_written_per_component(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:3])
        health = self.repository.health_for(runner.session.session_id)
        self.assertTrue(health)
        self.assertTrue(all("component" in row for row in health))

    def test_latency_is_written_per_stage(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:3])
        latency = self.repository.latency_for(runner.session.session_id)
        self.assertTrue(latency)
        self.assertIn("risk", [row["stage"] for row in latency])

    def test_reconciliations_are_written(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:3])
        rows = self.repository.reconciliations_for(runner.session.session_id)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["is_clean"] for row in rows))

    def test_positions_are_derived_from_fills(self):
        """
        Derived rather than stored, so what is shown is what the fills
        justify — the same independence reconciliation relies on.
        """
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20])
        derived = {p["instrument_id"]: p["quantity"]
                   for p in self.repository.positions_for(runner.session.session_id)}
        held = {p.instrument_id: p.quantity
                for p in runner.ledger.open_positions()}
        for instrument_id, quantity in held.items():
            self.assertAlmostEqual(derived.get(instrument_id, 0.0), quantity,
                                   places=6)


class TestIdempotencyAcrossProcesses(RecoveryTestCase):
    """
    The unique index is what catches duplicates across restarts —
    in-memory checks only cover one process, and restarts are where
    duplicates actually come from.
    """

    def test_the_order_idempotency_index_exists(self):
        indexes = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='paper_orders'")]
        self.assertIn("idx_paper_orders_idem", indexes)

    def test_a_duplicate_order_key_is_refused_by_the_database(self):
        import sqlite3
        runner = self.build()
        self.run_and_save(runner, self.anchors[:15])
        orders = runner.executor.get_orders()
        keyed = next(o for o in orders if o.idempotency_key)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("""
                INSERT INTO paper_orders
                (order_id, session_id, account_id, instrument_id, side, quantity,
                 state, idempotency_key)
                VALUES ('different-id',?,?,?,?,?,?,?)
            """, (keyed.session_id, keyed.account_id, keyed.instrument_id,
                  keyed.side.value, keyed.quantity, keyed.state.value,
                  keyed.idempotency_key))


class TestCheckpointAndRecovery(RecoveryTestCase):
    """Spec §78, §79, §80 — the state must survive the process."""

    def test_a_checkpoint_is_written(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20], checkpoint_every=10)
        checkpoint = self.repository.latest_checkpoint(runner.session.session_id)
        self.assertIsNotNone(checkpoint)
        self.assertGreater(checkpoint["ticks_processed"], 0)

    def test_recovery_from_a_checkpoint_restores_cash_and_positions(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:25], checkpoint_every=25)

        restored, restored_to, method = self.repository.restore_ledger(
            runner.session.session_id, self.account.initial_capital)

        self.assertEqual(method, "checkpoint+replay")
        self.assertIsNotNone(restored_to)
        self.assertAlmostEqual(restored.cash, runner.ledger.cash, places=6)
        self.assertEqual(
            {p.instrument_id: round(p.quantity, 6)
             for p in restored.open_positions()},
            {p.instrument_id: round(p.quantity, 6)
             for p in runner.ledger.open_positions()})

    def test_recovery_without_a_checkpoint_replays_every_fill(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:20])       # no checkpoints

        restored, restored_to, method = self.repository.restore_ledger(
            runner.session.session_id, self.account.initial_capital)

        self.assertEqual(method, "full_replay")
        self.assertIsNone(restored_to)
        self.assertAlmostEqual(restored.cash, runner.ledger.cash, places=6)

    def test_recovery_replays_only_fills_after_the_checkpoint(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:15], checkpoint_every=10)
        # More ticks after the checkpoint.
        self.run_and_save(runner, self.anchors[15:25])

        restored, _, _ = self.repository.restore_ledger(
            runner.session.session_id, self.account.initial_capital)
        self.assertAlmostEqual(restored.cash, runner.ledger.cash, places=6)

    def test_a_recovered_session_continues_consistently(self):
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)

        loaded_session = self.repository.get_session(original.session.session_id)
        resumed = self.build(session=loaded_session)
        resumed.prepare()
        resumed.restore_state(self.repository, at=self.anchors[19])

        result = resumed.tick(self.anchors[20])
        self.assertIsNotNone(result.snapshot)
        self.assertTrue(result.reconciliation.is_clean,
                        [d.detail for d in result.reconciliation.discrepancies])

    def test_restore_state_reports_what_it_recovered(self):
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)

        resumed = self.build(
            session=self.repository.get_session(original.session.session_id))
        resumed.prepare()
        summary = resumed.restore_state(self.repository, at=self.anchors[19])

        self.assertIn(summary["method"], ("checkpoint+replay", "full_replay"))
        self.assertEqual(summary["fills"], len(original.fills))

    def test_recovery_restores_the_fill_history_for_reconciliation(self):
        """
        Without the fill history, reconciliation would compare a fully
        restored ledger against no fills and report the whole book as
        corruption — a false alarm after every restart.
        """
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)

        resumed = self.build(
            session=self.repository.get_session(original.session.session_id))
        resumed.prepare()
        resumed.restore_state(self.repository, at=self.anchors[19])
        self.assertEqual(len(resumed.fills), len(original.fills))

    def test_a_recovered_session_matches_the_original_financially(self):
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)

        resumed = self.build(
            session=self.repository.get_session(original.session.session_id))
        resumed.prepare()
        resumed.restore_state(self.repository, at=self.anchors[19])

        self.assertAlmostEqual(resumed.ledger.cash, original.ledger.cash, places=6)
        self.assertAlmostEqual(resumed.ledger.realized_pnl,
                               original.ledger.realized_pnl, places=6)
        self.assertEqual(
            {p.instrument_id: round(p.quantity, 6)
             for p in resumed.ledger.open_positions()},
            {p.instrument_id: round(p.quantity, 6)
             for p in original.ledger.open_positions()})

    def test_working_orders_are_restored(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:15])
        working = self.repository.working_orders(runner.session.session_id)
        for order in working:
            self.assertTrue(order.state.is_working)

    def test_recovery_does_not_blindly_generate_orders(self):
        """Spec §79 — restore, verify, then decide; never trade on restore."""
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)
        orders_before = len(self.repository.orders_for(
            original.session.session_id))

        resumed = self.build(
            session=self.repository.get_session(original.session.session_id))
        resumed.prepare()
        resumed.restore_state(self.repository, at=self.anchors[19])

        # Restoring is not trading: no new order was created by it.
        self.assertEqual(
            len(self.repository.orders_for(original.session.session_id)),
            orders_before)

    def test_the_closed_trade_list_is_not_repopulated_by_a_checkpoint(self):
        """
        A known and deliberate limitation: a checkpoint stores the
        FINANCIAL state (cash, positions, realized P&L), not the list of
        closed round-trips. Those stay derivable from the persisted
        fills, and realized P&L — the number that matters — is restored
        exactly. Recorded as a test so the gap is visible rather than
        discovered later.
        """
        original = self.build()
        self.run_and_save(original, self.anchors[:20], checkpoint_every=20)

        restored, _, method = self.repository.restore_ledger(
            original.session.session_id, self.account.initial_capital)
        self.assertEqual(method, "checkpoint+replay")
        self.assertAlmostEqual(restored.realized_pnl,
                               original.ledger.realized_pnl, places=6)
        self.assertLessEqual(len(restored.trades), len(original.ledger.trades))


class TestExport(RecoveryTestCase):
    """Spec §64 — an export must carry its own interpretation."""

    def test_an_export_contains_every_record_type(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:15])
        payload = self.repository.export_session(runner.session.session_id)

        for key in ("session", "orders", "fills", "snapshots", "positions",
                    "events", "reconciliations", "latency"):
            self.assertIn(key, payload)

    def test_an_export_carries_the_configuration_and_versions(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:5])
        payload = self.repository.export_session(runner.session.session_id)
        self.assertTrue(payload["session"]["config_fingerprint"])
        self.assertTrue(payload["session"]["code_version"])
        self.assertTrue(payload["session"]["config"])

    def test_an_export_is_labelled_paper(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:5])
        payload = self.repository.export_session(runner.session.session_id)
        self.assertTrue(payload["is_paper"])
        self.assertEqual(payload["venue"], "paper")

    def test_an_export_serialises(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:10])
        payload = self.repository.export_session(runner.session.session_id)
        encoded = json.dumps(payload, default=str)
        self.assertGreater(len(encoded), 100)


class TestListing(RecoveryTestCase):
    def test_sessions_are_listable(self):
        runner = self.build()
        self.run_and_save(runner, self.anchors[:3])
        rows = self.repository.list_sessions()
        self.assertIn(runner.session.session_id, [r["session_id"] for r in rows])

    def test_accounts_are_listable(self):
        rows = self.repository.list_accounts()
        self.assertIn(self.account.account_id, [r["account_id"] for r in rows])

    def test_an_unknown_session_returns_none(self):
        self.assertIsNone(self.repository.get_session("nope"))


if __name__ == "__main__":
    unittest.main()
