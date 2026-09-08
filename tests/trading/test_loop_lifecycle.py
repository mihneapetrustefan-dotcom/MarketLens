"""
tests/trading/test_loop_lifecycle.py
------------------------------------------
Phase 25 — the loop, from mode to lineage.

WHAT THESE TESTS ARE ABOUT
------------------------------
The stages in order, the arithmetic between them, and the two
properties that make a scheduled loop safe: it may be run twice, and it
may be interrupted.

Nothing here mocks the risk engine, the validator or the state machine.
Only the VENUE is a double, and every assertion about an order is an
assertion about what Phase 14 actually did with it.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.trading_loop_models import (
    ActualPosition, BlockReason, CycleStatus, EligibilityCode, LoopHealth,
    LoopStage, PositionDelta, PositionOrigin, StageOutcome, TargetPosition,
    TradingMode, TradingModeRefused, cycle_anchor, cycle_id_for,
    configuration_fingerprint,
)
from src.domain.trading_loop_models import ConfigurationChanged
from src.trading.eligibility import (
    EligibilityContext, EligibilityGate, EligibilityPolicy, summarize,
)
from src.trading.mode import TradingModeStore
from src.trading.repository import TradingLoopRepository
from src.trading.targets import compute_deltas, pending_quantities
from tests.trading.helpers import (
    NOW, a_live_signal, build_loop, enable_paper, make_connection,
    store_signals, universe,
)


# ======================================================================
# The anchor and idempotent identity (§10)
# ======================================================================

class TestTheCycleAnchor(unittest.TestCase):

    def test_the_anchor_floors_to_the_cycle_boundary(self):
        moment = datetime(2026, 9, 7, 15, 7, 42, tzinfo=timezone.utc)
        self.assertEqual(cycle_anchor(moment, 900),
                         datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc))

    def test_two_moments_in_one_window_share_an_anchor(self):
        """
        The whole idempotency story in one assertion.

        Phase 11 derives `decision_id` from `as_of`. A wall-clock anchor
        would mint a new decision, a new intent and a new order on every
        invocation, so a retried scheduled job would double the
        position.
        """
        first = datetime(2026, 9, 7, 15, 0, 1, tzinfo=timezone.utc)
        second = datetime(2026, 9, 7, 15, 14, 59, tzinfo=timezone.utc)
        self.assertEqual(cycle_anchor(first, 900), cycle_anchor(second, 900))
        self.assertEqual(cycle_id_for("s", cycle_anchor(first, 900)),
                         cycle_id_for("s", cycle_anchor(second, 900)))

    def test_a_naive_datetime_is_refused(self):
        with self.assertRaises(ValueError):
            cycle_anchor(datetime(2026, 9, 7, 15, 0))


# ======================================================================
# Trading mode (§9, §26)
# ======================================================================

class TestTradingMode(unittest.TestCase):

    def setUp(self):
        self.conn = make_connection()
        self.store = TradingModeStore(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_an_unconfigured_database_is_off(self):
        resolution = self.store.resolve(NOW)
        self.assertIs(resolution.mode, TradingMode.OFF)
        self.assertFalse(resolution.may_trade)

    def test_paper_can_be_recorded_and_resolves(self):
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.assertTrue(self.store.resolve(NOW).may_trade)

    def test_live_cannot_be_recorded(self):
        with self.assertRaises(TradingModeRefused):
            self.store.set_mode(TradingMode.LIVE, actor="a", reason="r", at=NOW)

    def test_a_live_value_that_arrived_another_way_resolves_to_off(self):
        """
        A restored backup or a manual UPDATE. Refusing only at the write
        would leave this row acted on.
        """
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.conn.execute("UPDATE trading_mode SET mode = 'live'")
        self.conn.commit()
        resolution = self.store.resolve(NOW)
        self.assertIs(resolution.mode, TradingMode.OFF)
        self.assertIn("live trading is blocked", resolution.reason)

    def test_a_misspelled_mode_resolves_to_off_with_its_own_reason(self):
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.conn.execute("UPDATE trading_mode SET mode = 'papper'")
        self.conn.commit()
        resolution = self.store.resolve(NOW)
        self.assertIs(resolution.mode, TradingMode.OFF)
        self.assertIn("not a trading mode", resolution.reason)

    def test_a_blank_mode_resolves_to_off(self):
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.conn.execute("UPDATE trading_mode SET mode = ''")
        self.conn.commit()
        self.assertIs(self.store.resolve(NOW).mode, TradingMode.OFF)

    def test_the_kill_switch_overrides_a_permitted_mode(self):
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.store.activate_kill_switch(actor="a", reason="drawdown", at=NOW)
        resolution = self.store.resolve(NOW)
        self.assertFalse(resolution.may_trade)
        self.assertIn("kill switch", resolution.reason)

    def test_releasing_the_switch_restores_the_configured_mode(self):
        """An operator should not have to set the mode again."""
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.store.activate_kill_switch(actor="a", reason="x", at=NOW)
        self.store.release_kill_switch(actor="a", reason="resolved", at=NOW)
        self.assertTrue(self.store.resolve(NOW).may_trade)

    def test_the_kill_switch_is_durable_across_objects(self):
        """
        The reason this module exists. Phase 14's switch lived on an
        object every script constructed fresh, and `execution_controls`
        had no writer -- so stopping trading stopped it until the next
        process started.
        """
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r", at=NOW)
        self.store.activate_kill_switch(actor="a", reason="x", at=NOW)
        fresh = TradingModeStore(self.conn)
        self.assertTrue(fresh.kill_switch()[0])

    def test_every_change_appends_history(self):
        self.store.set_mode(TradingMode.PAPER, actor="a", reason="r1", at=NOW)
        self.store.activate_kill_switch(actor="b", reason="r2", at=NOW)
        rows = list(self.conn.execute(
            "SELECT actor, reason FROM trading_mode_history ORDER BY rowid"))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[0] for r in rows], ["a", "b"])

    def test_a_change_without_an_actor_or_reason_is_refused(self):
        for kwargs in ({"actor": "", "reason": "r"}, {"actor": "a", "reason": ""}):
            with self.assertRaises(ValueError):
                self.store.set_mode(TradingMode.PAPER, at=NOW, **kwargs)


# ======================================================================
# Eligibility (§14)
# ======================================================================

class TestEligibility(unittest.TestCase):

    def setUp(self):
        self.gate = EligibilityGate(
            EligibilityPolicy(allow_experimental_models=True))
        self.context = EligibilityContext(
            as_of=NOW, prices={"i-aapl": 100.0}, price_ages_days={"i-aapl": 0.5})

    def verdict(self, signal):
        return self.gate.evaluate(signal, "cyc-1", self.context)

    def test_a_live_signal_is_eligible(self):
        self.assertIs(self.verdict(a_live_signal()).code,
                      EligibilityCode.ELIGIBLE)

    def test_a_suppressed_signal_names_its_suppression(self):
        verdict = self.verdict(a_live_signal(suppressed=True))
        self.assertIs(verdict.code, EligibilityCode.SUPPRESSED)
        self.assertIn("confidence", verdict.detail)

    def test_an_expired_signal_is_refused(self):
        signal = a_live_signal(cutoff=NOW - timedelta(days=40), valid_days=1)
        self.assertIn(self.verdict(signal).code,
                      (EligibilityCode.EXPIRED, EligibilityCode.STALE))

    def test_stale_information_is_distinct_from_expiry(self):
        """
        Two different clocks. `valid_until` is the strategy's claim
        about how long its view holds; the age policy is the operator's
        claim about how old a view may be before it is re-derived.
        """
        signal = a_live_signal(cutoff=NOW - timedelta(hours=100),
                               valid_days=30)
        verdict = self.verdict(signal)
        self.assertIs(verdict.code, EligibilityCode.STALE)
        self.assertIn("48h policy", verdict.detail)

    def test_a_signal_with_no_price_cannot_trade(self):
        self.context.prices = {}
        self.assertIs(self.verdict(a_live_signal()).code,
                      EligibilityCode.NO_PRICE)

    def test_a_stale_price_is_named_separately_from_a_missing_one(self):
        self.context.price_ages_days = {"i-aapl": 40.0}
        self.assertIs(self.verdict(a_live_signal()).code,
                      EligibilityCode.STALE_PRICE)

    def test_an_unpromoted_model_is_refused_unless_the_session_says_experimental(self):
        strict = EligibilityGate(EligibilityPolicy(
            allow_experimental_models=False))
        verdict = strict.evaluate(a_live_signal(), "cyc-1", self.context)
        self.assertIs(verdict.code, EligibilityCode.MODEL_NOT_DEPLOYABLE)
        self.assertIn("did not declare itself experimental", verdict.detail)

    def test_experimental_is_recorded_on_the_row_either_way(self):
        """§37: paper trading is not a loophole; it is a label."""
        self.assertTrue(self.verdict(a_live_signal()).experimental)

    def test_an_order_working_the_other_way_blocks_the_signal(self):
        self.context.open_order_quantity = {"i-aapl": -50.0}
        self.assertIs(self.verdict(a_live_signal()).code,
                      EligibilityCode.CONFLICTING_OPEN_ORDER)

    def test_a_paused_instrument_is_refused(self):
        gate = EligibilityGate(EligibilityPolicy(
            allow_experimental_models=True,
            paused_instruments={"i-aapl"}))
        self.assertIs(gate.evaluate(a_live_signal(), "c", self.context).code,
                      EligibilityCode.INSTRUMENT_PAUSED)

    def test_every_signal_gets_exactly_one_verdict(self):
        """§14: no signal is silently discarded."""
        signals = [a_live_signal(signal_id="a"),
                   a_live_signal(signal_id="b", suppressed=True),
                   a_live_signal(signal_id="c", cutoff=NOW - timedelta(days=40))]
        verdicts = self.gate.evaluate_all(signals, "cyc-1", self.context)
        self.assertEqual(len(verdicts), 3)
        self.assertEqual({v.signal_id for v in verdicts}, {"a", "b", "c"})

    def test_the_summary_counts_what_it_saw(self):
        signals = [a_live_signal(signal_id="a"),
                   a_live_signal(signal_id="b", suppressed=True)]
        summary = summarize(self.gate.evaluate_all(signals, "c", self.context))
        self.assertEqual(summary["seen"], 2)
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["rate"], 0.5)

    def test_checks_performed_is_recorded(self):
        """
        A verdict that ran two checks and one that ran twelve both read
        'eligible'. The difference is how much the verdict is worth.
        """
        self.assertGreater(self.verdict(a_live_signal()).checks_performed, 8)


# ======================================================================
# Target vs actual (§7, §16)
# ======================================================================

def a_target(quantity, instrument="i-aapl"):
    return TargetPosition(cycle_id="c", instrument_id=instrument,
                          target_quantity=quantity, reference_price=100.0,
                          signal_id="s", decision_id="d", decided_at=NOW)


def an_actual(quantity, instrument="i-aapl",
              origin=PositionOrigin.BROKER_RECONCILED):
    return ActualPosition(cycle_id="c", instrument_id=instrument,
                          quantity=quantity, origin=origin, observed_at=NOW)


class TestPositionArithmetic(unittest.TestCase):

    def test_the_worked_example_from_the_spec(self):
        """§16: TARGET +100, BROKER +40, and the system sees +60 to do."""
        delta = compute_deltas([a_target(100.0)], [an_actual(40.0)]).deltas[0]
        self.assertEqual(delta.outstanding, 60.0)
        self.assertFalse(delta.is_satisfied)
        self.assertEqual(delta.action, "increase")

    def test_a_met_target_is_a_noop(self):
        delta = compute_deltas([a_target(100.0)], [an_actual(100.0)]).deltas[0]
        self.assertTrue(delta.is_noop)
        self.assertEqual(delta.action, "noop")
        self.assertIsNone(delta.side)

    def test_opening_closing_reducing_and_reversing_are_named(self):
        cases = [(25.0, 0.0, "open"), (0.0, 100.0, "close"),
                 (60.0, 100.0, "reduce"), (-60.0, 100.0, "reverse")]
        for target, held, expected in cases:
            with self.subTest(target=target, held=held):
                delta = compute_deltas([a_target(target)],
                                       [an_actual(held)]).deltas[0]
                self.assertEqual(delta.action, expected)

    def test_no_target_is_not_the_same_as_a_zero_target(self):
        """'We have no opinion' and 'we want nothing' are different."""
        delta = PositionDelta(instrument_id="i", target_quantity=None,
                              actual_quantity=10.0)
        self.assertIsNone(delta.outstanding)
        self.assertEqual(delta.action, "no_target")

    def test_pending_orders_are_subtracted(self):
        """
        Without this a loop ticking every fifteen minutes re-proposes
        the same trade until the account holds four times the target.
        """
        result = compute_deltas([a_target(100.0)], [an_actual(0.0)],
                                pending={"i-aapl": 100.0})
        self.assertTrue(result.deltas[0].is_satisfied)
        self.assertEqual(result.quantities, {})

    def test_a_target_with_no_quantity_is_rejected_not_invented(self):
        target = a_target(None)
        result = compute_deltas([target], [])
        self.assertEqual(result.quantities, {})
        self.assertEqual(result.rejected[0].reason, "no_target_quantity")

    def test_an_unreconciled_position_does_not_trade(self):
        """§25: an unknown portfolio state fails closed."""
        result = compute_deltas(
            [a_target(100.0)],
            [an_actual(40.0, origin=PositionOrigin.LOCAL_PROJECTION)])
        self.assertEqual(result.quantities, {})
        self.assertEqual(result.rejected[0].reason, "position_not_reconciled")

    def test_a_gap_below_the_venue_minimum_is_no_trade(self):
        """
        The bug this found: a 10% weight target against a held position
        produced a SELL of 0.1255 shares, which the validator refused
        three layers away for QUANTITY_INCREMENT.
        """
        class Mapping:
            minimum_quantity = 1.0
            quantity_increment = 1.0

            def normalize_quantity(self, quantity):
                return quantity, None

        result = compute_deltas([a_target(100.1255)], [an_actual(100.0)],
                                mappings={"i-aapl": Mapping()})
        self.assertEqual(result.quantities, {})
        self.assertEqual(result.deltas[0].action, "below_minimum")
        self.assertEqual(result.rejected[0].reason, "below_broker_minimum")

    def test_pending_quantities_ignores_terminal_orders(self):
        class Order:
            def __init__(self, state, quantity, filled):
                self.instrument_id = "i-aapl"
                self.state = state
                self.quantity = quantity
                self.filled_quantity = filled
                self.side = "buy"

        class State:
            def __init__(self, terminal):
                self.is_terminal = terminal

        pending = pending_quantities([Order(State(True), 100.0, 0.0)])
        self.assertEqual(pending, {})


# ======================================================================
# The cycle (§10, §13)
# ======================================================================

class TestTheCycle(unittest.TestCase):

    def setUp(self):
        self.conn = make_connection()
        universe(self.conn)
        store_signals(self.conn, [a_live_signal()])
        enable_paper(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_full_cycle_reaches_every_stage(self):
        result = build_loop(self.conn).run_cycle(NOW)
        self.assertIs(result.status, CycleStatus.COMPLETED)
        reached = {s.stage for s in result.stages}
        for stage in (LoopStage.MODE, LoopStage.SIGNALS, LoopStage.ELIGIBILITY,
                      LoopStage.PORTFOLIO, LoopStage.RISK, LoopStage.TARGETS,
                      LoopStage.INTENTS, LoopStage.SUBMISSION,
                      LoopStage.BROKER_POLL, LoopStage.FILLS,
                      LoopStage.POSITIONS, LoopStage.RECONCILIATION,
                      LoopStage.PNL, LoopStage.OUTCOMES, LoopStage.PERSIST):
            self.assertIn(stage, reached, f"{stage.value} never ran")

    def test_a_cycle_places_an_order_through_the_real_stack(self):
        result = build_loop(self.conn).run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 1)
        order = self.conn.execute(
            "SELECT signal_id, decision_id, environment FROM execution_orders"
        ).fetchone()
        self.assertEqual(order[0], "sig-live-1")
        self.assertTrue(order[1])
        self.assertEqual(order[2], "paper")

    def test_running_the_same_anchor_twice_creates_no_second_order(self):
        """§10: a repeated scheduled job must not double the position."""
        loop = build_loop(self.conn)
        loop.run_cycle(NOW)
        second = loop.run_cycle(NOW + timedelta(seconds=30))
        self.assertIs(second.status, CycleStatus.ABANDONED)
        self.assertEqual(second.blocks[0].reason,
                         BlockReason.CYCLE_ALREADY_RUNNING)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 1)

    def test_a_fresh_process_does_not_resubmit_an_existing_order(self):
        """
        The restore path. A new process has an empty idempotency index,
        and without `ExecutionRepository.restore` the first submission
        of a re-run mints a duplicate carrying a key the database
        already holds.
        """
        build_loop(self.conn).run_cycle(NOW)
        rebuilt = build_loop(self.conn)
        rebuilt.run_cycle(NOW + timedelta(minutes=15))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM execution_orders")
            .fetchone()[0], 1)

    def test_a_dry_run_validates_and_sends_nothing(self):
        result = build_loop(self.conn, dry_run=True).run_cycle(NOW)
        self.assertEqual(result.orders_submitted, 0)
        stage = result.stage(LoopStage.SUBMISSION)
        self.assertIs(stage.outcome, StageOutcome.SKIPPED)
        self.assertIn("not sent", stage.detail)

    def test_a_stale_claim_can_be_reclaimed(self):
        repository = TradingLoopRepository(self.conn)
        repository.open_cycle("cyc-x", "sess", NOW, NOW)
        repository.claim("cyc-x", "dead-worker", NOW)
        reclaimed = repository.reclaim_stale(NOW + timedelta(hours=2))
        self.assertEqual(reclaimed, ["cyc-x"])
        self.assertTrue(repository.claim("cyc-x", "new-worker",
                                         NOW + timedelta(hours=2)))

    def test_a_claim_is_atomic(self):
        repository = TradingLoopRepository(self.conn)
        repository.open_cycle("cyc-y", "sess", NOW, NOW)
        self.assertTrue(repository.claim("cyc-y", "first", NOW))
        self.assertFalse(repository.claim("cyc-y", "second", NOW))

    def test_the_stage_record_survives_a_failure(self):
        """A stage that crashed must leave the reason behind."""
        loop = build_loop(self.conn)

        def explode(*args, **kwargs):
            raise RuntimeError("the gateway fell over")

        loop.stack.gateway.get_positions = explode
        result = loop.run_cycle(NOW)
        stages = {s.stage: s for s in result.stages}
        failed = [s for s in stages.values()
                  if s.outcome is StageOutcome.FAILED]
        self.assertTrue(failed)
        self.assertIn("the gateway fell over", failed[0].detail)

    def test_timestamps_are_in_order(self):
        """§24: correct chronology is mandatory for later research."""
        result = build_loop(self.conn).run_cycle(NOW)
        self.assertEqual(result.timestamps.out_of_order(), [])

    def test_a_configuration_change_refuses_to_resume_a_session(self):
        """§29: results across a silent configuration change are ambiguous."""
        build_loop(self.conn, session_id="sess-cfg").run_cycle(NOW)
        with self.assertRaises(ConfigurationChanged):
            build_loop(self.conn, session_id="sess-cfg",
                       cycle_seconds=1800).run_cycle(NOW + timedelta(hours=1))

    def test_the_fingerprint_ignores_whitespace_but_not_values(self):
        base = {"a": "one two", "b": 3}
        self.assertEqual(configuration_fingerprint(base),
                         configuration_fingerprint({"a": "one  two ", "b": 3}))
        self.assertNotEqual(configuration_fingerprint(base),
                            configuration_fingerprint({"a": "one two", "b": 4}))


if __name__ == "__main__":
    unittest.main()
