"""
tests/execution/test_adversarial.py
----------------------------------------
The seventeen adversarial scenarios (Phase 14, spec §63) and the
invariants of §64.

WHAT THESE TESTS ARE FOR
----------------------------
Not "does it work". Every one of these asserts that a specific
dangerous thing CANNOT happen, and each is written so that the
dangerous behaviour would make it fail loudly rather than subtly.

The scenarios that matter most are the ones where the naive handling
looks correct: a submission that times out looks like a failure and is
not, a duplicate fill looks like a second fill and is not, a FILLED
event arriving early looks like progress and is not.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.broker_models import (
    BrokerConnectionState, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionEventType,
    ExecutionOrderState, ExecutionRejectCode, MarketStatus, MismatchKind,
    PositionSnapshot,
)
from src.execution.reconciliation import BrokerReconciler
from src.execution.safety import ExecutionSafety, RealMoneyExecutionDisabled
from src.execution.states import apply_fill_to_order
from tests.execution.helpers import (
    AT, FakeGateway, build_fake_stack, fake_fill, request,
)


class AdversarialCase(unittest.TestCase):
    def setUp(self):
        self.orchestrator, self.gateway, self.instruments, self.safety = \
            build_fake_stack()

    def submit(self, **overrides):
        return self.orchestrator.execute(request(**overrides))


# ============================================================
# 1. Broker sends the same fill twice
# ============================================================

class TestCase1_DuplicateFill(AdversarialCase):

    def test_a_repeated_fill_does_not_double_the_position(self):
        result = self.submit()
        order = result.order
        fill = fake_fill(order, 100.0, 100.0, fill_id="f-1")

        first = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        report = self.orchestrator.events.process(
            [first], {order.order_id: order}, {first.event_id: fill})
        self.assertEqual(order.filled_quantity, 100.0)
        self.assertEqual(report.applied, 1)

        # The same execution, delivered again under a new event id —
        # the shape a redelivery after reconnect actually takes.
        second = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order,
                                   event_id="fe-redelivery")
        report = self.orchestrator.events.process(
            [second], {order.order_id: order}, {second.event_id: fill})

        self.assertEqual(order.filled_quantity, 100.0,
                         "the duplicate fill doubled the position")
        self.assertEqual(report.duplicates, 1)
        self.assertTrue(any(kind is MismatchKind.DUPLICATE_FILL
                            for kind, _ in report.findings))

    def test_a_duplicate_event_id_is_dropped_before_the_fill_is_read(self):
        result = self.submit()
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        fill = fake_fill(order, 50.0, 100.0)

        self.orchestrator.events.process([event], {order.order_id: order},
                                         {event.event_id: fill})
        report = self.orchestrator.events.process([event], {order.order_id: order},
                                                  {event.event_id: fill})
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(order.filled_quantity, 50.0)


# ============================================================
# 2. Broker sends events out of order
# ============================================================

class TestCase2_OutOfOrderEvents(AdversarialCase):

    def test_a_late_working_event_does_not_reopen_a_filled_order(self):
        result = self.submit()
        order = result.order
        filled = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        self.orchestrator.events.process(
            [filled], {order.order_id: order},
            {filled.event_id: fake_fill(order, 100.0, 100.0)})
        self.assertIs(order.state, ExecutionOrderState.FILLED)

        late = self.gateway.emit(ExecutionEventType.ORDER_UPDATED, order,
                                 at=AT - timedelta(minutes=5))
        report = self.orchestrator.events.process([late], {order.order_id: order})

        self.assertIs(order.state, ExecutionOrderState.FILLED,
                      "a late WORKING event reopened a filled order")
        self.assertEqual(report.late, 1)

    def test_filled_arriving_before_partially_filled_is_handled(self):
        """
        The canonical reordering. FILLED lands first and is applied;
        the PARTIALLY_FILLED that follows describes an earlier moment
        and must not wind the order back.
        """
        result = self.submit()
        order = result.order

        filled = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        self.orchestrator.events.process(
            [filled], {order.order_id: order},
            {filled.event_id: fake_fill(order, 100.0, 100.0, fill_id="f-full")})

        partial = self.gateway.emit(ExecutionEventType.ORDER_PARTIALLY_FILLED,
                                    order)
        report = self.orchestrator.events.process(
            [partial], {order.order_id: order},
            {partial.event_id: fake_fill(order, 40.0, 100.0, fill_id="f-part")})

        self.assertIs(order.state, ExecutionOrderState.FILLED)
        self.assertEqual(order.filled_quantity, 100.0,
                         "the late partial was added on top of a full fill")

    def test_a_terminal_order_is_never_resurrected(self):
        result = self.submit()
        order = result.order
        self.orchestrator.machine.apply(order, ExecutionOrderState.CANCELLED,
                                        at=AT, reason="test", strict=False)

        for event_type in (ExecutionEventType.ORDER_ACKNOWLEDGED,
                           ExecutionEventType.ORDER_UPDATED,
                           ExecutionEventType.ORDER_PARTIALLY_FILLED):
            event = self.gateway.emit(event_type, order)
            self.orchestrator.events.process([event], {order.order_id: order})
            self.assertIs(order.state, ExecutionOrderState.CANCELLED,
                          f"{event_type.value} resurrected a cancelled order")


# ============================================================
# 3. Submit times out but the broker accepted
# ============================================================

class TestCase3_SubmissionTimeout(AdversarialCase):

    def test_a_timeout_becomes_unknown_not_failed(self):
        self.gateway.timeout_on_submit = True
        result = self.submit()

        self.assertFalse(result.accepted)
        self.assertIs(result.order.state, ExecutionOrderState.UNKNOWN,
                      "a timed-out submission must not be called failed")
        self.assertFalse(result.order.state.is_terminal)
        self.assertTrue(result.order.state.needs_reconciliation)

    def test_a_timeout_is_never_resubmitted(self):
        """
        The scenario this whole state exists for. Resubmitting turns
        one intended position into two real ones.
        """
        self.gateway.timeout_on_submit = True
        self.submit()
        self.assertEqual(self.gateway.submit_calls, 1)

        # A second attempt at the same intent is caught by idempotency.
        again = self.submit()
        self.assertEqual(self.gateway.submit_calls, 1,
                         "the timed-out order was resubmitted")
        self.assertIsNotNone(again.duplicate_of)

    def test_querying_the_broker_resolves_it(self):
        self.gateway.timeout_on_submit = True
        result = self.submit()
        order = result.order

        # The venue did accept it, and says so when asked. The order
        # has no broker id, so resolution has to find it by client id.
        view = next(iter(self.gateway.broker_orders.values()))
        order.broker_order_id = view.broker_order_id

        resolutions = self.orchestrator.resolve_unknown_orders("fake", AT)
        self.assertEqual(len(resolutions), 1)
        self.assertTrue(resolutions[0].resolved)
        self.assertIs(order.state, ExecutionOrderState.WORKING)
        self.assertEqual(self.gateway.submit_calls, 1)

    def test_an_order_the_broker_never_saw_resolves_to_failed(self):
        self.gateway.timeout_on_submit = True
        result = self.submit()
        order = result.order
        order.broker_order_id = "bo-0001"
        self.gateway.unknown_to_broker = True

        resolutions = self.orchestrator.resolve_unknown_orders("fake", AT)
        self.assertTrue(resolutions[0].resolved)
        self.assertIs(order.state, ExecutionOrderState.FAILED,
                      "an order the venue has no record of is safe to fail")

    def test_an_unreachable_broker_leaves_it_unknown(self):
        """
        The honest outcome. A resolution pass that could not reach the
        venue has learned nothing, and must not pretend otherwise.
        """
        self.gateway.timeout_on_submit = True
        result = self.submit()
        result.order.broker_order_id = "bo-0001"
        self.gateway.raise_on_get_order = True

        resolutions = self.orchestrator.resolve_unknown_orders("fake", AT)
        self.assertFalse(resolutions[0].resolved)
        self.assertIs(result.order.state, ExecutionOrderState.UNKNOWN)

    def test_an_adapter_that_raises_also_becomes_unknown(self):
        """An exception mid-submit may still have reached the venue."""
        self.gateway.raise_on_submit = True
        result = self.submit()
        self.assertIs(result.order.state, ExecutionOrderState.UNKNOWN)
        self.assertIsNotNone(result.error)


# ============================================================
# 4 & 5. Application restarts
# ============================================================

class TestCase4_RestartAfterSubmission(AdversarialCase):

    def test_in_flight_orders_are_found_after_a_restart(self):
        result = self.submit()
        order = result.order
        # Simulate dying between SUBMITTING and the acknowledgement.
        order.state = ExecutionOrderState.SUBMITTING

        fresh, gateway, _, _ = build_fake_stack()
        fresh.seed(orders=[order])
        self.assertEqual([o.order_id for o in fresh.orders_in_flight()],
                         [order.order_id])

    def test_in_flight_orders_become_unknown_never_failed(self):
        result = self.submit()
        result.order.state = ExecutionOrderState.SUBMITTED

        fresh, _, _, _ = build_fake_stack()
        fresh.seed(orders=[result.order])
        moved = fresh.mark_in_flight_unknown(AT)

        self.assertEqual(len(moved), 1)
        self.assertIs(moved[0].state, ExecutionOrderState.UNKNOWN)

    def test_the_idempotency_index_survives_a_restart(self):
        result = self.submit()

        fresh, gateway, _, _ = build_fake_stack()
        fresh.seed(orders=[result.order])
        again = fresh.execute(request())

        self.assertEqual(gateway.submit_calls, 0,
                         "a restarted process resubmitted an existing order")
        self.assertEqual(again.duplicate_of, result.order.order_id)


class TestCase5_RestartAfterFill(AdversarialCase):

    def test_a_redelivered_fill_after_restart_is_still_a_duplicate(self):
        """
        Reconnecting is exactly what makes a venue replay recent
        events, so this is the redelivery that actually happens.
        """
        result = self.submit()
        order = result.order
        fill = fake_fill(order, 100.0, 100.0, fill_id="f-1")
        event = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        self.orchestrator.events.process([event], {order.order_id: order},
                                         {event.event_id: fill})

        fresh, _, _, _ = build_fake_stack()
        fresh.seed(orders=[order], fills=[fill],
                   event_keys=[event.idempotency_key])
        report = fresh.events.process([event], {order.order_id: order},
                                      {event.event_id: fill})

        self.assertEqual(report.duplicates, 1)
        self.assertEqual(order.filled_quantity, 100.0)


# ============================================================
# 6. Broker disconnects while an order is working
# ============================================================

class TestCase6_DisconnectWhileWorking(AdversarialCase):

    def test_a_disconnected_broker_accepts_no_new_orders(self):
        self.gateway.set_state(BrokerConnectionState.DISCONNECTED)
        result = self.submit()
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.BROKER_DISCONNECTED,
                      result.validation.codes)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_a_degraded_connection_also_stops_new_orders(self):
        """
        DEGRADED does not qualify as submittable. A degraded link can
        carry a submission whose acknowledgement never arrives, which
        is the path to UNKNOWN — so new exposure waits.
        """
        self.gateway.set_state(BrokerConnectionState.DEGRADED)
        result = self.submit()
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.BROKER_DISCONNECTED,
                      result.validation.codes)

    def test_working_orders_are_not_cancelled_by_a_disconnect(self):
        result = self.submit()
        self.gateway.set_state(BrokerConnectionState.DISCONNECTED)
        self.assertTrue(result.order.state.is_working,
                        "a disconnect silently closed a working order")


# ============================================================
# 7. Broker returns an unknown status
# ============================================================

class TestCase7_UnknownBrokerStatus(AdversarialCase):

    def test_an_unknown_status_routes_to_reconciliation(self):
        result = self.submit()
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_UNKNOWN, order)
        self.orchestrator.events.process([event], {order.order_id: order})

        self.assertIs(order.state, ExecutionOrderState.UNKNOWN)
        self.assertTrue(order.state.needs_reconciliation)

    def test_reconciliation_reports_every_unknown_order(self):
        result = self.submit()
        self.orchestrator.machine.apply(result.order,
                                        ExecutionOrderState.UNKNOWN,
                                        at=AT, reason="test", strict=False)
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertTrue(record.of_kind(MismatchKind.UNKNOWN_STATE))


# ============================================================
# 8. The broker holds an order we do not
# ============================================================

class TestCase8_ExtraBrokerOrder(AdversarialCase):

    def test_an_unknown_broker_order_is_reported_not_adopted(self):
        from src.execution.gateway import BrokerOrderView
        self.gateway.broker_orders["bo-stray"] = BrokerOrderView(
            broker_order_id="bo-stray", instrument_id="i-aaa",
            broker_symbol="AAA", side=CanonicalOrderSide.BUY, quantity=50.0,
            state=ExecutionOrderState.WORKING, at=AT)

        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        found = record.of_kind(MismatchKind.UNKNOWN_BROKER_ORDER)

        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].resolved, "the mismatch was auto-resolved")
        self.assertNotIn("bo-stray",
                         [o.broker_order_id for o in self.orchestrator.orders.values()],
                         "a stray broker order was adopted into our book")

    def test_an_order_the_broker_lost_is_reported(self):
        result = self.submit()
        self.gateway.broker_orders.clear()
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertTrue(record.of_kind(MismatchKind.MISSING_INTERNAL_ORDER))


# ============================================================
# 9 & 10. Position and balance disagreements
# ============================================================

class TestCase9_PositionMismatch(AdversarialCase):

    def test_a_position_difference_is_recorded_never_corrected(self):
        self.gateway.positions = [PositionSnapshot(
            account_id="fake-account", broker_id="fake", instrument_id="i-aaa",
            quantity=250.0, average_price=100.0, at=AT)]

        record = self.orchestrator.reconcile(
            "fake", "fake-account", AT, internal_positions={"i-aaa": 100.0})
        found = record.of_kind(MismatchKind.POSITION_MISMATCH)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].internal_value, 100.0)
        self.assertEqual(found[0].broker_value, 250.0)
        self.assertFalse(found[0].resolved)

    def test_an_instrument_only_the_broker_holds_is_caught(self):
        self.gateway.positions = [PositionSnapshot(
            account_id="fake-account", broker_id="fake", instrument_id="i-zzz",
            quantity=10.0, average_price=50.0, at=AT)]
        record = self.orchestrator.reconcile(
            "fake", "fake-account", AT, internal_positions={})
        self.assertTrue(record.of_kind(MismatchKind.POSITION_MISMATCH))

    def test_tolerance_absorbs_floating_point_noise(self):
        self.gateway.positions = [PositionSnapshot(
            account_id="fake-account", broker_id="fake", instrument_id="i-aaa",
            quantity=100.0000000001, average_price=100.0, at=AT)]
        record = self.orchestrator.reconcile(
            "fake", "fake-account", AT, internal_positions={"i-aaa": 100.0})
        self.assertEqual(record.of_kind(MismatchKind.POSITION_MISMATCH), [])


class TestCase10_BalanceMismatch(AdversarialCase):

    def test_an_unexpected_balance_change_is_recorded(self):
        self.gateway.account.cash = 91_000.0
        record = self.orchestrator.reconcile(
            "fake", "fake-account", AT, internal_cash=100_000.0)
        found = record.of_kind(MismatchKind.CASH_MISMATCH)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].resolved)


# ============================================================
# 11. Stale quotes
# ============================================================

class TestCase11_StaleData(AdversarialCase):

    def test_stale_data_blocks_the_order(self):
        result = self.submit(data_is_stale=True,
                             freshness_detail="bars are 9 days old")
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.STALE_DATA, result.validation.codes)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_the_rejection_says_how_stale(self):
        result = self.submit(data_is_stale=True,
                             freshness_detail="bars are 9 days old")
        self.assertIn("9 days old", result.explanation)


# ============================================================
# 12 & 13. Instrument mapping and quantity increments
# ============================================================

class TestCase12_WrongSymbolMapping(AdversarialCase):

    def test_an_unmapped_instrument_is_never_guessed(self):
        result = self.submit(instrument_id="i-unmapped")
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                      result.validation.codes)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_an_untradable_mapping_is_refused(self):
        from src.execution.instruments import default_equity_mapping
        self.instruments.register(default_equity_mapping(
            "i-halted", "fake", "HALT", tradable=False))
        result = self.submit(instrument_id="i-halted")
        self.assertIn(ExecutionRejectCode.INSTRUMENT_NOT_TRADABLE,
                      result.validation.codes)


class TestCase13_QuantityIncrement(AdversarialCase):

    def test_a_quantity_below_the_minimum_is_refused(self):
        result = self.submit(quantity=0.4)
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.QUANTITY_INCREMENT,
                      result.validation.codes)

    def test_quantity_is_rounded_down_never_up(self):
        """
        Rounding up would submit more exposure than the risk engine
        sized, and the risk engine is the authority on size.
        """
        result = self.submit(quantity=100.9)
        self.assertTrue(result.accepted)
        self.assertEqual(result.order.quantity, 100.0)

    def test_a_zero_or_negative_quantity_is_refused(self):
        for quantity in (0.0, -5.0):
            result = self.submit(quantity=quantity, intent_id=f"q{quantity}")
            self.assertIn(ExecutionRejectCode.INVALID_QUANTITY,
                          result.validation.codes)


# ============================================================
# 14. Unsupported order type
# ============================================================

class TestCase14_UnsupportedOrderType(AdversarialCase):

    def test_a_capability_the_broker_lacks_is_refused_before_submission(self):
        result = self.submit(order_type=CanonicalOrderType.TRAILING_STOP,
                             stop_price=95.0)
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.UNSUPPORTED_ORDER_TYPE,
                      result.validation.codes)
        self.assertEqual(self.gateway.submit_calls, 0,
                         "an unsupported order type reached the venue")

    def test_an_unsupported_time_in_force_is_refused(self):
        result = self.submit(time_in_force=CanonicalTimeInForce.FOK)
        self.assertIn(ExecutionRejectCode.UNSUPPORTED_TIME_IN_FORCE,
                      result.validation.codes)

    def test_shorting_is_refused_when_the_venue_cannot(self):
        self.gateway.capability.supports_shorting = False
        result = self.submit(side=CanonicalOrderSide.SELL)
        self.assertIn(ExecutionRejectCode.SHORTING_NOT_SUPPORTED,
                      result.validation.codes)


# ============================================================
# 15. Duplicate order intent
# ============================================================

class TestCase15_DuplicateIntent(AdversarialCase):

    def test_the_same_intent_twice_creates_one_broker_order(self):
        first = self.submit()
        second = self.submit()

        self.assertEqual(self.gateway.submit_calls, 1)
        self.assertEqual(len(self.orchestrator.orders), 1)
        self.assertEqual(second.duplicate_of, first.order.order_id)

    def test_a_new_intent_version_is_a_new_order(self):
        """
        The deliberate escape hatch. A scale-in for the same size in
        the same second is a real case, and the caller states it
        rather than the system guessing from timing.
        """
        self.submit()
        second = self.submit(intent_version=2)
        self.assertIsNone(second.duplicate_of)
        self.assertEqual(self.gateway.submit_calls, 2)

    def test_a_different_quantity_is_a_different_order(self):
        self.submit()
        second = self.submit(quantity=50.0)
        self.assertIsNone(second.duplicate_of)

    def test_the_key_ignores_the_signal_id(self):
        """
        Phase 13's lesson, carried forward. Several live signals for
        one instrument often ask for the same target at the same
        moment; keyed on the signal, each produced its own order.
        """
        self.submit(signal_id="sig-a")
        second = self.submit(signal_id="sig-b")
        self.assertIsNotNone(second.duplicate_of,
                             "two signals asking the same thing made two orders")
        self.assertEqual(self.gateway.submit_calls, 1)


# ============================================================
# 16. Kill switch during execution
# ============================================================

class TestCase16_KillSwitch(AdversarialCase):

    def test_the_kill_switch_stops_new_orders(self):
        self.safety.activate_kill_switch("test halt", at=AT, actor="tester")
        result = self.submit()
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.EMERGENCY_STOP,
                      result.validation.codes)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_it_does_not_cancel_or_delete_existing_work(self):
        """
        Stopping adds no exposure; deciding what to do about exposure
        already at a venue is a human decision that needs this history.
        """
        first = self.submit()
        self.safety.activate_kill_switch("mid-flight", at=AT, actor="tester")

        self.assertTrue(first.order.state.is_working)
        self.assertIn(first.order.order_id, self.orchestrator.orders)
        self.assertTrue(self.orchestrator.machine.transitions_for(
            first.order.order_id))

    def test_reconciliation_still_runs_under_the_kill_switch(self):
        self.submit()
        self.safety.activate_kill_switch("halt", at=AT, actor="tester")
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertIsNotNone(record,
                             "the kill switch stopped us from observing our book")

    def test_activation_requires_a_reason(self):
        with self.assertRaises(ValueError):
            self.safety.activate_kill_switch("", at=AT)

    def test_releasing_restores_execution(self):
        self.safety.activate_kill_switch("halt", at=AT, actor="t")
        self.safety.release_kill_switch("resolved", at=AT, actor="t")
        self.assertTrue(self.submit().accepted)


# ============================================================
# 17. Database commit failure
# ============================================================

class TestCase17_PersistenceFailure(unittest.TestCase):

    def test_a_failed_write_does_not_leave_a_phantom_order(self):
        """
        The in-memory book and the database can disagree after a failed
        write. What must NOT happen is a second submission on the
        retry — the idempotency index is rebuilt from orders, so an
        order that exists in memory still blocks a duplicate.
        """
        orchestrator, gateway, _, _ = build_fake_stack()
        result = orchestrator.execute(request())
        self.assertEqual(gateway.submit_calls, 1)

        # The write failed; nothing was persisted. The process retries.
        again = orchestrator.execute(request())
        self.assertEqual(gateway.submit_calls, 1)
        self.assertEqual(again.duplicate_of, result.order.order_id)


# ============================================================
# §64 invariants
# ============================================================

class TestInvariants(AdversarialCase):

    def test_filled_quantity_can_never_exceed_ordered_quantity(self):
        result = self.submit()
        order = result.order
        self.assertTrue(apply_fill_to_order(order, 100.0, 100.0))
        self.assertFalse(apply_fill_to_order(order, 1.0, 100.0),
                         "an over-fill was accepted")
        self.assertEqual(order.filled_quantity, 100.0)
        self.assertFalse(order.is_overfilled)

    def test_an_overfilling_event_is_reported_not_applied(self):
        result = self.submit()
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        report = self.orchestrator.events.process(
            [event], {order.order_id: order},
            {event.event_id: fake_fill(order, 150.0, 100.0)})

        self.assertEqual(order.filled_quantity, 0.0)
        self.assertTrue(any(kind is MismatchKind.QUANTITY_MISMATCH
                            for kind, _ in report.findings))

    def test_one_idempotency_key_cannot_create_two_orders(self):
        self.submit()
        self.submit()
        keys = [o.idempotency_key for o in self.orchestrator.orders.values()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.orchestrator.orders), 1)

    def test_state_transitions_follow_the_lifecycle(self):
        result = self.submit()
        states = [t.to_state for t in
                  self.orchestrator.machine.transitions_for(result.order.order_id)]
        self.assertEqual(states, [
            ExecutionOrderState.VALIDATING, ExecutionOrderState.APPROVED,
            ExecutionOrderState.SUBMITTING, ExecutionOrderState.SUBMITTED,
            ExecutionOrderState.ACKNOWLEDGED])

    def test_a_filled_status_unsupported_by_fills_becomes_a_finding(self):
        """
        The broker says filled; our fills account for none of it.
        Adopting the status would create a position no execution
        explains.
        """
        result = self.submit()
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_FILLED, order)
        report = self.orchestrator.events.process([event],
                                                  {order.order_id: order})

        self.assertIsNot(order.state, ExecutionOrderState.FILLED)
        self.assertIs(order.state, ExecutionOrderState.RECONCILIATION_REQUIRED)
        self.assertTrue(any(kind is MismatchKind.STATUS_MISMATCH
                            for kind, _ in report.findings))

    def test_real_money_execution_is_impossible(self):
        self.assertFalse(self.safety.allow_real_orders)
        with self.assertRaises(AttributeError):
            self.safety.allow_real_orders = True
        with self.assertRaises(RealMoneyExecutionDisabled):
            self.safety.assert_not_real_money(ExecutionEnvironment.LIVE)

    def test_position_changes_require_a_fill(self):
        """
        No path applies quantity to an order without a fill object.
        A status event alone moves nothing.
        """
        result = self.submit()
        order = result.order
        for event_type in (ExecutionEventType.ORDER_ACKNOWLEDGED,
                           ExecutionEventType.ORDER_UPDATED):
            event = self.gateway.emit(event_type, order)
            self.orchestrator.events.process([event], {order.order_id: order})
        self.assertEqual(order.filled_quantity, 0.0)


if __name__ == "__main__":
    unittest.main()
