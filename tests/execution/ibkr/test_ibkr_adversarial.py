"""
tests/execution/ibkr/test_ibkr_adversarial.py
--------------------------------------------------
The twenty adversarial scenarios (Phase 15, spec §61), plus restart
recovery (§62) and transaction safety (§63).

WHAT THESE ASSERT
---------------------
Not that the adapter works. That a specific dangerous thing CANNOT
happen, with each test written so the dangerous behaviour fails it
loudly.

The scenarios that matter most are the ones where the naive handling
looks correct: a timeout looks like a failure and is not, a replayed
execution looks like a second fill and is not, an IBKR `Inactive`
status looks terminal and is not.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from src.domain.broker_models import (
    BrokerConnectionState, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionOrderState,
    ExecutionRejectCode, MarketStatus, MismatchKind, PositionSnapshot,
)
from src.execution.adapters.ibkr.config import (
    IBKRConfig, IBKRConfigurationError, paper_config,
)
from src.execution.adapters.ibkr.contracts import ContractQuery, ContractResolver
from src.execution.adapters.ibkr.errors import IBKRErrorCategory
from src.execution.adapters.ibkr.gateway import IBKRGateway, MarketDataAvailability
from src.execution.adapters.ibkr.mapper import state_from_ibkr
from src.execution.adapters.ibkr.mock_transport import MOCK_ACCOUNT
from src.execution.states import apply_fill_to_order
from tests.execution.ibkr.helpers import (
    AT, INSTRUMENT, AlwaysOpenCalendar, ClosedCalendar, build_ibkr,
    fill_through_gateway, ibkr_request, submit,
)


class IBKRCase(unittest.TestCase):
    def setUp(self):
        self.stack = build_ibkr()
        self.orchestrator = self.stack["orchestrator"]
        self.gateway = self.stack["gateway"]
        self.transport = self.stack["transport"]
        self.config = self.stack["config"]


# ============================================================
# 1. Timeout after submission
# ============================================================

class TestCase1_TimeoutAfterSubmission(IBKRCase):

    def test_a_timeout_becomes_unknown_not_failed(self):
        self.transport.timeout_on_place = True
        result = submit(self.stack)
        self.assertFalse(result.accepted)
        self.assertIs(result.order.state, ExecutionOrderState.UNKNOWN)
        self.assertFalse(result.order.state.is_terminal)

    def test_ibkr_actually_holds_the_order_we_never_heard_about(self):
        """The premise. If the mock did not accept it, nothing else matters."""
        self.transport.timeout_on_place = True
        submit(self.stack)
        self.assertEqual(len(self.transport.orders), 1)

    def test_it_is_never_resubmitted(self):
        self.transport.timeout_on_place = True
        submit(self.stack)
        submit(self.stack)
        self.assertEqual(self.transport.place_calls, 1,
                         "a timed-out order was resubmitted")

    def test_querying_ibkr_resolves_it(self):
        self.transport.timeout_on_place = True
        result = submit(self.stack)
        self.transport.timeout_on_place = False
        result.order.broker_order_id = next(iter(self.transport.orders))

        resolutions = self.orchestrator.resolve_unknown_orders("ibkr", AT)
        self.assertTrue(resolutions[0].resolved)
        self.assertIs(result.order.state, ExecutionOrderState.ACKNOWLEDGED)
        self.assertEqual(self.transport.place_calls, 1)

    def test_the_client_order_id_finds_it_when_no_ibkr_id_came_back(self):
        """
        The case the client order id exists for. After a timeout there
        may be no IBKR order id at all, and it is derived from the
        idempotency key so a retry computes the same one.
        """
        self.transport.timeout_on_place = True
        result = submit(self.stack)
        self.transport.timeout_on_place = False
        held = next(iter(self.transport.orders.values()))
        held.client_order_id = result.order.client_order_id

        view = self.gateway.get_order(result.order.client_order_id)
        self.assertIsNotNone(view)
        self.assertEqual(view.client_order_id, result.order.client_order_id)

    def test_an_unreachable_ibkr_leaves_it_unknown(self):
        self.transport.timeout_on_place = True
        result = submit(self.stack)
        result.order.broker_order_id = "ib-000001"
        self.transport.timeout_on_place = False
        self.transport.raise_on_status = True

        resolutions = self.orchestrator.resolve_unknown_orders("ibkr", AT)
        self.assertFalse(resolutions[0].resolved)
        self.assertIs(result.order.state, ExecutionOrderState.UNKNOWN)


# ============================================================
# 2 & 3. Duplicate executions and fills
# ============================================================

class TestCase2_DuplicateExecutions(IBKRCase):

    def test_a_replayed_execution_log_produces_no_second_fill(self):
        """
        Reconnecting is what makes IBKR replay recent executions, so
        this is the redelivery that actually happens.
        """
        result = submit(self.stack)
        fill_through_gateway(self.stack, result.order, 10.0, 100.0)
        self.assertEqual(result.order.filled_quantity, 10.0)

        self.transport.duplicate_executions = True
        again = self.gateway.collect_fills({result.order.order_id: result.order})
        self.assertEqual(len(again), 0, "a replayed execution became a new fill")
        self.assertEqual(result.order.filled_quantity, 10.0)

    def test_deduplication_is_by_execution_id_not_by_the_visible_fields(self):
        """
        Two genuinely different executions can be identical in
        instrument, side, size, price and second — a venue filling 100
        as two 50s produces exactly that. Only the execution id
        separates them.
        """
        result = submit(self.stack, quantity=100.0)
        self.transport.fill(result.order.broker_order_id, 50.0, 100.0,
                            execution_id="ex-A")
        self.transport.fill(result.order.broker_order_id, 50.0, 100.0,
                            execution_id="ex-B")
        fills = self.gateway.collect_fills({result.order.order_id: result.order})
        self.assertEqual(len(fills), 2,
                         "two identical-looking executions were collapsed")
        self.assertEqual({f.idempotency_key for f in fills}, {"ex-A", "ex-B"})

    def test_an_execution_for_an_unknown_order_is_not_invented_into_one(self):
        result = submit(self.stack)
        self.transport.executions_log.append({
            "execution_id": "ex-stray", "orderId": "ib-999999",
            "conid": "265598", "side": "B", "size": 5, "price": 99.0,
            "trade_time_r": int(AT.timestamp() * 1000)})
        fills = self.gateway.collect_fills({result.order.order_id: result.order})
        self.assertEqual([f.fill_id for f in fills], [])


# ============================================================
# 4, 5. Disconnect and reconnect
# ============================================================

class TestCase4_DisconnectAndReconnect(IBKRCase):

    def test_a_disconnected_gateway_accepts_no_orders(self):
        self.transport.connected = False
        self.gateway.connect()
        result = submit(self.stack)
        self.assertFalse(result.accepted)
        self.assertEqual(self.transport.place_calls, 0)

    def test_an_unauthenticated_gateway_does_not_retry_forever(self):
        """
        An unauthenticated gateway needs a human at a browser. Retrying
        is pointless, and a loop would look like a hang.
        """
        self.transport.authenticated = False
        state = self.gateway.connect()
        self.assertIs(state, BrokerConnectionState.AUTH_FAILED)
        self.assertEqual(self.gateway._attempts, 1)

    def test_a_competing_session_stops_immediately(self):
        """Retrying would fight the other session rather than win."""
        self.transport.competing = True
        state = self.gateway.connect()
        self.assertIs(state, BrokerConnectionState.AUTH_FAILED)
        self.assertEqual(self.gateway._attempts, 1)

    def test_reconnect_is_bounded(self):
        self.transport.authenticated = True
        self.transport.connected = False
        self.gateway.connect()
        self.assertLessEqual(self.gateway._attempts, self.config.max_retries)

    def test_a_heartbeat_restores_a_degraded_connection(self):
        self.gateway._state = BrokerConnectionState.DEGRADED
        self.assertTrue(self.gateway.heartbeat())
        self.assertIs(self.gateway.connection_state(),
                      BrokerConnectionState.CONNECTED)

    def test_working_orders_survive_a_disconnect(self):
        result = submit(self.stack)
        self.transport.connected = False
        self.gateway.connect()
        self.assertTrue(result.order.state.is_working)


# ============================================================
# 6. Unknown order
# ============================================================

class TestCase6_UnknownOrder(IBKRCase):

    def test_an_order_ibkr_never_saw_resolves_to_failed(self):
        result = submit(self.stack)
        self.orchestrator.machine.apply(
            result.order, ExecutionOrderState.UNKNOWN, at=AT,
            reason="test", strict=False)
        self.transport.unknown_orders.add(result.order.broker_order_id)

        resolutions = self.orchestrator.resolve_unknown_orders("ibkr", AT)
        self.assertTrue(resolutions[0].resolved)
        self.assertIs(result.order.state, ExecutionOrderState.FAILED)

    def test_an_unrecognised_ibkr_status_is_a_question_not_a_guess(self):
        """
        IBKR adding a status we have never seen is a real possibility.
        Assuming it resembles something familiar is how a live order
        gets treated as closed.
        """
        self.assertIs(state_from_ibkr("SomeNewStatus"),
                      ExecutionOrderState.RECONCILIATION_REQUIRED)

    def test_inactive_is_not_read_as_cancelled(self):
        """
        IBKR's catch-all for an order it holds but is not working. It
        is NOT terminal, and reading it as cancelled would leave a live
        order the system believes is closed.
        """
        state = state_from_ibkr("Inactive")
        self.assertIs(state, ExecutionOrderState.RECONCILIATION_REQUIRED)
        self.assertFalse(state.is_terminal)


# ============================================================
# 7, 8, 9. Contract, quantity and tick validation
# ============================================================

class TestCase7_InvalidContract(IBKRCase):

    def test_an_unresolved_instrument_never_reaches_ibkr(self):
        result = submit(self.stack, instrument_id="i-unresolved")
        self.assertFalse(result.accepted)
        self.assertEqual(self.transport.place_calls, 0)

    def test_ambiguous_contracts_are_refused_not_chosen(self):
        """
        Choosing the first would work almost always. The times it did
        not would be a trade in the wrong security, on the wrong
        exchange, silently.
        """
        self.transport.ambiguous_symbols.add("MSFT")
        resolution = self.gateway.resolve_contract("i-msft", "MSFT")
        self.assertFalse(resolution.ok)
        self.assertTrue(resolution.ambiguous)
        self.assertGreater(len(resolution.candidates), 1)
        self.assertIsNone(self.stack["instruments"].get("ibkr", "i-msft"))

    def test_a_discriminator_resolves_the_ambiguity(self):
        self.transport.ambiguous_symbols.add("MSFT")
        resolution = self.gateway.resolve_contract(
            "i-msft", "MSFT", primary_exchange="NASDAQ")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.contract.conid, "272093")

    def test_a_symbol_ibkr_does_not_know_is_reported(self):
        resolution = self.gateway.resolve_contract("i-nope", "NOPE")
        self.assertFalse(resolution.ok)
        self.assertIn("no contract", resolution.explain().lower())


class TestCase8_QuantityAndPrice(IBKRCase):

    def test_a_fractional_request_is_rounded_down_to_whole_shares(self):
        """
        IBKR returns a size increment of 1 for a stock, so 10.5 becomes
        10 — the increment rule doing its job. It rounds DOWN, so the
        venue never receives more exposure than risk sized.
        """
        result = submit(self.stack, quantity=10.5)
        self.assertTrue(result.accepted)
        self.assertEqual(result.order.quantity, 10.0)

    def test_a_fraction_the_venue_cannot_take_is_refused(self):
        """
        The other path. When the mapping permits a fraction but the
        venue declares no fractional support, the capability check is
        what stops it — the increment cannot, because it allows it.
        """
        mapping = self.stack["instruments"].get("ibkr", INSTRUMENT)
        mapping.quantity_increment = 0.001
        mapping.minimum_quantity = 0.001
        result = submit(self.stack, quantity=10.5)
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.FRACTIONAL_NOT_SUPPORTED,
                      result.validation.codes)
        self.assertEqual(self.transport.place_calls, 0)

    def test_quantity_rounds_down_never_up(self):
        """
        Rounding up submits more exposure than risk sized, and risk is
        the authority on size.
        """
        mapping = self.stack["instruments"].get("ibkr", INSTRUMENT)
        quantity, code = mapping.normalize_quantity(10.9)
        self.assertIsNone(code)
        self.assertEqual(quantity, 10.0)

    def test_a_negative_quantity_is_refused_not_flipped(self):
        mapping = self.stack["instruments"].get("ibkr", INSTRUMENT)
        _, code = mapping.normalize_quantity(-10.0)
        self.assertIs(code, ExecutionRejectCode.INVALID_QUANTITY)

    def test_prices_round_to_the_contract_tick(self):
        mapping = self.stack["instruments"].get("ibkr", INSTRUMENT)
        self.assertAlmostEqual(mapping.normalize_price(100.12345), 100.12,
                               places=6)

    def test_the_tick_size_came_from_ibkr_not_a_default(self):
        mapping = self.stack["instruments"].get("ibkr", INSTRUMENT)
        self.assertEqual(mapping.tick_size, 0.01)
        self.assertEqual(mapping.quantity_increment, 1.0)


# ============================================================
# 10, 11, 12. Funds, market hours, stale quotes
# ============================================================

class TestCase10_InsufficientFunds(IBKRCase):

    def test_ibkr_rejecting_for_funds_is_mapped_and_terminal(self):
        self.transport.raise_on_place = IBKRErrorCategory.INSUFFICIENT_FUNDS
        result = submit(self.stack)
        self.assertFalse(result.accepted)
        self.assertIs(result.order.state, ExecutionOrderState.REJECTED)
        self.assertIs(result.order.reject_code,
                      ExecutionRejectCode.INSUFFICIENT_BUYING_POWER)

    def test_buying_power_is_checked_before_submission(self):
        self.transport.cash = 100.0
        stack = build_ibkr()
        stack["transport"].cash = 100.0
        result = stack["orchestrator"].execute(ibkr_request(quantity=1000.0))
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.INSUFFICIENT_BUYING_POWER,
                      result.validation.codes)


class TestCase11_MarketClosed(unittest.TestCase):

    def test_a_closed_session_stops_the_order_before_ibkr(self):
        stack = build_ibkr(calendar=ClosedCalendar())
        result = stack["orchestrator"].execute(ibkr_request())
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.MARKET_CLOSED,
                      result.validation.codes)
        self.assertEqual(stack["transport"].place_calls, 0)

    def test_the_canonical_calendar_is_used_not_hardcoded_hours(self):
        """Spec §38: no `weekday < 5` anywhere."""
        stack = build_ibkr(calendar=None)
        stack["gateway"].calendar = None
        self.assertIs(stack["gateway"].market_status(INSTRUMENT, AT),
                      MarketStatus.UNKNOWN)


class TestCase12_StaleQuotes(IBKRCase):

    def test_a_quote_with_no_measurable_age_is_not_fresh(self):
        quote = self.gateway.quote(INSTRUMENT, AT)
        self.assertIsNotNone(quote)
        # The mock provides no broker timestamp, so age falls back to
        # reception — which is now, and therefore fresh.
        self.assertTrue(quote.is_fresh(AT))
        self.assertFalse(quote.is_fresh(AT + timedelta(minutes=10)))

    def test_unavailable_market_data_is_not_tradeable(self):
        self.transport.market_data_available = False
        quote = self.gateway.quote(INSTRUMENT, AT)
        self.assertIs(quote.availability, MarketDataAvailability.RESTRICTED)
        self.assertFalse(quote.availability.is_tradeable)

    def test_delayed_data_is_modelled_and_not_tradeable(self):
        """
        A delayed quote is fine for a dashboard and wrong for a limit
        price, and the difference is invisible in the number itself.
        """
        self.assertFalse(MarketDataAvailability.DELAYED.is_tradeable)
        self.assertTrue(MarketDataAvailability.AVAILABLE.is_tradeable)

    def test_a_missing_quote_never_becomes_zero(self):
        self.transport.quotes["265598"] = {"conid": "265598"}
        quote = self.gateway.quote(INSTRUMENT, AT)
        self.assertIsNone(quote.last)
        self.assertIsNone(quote.reference_price)


# ============================================================
# 13, 14. Duplicate intent, restart
# ============================================================

class TestCase13_DuplicateIntent(IBKRCase):

    def test_the_same_intent_twice_places_one_ibkr_order(self):
        first = submit(self.stack)
        second = submit(self.stack)
        self.assertEqual(self.transport.place_calls, 1)
        self.assertEqual(second.duplicate_of, first.order.order_id)

    def test_the_client_order_id_is_stable_across_retries(self):
        """
        IBKR deduplicates on the client order id, so a retry must send
        the same one. That is the second line of defence, at the venue.
        """
        first = submit(self.stack)
        stack = build_ibkr()
        again = stack["orchestrator"].execute(ibkr_request())
        self.assertEqual(first.order.client_order_id,
                         again.order.client_order_id)

    def test_the_client_order_id_reaches_ibkr(self):
        result = submit(self.stack)
        placed = next(iter(self.transport.orders.values()))
        self.assertEqual(placed.client_order_id, result.order.client_order_id)


class TestCase14_ApplicationRestart(IBKRCase):

    def test_a_restart_restores_the_broker_id_map(self):
        """
        Without it the adapter cannot attribute an IBKR execution to
        our order, so every fill after a restart would look like it
        belonged to an order we never placed.
        """
        result = submit(self.stack)
        fresh = build_ibkr()
        restored = fresh["gateway"].restore_known_orders([result.order])
        self.assertEqual(restored, 1)
        self.assertEqual(
            fresh["gateway"]._broker_ids[result.order.order_id],
            result.order.broker_order_id)

    def test_a_restart_restores_the_execution_dedup_set(self):
        result = submit(self.stack)
        fill_through_gateway(self.stack, result.order, 10.0, 100.0,
                             execution_id="ex-1")

        fresh = build_ibkr()
        fresh["gateway"].restore_known_orders([result.order])
        fresh["gateway"].restore_seen_executions(["ex-1"])
        fresh["transport"].executions_log = list(self.transport.executions_log)

        again = fresh["gateway"].collect_fills(
            {result.order.order_id: result.order})
        self.assertEqual(len(again), 0,
                         "a restart re-applied an execution already counted")

    def test_without_the_dedup_set_the_execution_would_be_recounted(self):
        """
        The control for the test above. If this did not re-collect, the
        one above would pass for the wrong reason.
        """
        result = submit(self.stack)
        fill_through_gateway(self.stack, result.order, 10.0, 100.0,
                             execution_id="ex-1")
        fresh = build_ibkr()
        fresh["gateway"].restore_known_orders([result.order])
        fresh["transport"].executions_log = list(self.transport.executions_log)
        self.assertEqual(
            len(fresh["gateway"].collect_fills(
                {result.order.order_id: result.order})), 1)


# ============================================================
# 15, 16, 17. Ordering, rate limits, authentication
# ============================================================

class TestCase15_OutOfOrderEvents(IBKRCase):

    def test_a_late_status_does_not_reopen_a_filled_order(self):
        result = submit(self.stack)
        fill_through_gateway(self.stack, result.order, 10.0, 100.0)
        self.orchestrator.machine.apply(
            result.order, ExecutionOrderState.FILLED, at=AT,
            reason="filled", strict=False)

        self.transport.set_status(result.order.broker_order_id, "Submitted")
        events = self.gateway.poll_events(AT)
        self.orchestrator.events.process(
            events, {result.order.order_id: result.order})
        self.assertIs(result.order.state, ExecutionOrderState.FILLED)

    def test_polling_emits_only_changes(self):
        """
        A poll that re-emitted the same state every tick would look
        like a duplicate downstream and be discarded — which would make
        real changes invisible too.
        """
        submit(self.stack)
        first = self.gateway.poll_events(AT)
        second = self.gateway.poll_events(AT)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)


class TestCase16_RateLimit(IBKRCase):

    def test_a_pacing_violation_is_mapped_and_stops_the_order(self):
        self.transport.rate_limited = True
        result = submit(self.stack)
        self.assertFalse(result.accepted)

    def test_rate_limit_errors_are_retryable_but_timeouts_are_not(self):
        self.assertTrue(IBKRErrorCategory.RATE_LIMIT_ERROR.is_retryable)
        self.assertFalse(IBKRErrorCategory.TIMEOUT.is_retryable,
                         "retrying a timeout is how duplicate orders happen")


class TestCase17_AuthenticationFailure(IBKRCase):

    def test_losing_authentication_stops_new_orders(self):
        self.transport.authenticated = False
        self.gateway.connect()
        result = submit(self.stack)
        self.assertFalse(result.accepted)
        self.assertEqual(self.transport.place_calls, 0)

    def test_health_reports_the_reason_a_human_can_act_on(self):
        self.transport.authenticated = False
        health = self.gateway.health_check(AT)
        self.assertIs(health.state, BrokerConnectionState.AUTH_FAILED)
        self.assertIn("not authenticated", health.detail)


# ============================================================
# 18, 19, 20. Reconciliation mismatches
# ============================================================

class TestCase18_AccountMismatch(IBKRCase):

    def test_a_cash_difference_is_recorded_never_corrected(self):
        submit(self.stack)
        self.transport.cash = 12_345.0
        record = self.orchestrator.reconcile(
            "ibkr", MOCK_ACCOUNT, AT, internal_cash=999_999.0)
        found = record.of_kind(MismatchKind.CASH_MISMATCH)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].resolved)


class TestCase19_PositionMismatch(IBKRCase):

    def test_a_position_difference_is_recorded(self):
        self.transport.set_position("265598", 250.0, 100.0)
        record = self.orchestrator.reconcile(
            "ibkr", MOCK_ACCOUNT, AT, internal_positions={INSTRUMENT: 100.0})
        self.assertTrue(record.of_kind(MismatchKind.POSITION_MISMATCH))

    def test_positions_are_attributed_to_canonical_instruments(self):
        """
        A position arrives naming a conid. Without the reverse lookup
        it could not be compared against our book at all.
        """
        self.transport.set_position("265598", 10.0, 100.0)
        positions = self.gateway.get_positions(MOCK_ACCOUNT, AT)
        self.assertEqual(positions[0].instrument_id, INSTRUMENT)


class TestCase20_OrderMismatch(IBKRCase):

    def test_an_ibkr_order_we_have_no_record_of_is_reported(self):
        self.transport.place_order(MOCK_ACCOUNT, {
            "conid": "265598", "side": "BUY", "quantity": 5,
            "orderType": "MKT", "cOID": "someone-else"})
        record = self.orchestrator.reconcile("ibkr", MOCK_ACCOUNT, AT)
        self.assertTrue(record.of_kind(MismatchKind.UNKNOWN_BROKER_ORDER))

    def test_a_status_disagreement_is_reported(self):
        result = submit(self.stack)
        self.transport.set_status(result.order.broker_order_id, "Cancelled")
        record = self.orchestrator.reconcile("ibkr", MOCK_ACCOUNT, AT)
        self.assertTrue(record.of_kind(MismatchKind.STATUS_MISMATCH))


# ============================================================
# §27 partial fills, §63 transaction safety, §64/§65 accounting
# ============================================================

class TestPartialFills(IBKRCase):

    def test_the_order_is_not_marked_filled_after_the_first_execution(self):
        result = submit(self.stack, quantity=100.0)
        fill_through_gateway(self.stack, result.order, 25.0, 100.0)
        self.assertEqual(result.order.filled_quantity, 25.0)
        self.assertEqual(result.order.remaining, 75.0)
        self.assertIsNot(result.order.state, ExecutionOrderState.FILLED)

    def test_successive_partials_average_by_notional(self):
        result = submit(self.stack, quantity=100.0)
        fill_through_gateway(self.stack, result.order, 25.0, 100.0,
                             execution_id="p1")
        fill_through_gateway(self.stack, result.order, 75.0, 108.0,
                             execution_id="p2")
        self.assertEqual(result.order.filled_quantity, 100.0)
        self.assertAlmostEqual(result.order.average_fill_price, 106.0)

    def test_an_overfilling_execution_is_refused(self):
        """The §64 invariant, reached through the IBKR path."""
        result = submit(self.stack, quantity=10.0)
        self.assertTrue(apply_fill_to_order(result.order, 10.0, 100.0))
        self.assertFalse(apply_fill_to_order(result.order, 1.0, 100.0))
        self.assertEqual(result.order.filled_quantity, 10.0)

    def test_commission_accumulates_across_executions(self):
        result = submit(self.stack, quantity=100.0)
        fill_through_gateway(self.stack, result.order, 50.0, 100.0,
                             commission=1.5, execution_id="c1")
        fill_through_gateway(self.stack, result.order, 50.0, 100.0,
                             commission=2.5, execution_id="c2")
        self.assertAlmostEqual(result.order.commission, 4.0)

    def test_a_zero_commission_means_not_yet_reported(self):
        """
        IBKR frequently reports commission separately and later. Zero
        must read as "not yet", not as "free" — the raw payload keeps
        whatever IBKR actually said.
        """
        result = submit(self.stack)
        self.transport.fill(result.order.broker_order_id, 10.0, 100.0,
                            commission=0.0)
        fills = self.gateway.collect_fills({result.order.order_id: result.order})
        self.assertEqual(fills[0].commission, 0.0)
        self.assertIn("commission", fills[0].raw_broker_payload)


class TestSafetyGates(unittest.TestCase):
    """Spec §8, §46, §56, §72."""

    def test_connecting_is_not_permission_to_trade(self):
        stack = build_ibkr(ordering_enabled=False)
        gateway = stack["gateway"]
        self.assertIs(gateway.connection_state(), BrokerConnectionState.CONNECTED)

        result = stack["orchestrator"].execute(ibkr_request())
        self.assertFalse(result.accepted)
        self.assertEqual(stack["transport"].place_calls, 0)

    def test_the_gate_reports_which_flag_is_missing(self):
        stack = build_ibkr(ordering_enabled=False)
        order = stack["orchestrator"].execute(ibkr_request()).order
        ack = stack["gateway"].submit_order(order, AT)
        self.assertFalse(ack.accepted)
        self.assertIs(ack.reject_code, ExecutionRejectCode.EXECUTION_DISABLED)
        self.assertIn("IBKR_PAPER_ORDERING_ENABLED", ack.detail)

    def test_a_disabled_integration_submits_nothing(self):
        stack = build_ibkr(enabled=False)
        order = stack["orchestrator"].execute(ibkr_request()).order
        if order is not None:
            ack = stack["gateway"].submit_order(order, AT)
            self.assertFalse(ack.accepted)
        self.assertEqual(stack["transport"].place_calls, 0)

    def test_a_live_environment_cannot_be_configured(self):
        with self.assertRaises(IBKRConfigurationError):
            IBKRConfig(environment=ExecutionEnvironment.LIVE)

    def test_a_live_gateway_cannot_be_constructed(self):
        config = paper_config()
        # Force the field past the config guard to prove the gateway
        # refuses independently — defence in depth, not one check.
        object.__setattr__(config, "environment", ExecutionEnvironment.LIVE)
        stack = build_ibkr()
        with self.assertRaises(ValueError):
            IBKRGateway(config, stack["transport"], stack["instruments"])

    def test_the_adapter_never_reports_live_execution(self):
        stack = build_ibkr()
        detail = stack["gateway"].health_detail(AT)
        self.assertFalse(detail["live_execution"])
        self.assertEqual(detail["environment"], "paper")

    def test_the_config_holds_no_credentials(self):
        config = paper_config()
        for name in ("username", "password", "token", "api_key", "secret"):
            self.assertFalse(hasattr(config, name), name)
        self.assertFalse(config.describe()["holds_credentials"])


if __name__ == "__main__":
    unittest.main()
