"""
tests/execution/test_validation_and_recovery.py
----------------------------------------------------
Pre-trade validation, instrument mapping, reconciliation and restart
recovery (Phase 14, spec §13, §15, §16, §17, §23, §34, §40, §53, §60).
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.broker_models import (
    AccountSnapshot, Broker, BrokerAccount, BrokerInstrumentMapping,
    CanonicalOrderSide, CanonicalOrderType, CanonicalTimeInForce,
    ExecutionEnvironment, ExecutionEventType, ExecutionOrderState,
    ExecutionRejectCode, MarketStatus, MismatchKind, PositionSide,
    PositionSnapshot, REJECT_EXPLANATIONS, explain,
)
from src.execution.gateway import BrokerOrderView
from src.execution.instruments import (
    InstrumentRegistry, default_equity_mapping,
)
from src.execution.reconciliation import BrokerReconciler
from tests.execution.helpers import (
    AT, build_fake_stack, execution_connection, fake_fill, request,
)


class TestInstrumentMapping(unittest.TestCase):
    """Spec §15."""

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.registry.register(BrokerInstrumentMapping(
            canonical_instrument_id="i-eurusd", broker_id="mt5-like",
            broker_symbol="EURUSD.a", asset_class="forex", currency="USD",
            tick_size=0.00001, minimum_quantity=0.01,
            quantity_increment=0.01, price_precision=5,
            contract_multiplier=100_000.0))
        self.registry.register(default_equity_mapping(
            "i-aapl", "ibkr-like", "AAPL", venue="NASDAQ"))

    def test_the_core_never_learns_the_venue_spelling(self):
        """
        Two venues, two spellings, one canonical id. Nothing above the
        adapter has to know either spelling.
        """
        forex = self.registry.resolve("mt5-like", "i-eurusd")
        self.assertEqual(forex.mapping.broker_symbol, "EURUSD.a")
        equity = self.registry.resolve("ibkr-like", "i-aapl")
        self.assertEqual(equity.mapping.broker_symbol, "AAPL")

    def test_an_unmapped_pair_is_never_guessed(self):
        """
        `AAPL` being a valid symbol somewhere is not evidence it is the
        right symbol here. A guess that is usually right is the worst
        kind — it works until it trades the wrong contract.
        """
        resolution = self.registry.resolve("mt5-like", "i-aapl")
        self.assertFalse(resolution.ok)
        self.assertIs(resolution.code,
                      ExecutionRejectCode.NO_INSTRUMENT_MAPPING)

    def test_the_reverse_lookup_attributes_inbound_events(self):
        self.assertEqual(
            self.registry.canonical_for("mt5-like", "EURUSD.a"), "i-eurusd")
        self.assertIsNone(self.registry.canonical_for("ibkr-like", "EURUSD.a"))

    def test_quantity_is_rounded_down_to_the_venue_increment(self):
        mapping = self.registry.get("mt5-like", "i-eurusd")
        quantity, code = mapping.normalize_quantity(1.237)
        self.assertIsNone(code)
        self.assertAlmostEqual(quantity, 1.23)

    def test_a_quantity_below_one_increment_is_refused(self):
        mapping = self.registry.get("mt5-like", "i-eurusd")
        quantity, code = mapping.normalize_quantity(0.004)
        self.assertIs(code, ExecutionRejectCode.QUANTITY_INCREMENT)

    def test_a_quantity_below_the_minimum_is_refused(self):
        mapping = BrokerInstrumentMapping(
            canonical_instrument_id="i", broker_id="b", broker_symbol="S",
            minimum_quantity=10.0, quantity_increment=1.0)
        quantity, code = mapping.normalize_quantity(5.0)
        self.assertIs(code, ExecutionRejectCode.BELOW_MINIMUM_QUANTITY)

    def test_price_is_rounded_to_the_venue_tick(self):
        mapping = self.registry.get("mt5-like", "i-eurusd")
        self.assertAlmostEqual(mapping.normalize_price(1.234567), 1.23457,
                               places=5)

    def test_an_invalid_quantity_is_refused_before_arithmetic(self):
        mapping = self.registry.get("ibkr-like", "i-aapl")
        for value in (0.0, -1.0, float("nan"), float("inf")):
            _, code = mapping.normalize_quantity(value)
            self.assertIs(code, ExecutionRejectCode.INVALID_QUANTITY, repr(value))

    def test_the_contract_multiplier_is_a_venue_fact(self):
        self.assertEqual(
            self.registry.get("mt5-like", "i-eurusd").contract_multiplier,
            100_000.0)
        self.assertEqual(
            self.registry.get("ibkr-like", "i-aapl").contract_multiplier, 1.0)

    def test_candidate_venues_come_from_the_mappings(self):
        self.registry.register(default_equity_mapping(
            "i-aapl", "mt5-like", "AAPL.US"))
        self.assertEqual(self.registry.brokers_for("i-aapl"),
                         ["ibkr-like", "mt5-like"])


class TestPreTradeValidation(unittest.TestCase):
    """Spec §13, §34, §40."""

    def setUp(self):
        self.orchestrator, self.gateway, self.instruments, self.safety = \
            build_fake_stack()

    def validate(self, **overrides):
        return self.orchestrator.dry_run(request(**overrides)).validation

    def test_every_failure_carries_a_code_and_a_sentence(self):
        result = self.validate(instrument_id="i-unmapped")
        for finding in result.findings:
            self.assertIsInstance(finding.code, ExecutionRejectCode)
            self.assertTrue(finding.explanation)
            self.assertTrue(finding.correlation_id)

    def test_every_reject_code_has_a_human_sentence(self):
        for code in ExecutionRejectCode:
            self.assertIn(code, REJECT_EXPLANATIONS, code.value)
            self.assertTrue(explain(code))

    def test_all_findings_are_collected_not_just_the_first(self):
        """
        One rejection tells an operator what to fix; the whole list
        tells them whether fixing it will help.
        """
        self.gateway.market = MarketStatus.CLOSED
        result = self.validate(instrument_id="i-unmapped", data_is_stale=True,
                               quantity=-5.0)
        self.assertGreaterEqual(len(result.findings), 4)

    def test_a_closed_market_stops_the_order(self):
        self.gateway.market = MarketStatus.CLOSED
        self.assertIn(ExecutionRejectCode.MARKET_CLOSED, self.validate().codes)

    def test_unknown_session_state_is_not_assumed_open(self):
        """
        A holiday is a weekday. There is no `weekday < 5` fallback
        anywhere, so an unanswerable session state stops the order.
        """
        self.gateway.market = MarketStatus.UNKNOWN
        self.assertIn(ExecutionRejectCode.MARKET_CLOSED, self.validate().codes)

    def test_a_halt_is_reported_as_a_halt(self):
        self.gateway.market = MarketStatus.HALTED
        self.assertIn(ExecutionRejectCode.INSTRUMENT_HALTED,
                      self.validate().codes)

    def test_extended_hours_need_the_venue_to_support_them(self):
        self.gateway.market = MarketStatus.PRE_MARKET
        self.assertIn(ExecutionRejectCode.SESSION_NOT_PERMITTED,
                      self.validate().codes)
        self.gateway.capability.supports_extended_hours = True
        self.assertNotIn(ExecutionRejectCode.SESSION_NOT_PERMITTED,
                         self.validate().codes)

    def test_insufficient_buying_power_is_refused(self):
        self.gateway.account.buying_power = 500.0
        codes = self.validate().codes
        self.assertIn(ExecutionRejectCode.INSUFFICIENT_BUYING_POWER, codes)

    def test_buying_power_prefers_the_weakest_available_measure(self):
        """
        Buying power, then available funds, then cash — each is a
        weaker statement, and substituting a stronger one would
        overstate capacity.
        """
        snapshot = AccountSnapshot(account_id="a", broker_id="b", at=AT,
                                   cash=100.0, available_funds=50.0,
                                   buying_power=25.0)
        self.assertEqual(snapshot.spendable, 25.0)
        snapshot.buying_power = None
        self.assertEqual(snapshot.spendable, 50.0)
        snapshot.available_funds = None
        self.assertEqual(snapshot.spendable, 100.0)

    def test_a_missing_limit_price_is_caught(self):
        self.assertIn(
            ExecutionRejectCode.MISSING_LIMIT_PRICE,
            self.validate(order_type=CanonicalOrderType.LIMIT,
                          reference_price=None, policy="market").codes)

    def test_a_position_limit_is_enforced_against_the_projection(self):
        self.gateway.positions = [PositionSnapshot(
            account_id="fake-account", broker_id="fake", instrument_id="i-aaa",
            quantity=900.0, average_price=100.0, at=AT)]
        self.assertIn(ExecutionRejectCode.POSITION_LIMIT,
                      self.validate(position_limit=950.0).codes)

    def test_risk_not_consulted_is_not_approval(self):
        codes = self.validate(risk_approved=None).codes
        self.assertIn(ExecutionRejectCode.RISK_UNAVAILABLE, codes)

    def test_a_risk_rejection_carries_its_detail(self):
        result = self.validate(risk_approved=False,
                               risk_detail="max_sector_weight exceeded")
        self.assertIn("max_sector_weight", result.explanation)

    def test_a_passing_validation_says_so_plainly(self):
        result = self.validate()
        self.assertTrue(result.passed)
        self.assertEqual(result.explanation, "All pre-trade checks passed.")
        self.assertGreaterEqual(result.checks_performed, 12)


class TestReconciliation(unittest.TestCase):
    """Spec §23."""

    def setUp(self):
        self.orchestrator, self.gateway, _, _ = build_fake_stack()
        self.reconciler = BrokerReconciler()

    def test_a_matching_book_reconciles_clean(self):
        self.orchestrator.execute(request())
        record = self.orchestrator.reconcile("fake", "fake-account", AT,
                                             internal_positions={},
                                             internal_cash=100_000.0)
        self.assertTrue(record.is_clean, [m.detail for m in record.mismatches])

    def test_a_status_disagreement_is_recorded(self):
        result = self.orchestrator.execute(request())
        view = self.gateway.broker_orders[result.order.broker_order_id]
        view.state = ExecutionOrderState.CANCELLED
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertTrue(record.of_kind(MismatchKind.STATUS_MISMATCH))

    def test_a_filled_quantity_disagreement_is_recorded(self):
        result = self.orchestrator.execute(request())
        view = self.gateway.broker_orders[result.order.broker_order_id]
        view.filled_quantity = 60.0
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        found = record.of_kind(MismatchKind.QUANTITY_MISMATCH)
        self.assertEqual(found[0].broker_value, 60.0)

    def test_a_price_disagreement_beyond_tolerance_is_recorded(self):
        result = self.orchestrator.execute(request())
        order = result.order
        order.average_fill_price = 100.0
        view = self.gateway.broker_orders[order.broker_order_id]
        view.average_fill_price = 105.0
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertTrue(record.of_kind(MismatchKind.PRICE_MISMATCH))

    def test_rounding_differences_are_not_reported_as_mismatches(self):
        result = self.orchestrator.execute(request())
        order = result.order
        order.average_fill_price = 100.0
        view = self.gateway.broker_orders[order.broker_order_id]
        view.average_fill_price = 100.000001
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertEqual(record.of_kind(MismatchKind.PRICE_MISMATCH), [])

    def test_an_order_is_matched_by_client_id_when_the_broker_id_is_missing(self):
        """
        The fallback that matters after a timeout: we may have no
        broker id, but we always sent a client id.
        """
        result = self.orchestrator.execute(request())
        order = result.order
        view = self.gateway.broker_orders.pop(order.broker_order_id)
        view.broker_order_id = "bo-renamed"
        self.gateway.broker_orders["bo-renamed"] = view
        order.broker_order_id = None

        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertEqual(record.of_kind(MismatchKind.MISSING_INTERNAL_ORDER), [])

    def test_a_duplicate_fill_in_storage_is_caught(self):
        result = self.orchestrator.execute(request())
        order = result.order
        fill = fake_fill(order, 50.0, 100.0, fill_id="f-1", key="same-key")
        twin = fake_fill(order, 50.0, 100.0, fill_id="f-2", key="same-key")
        record = self.reconciler.reconcile(
            "fake", "fake-account", AT, internal_orders=[order],
            broker_orders=[], internal_fills=[fill, twin])
        self.assertTrue(record.of_kind(MismatchKind.DUPLICATE_FILL))

    def test_nothing_is_ever_auto_resolved(self):
        self.gateway.account.cash = 1.0
        self.gateway.positions = [PositionSnapshot(
            account_id="fake-account", broker_id="fake", instrument_id="i-aaa",
            quantity=99.0, average_price=100.0, at=AT)]
        record = self.orchestrator.reconcile(
            "fake", "fake-account", AT, internal_positions={"i-aaa": 0.0},
            internal_cash=100_000.0)
        self.assertFalse(record.is_clean)
        for mismatch in record.mismatches:
            self.assertFalse(mismatch.resolved, mismatch.kind.value)

    def test_the_record_counts_what_it_compared(self):
        self.orchestrator.execute(request())
        record = self.orchestrator.reconcile("fake", "fake-account", AT)
        self.assertGreaterEqual(record.checks_performed, 5)
        self.assertEqual(record.orders_compared, 1)


class TestPositionAbstraction(unittest.TestCase):
    """Spec §17."""

    def test_side_is_derived_from_the_signed_quantity(self):
        for quantity, expected in ((10.0, PositionSide.LONG),
                                   (-10.0, PositionSide.SHORT),
                                   (0.0, PositionSide.FLAT)):
            snapshot = PositionSnapshot(
                account_id="a", broker_id="b", instrument_id="i",
                quantity=quantity, at=AT)
            self.assertIs(snapshot.side, expected)

    def test_market_value_is_none_without_a_price(self):
        snapshot = PositionSnapshot(account_id="a", broker_id="b",
                                    instrument_id="i", quantity=10.0, at=AT)
        self.assertIsNone(snapshot.market_value)
        snapshot.market_price = 50.0
        self.assertEqual(snapshot.market_value, 500.0)

    def test_hedging_lots_stay_distinct(self):
        """
        MT5 supports hedging, IBKR nets. The canonical model has to
        admit the difference rather than assume one.
        """
        long_lot = PositionSnapshot(
            account_id="a", broker_id="b", instrument_id="i", quantity=10.0,
            at=AT, lot_id="lot-1")
        short_lot = PositionSnapshot(
            account_id="a", broker_id="b", instrument_id="i", quantity=-4.0,
            at=AT, lot_id="lot-2")
        self.assertNotEqual(long_lot.lot_id, short_lot.lot_id)
        self.assertIs(long_lot.side, PositionSide.LONG)
        self.assertIs(short_lot.side, PositionSide.SHORT)


class TestPersistenceAndRecovery(unittest.TestCase):
    """Spec §53, §60."""

    def setUp(self):
        self.conn = execution_connection()
        self.repository = ExecutionRepository(self.conn)
        self.orchestrator, self.gateway, self.instruments, self.safety = \
            build_fake_stack()

    def tearDown(self):
        self.conn.close()

    def persist(self, result):
        self.repository.save_execution(
            result.order,
            transitions=self.orchestrator.machine.transitions_for(
                result.order.order_id),
            fills=[f for f in self.orchestrator.fills
                   if f.order_id == result.order.order_id],
            events=[e for e in self.orchestrator.event_log
                    if e.order_id == result.order.order_id])

    def test_the_schema_is_idempotent(self):
        initialize_execution_schema(self.conn)
        initialize_execution_schema(self.conn)
        self.assertIsNotNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='execution_orders'"
        ).fetchone())

    def test_an_order_round_trips_with_its_provenance(self):
        result = self.orchestrator.execute(request())
        self.persist(result)
        loaded = self.repository.get_order(result.order.order_id)

        self.assertEqual(loaded.signal_id, "sig-1")
        self.assertEqual(loaded.strategy_id, "strat-1")
        self.assertEqual(loaded.correlation_id, result.order.correlation_id)
        self.assertIs(loaded.state, result.order.state)
        self.assertIs(loaded.environment, ExecutionEnvironment.PAPER)

    def test_the_state_history_round_trips_in_order(self):
        result = self.orchestrator.execute(request())
        self.persist(result)
        transitions = self.repository.transitions_for(result.order.order_id)
        self.assertEqual([t.sequence for t in transitions], [1, 2, 3, 4, 5])
        self.assertIs(transitions[-1].to_state, ExecutionOrderState.ACKNOWLEDGED)

    def test_history_is_append_only(self):
        """A re-save must not be able to rewrite what was recorded."""
        result = self.orchestrator.execute(request())
        self.persist(result)
        before = self.repository.transitions_for(result.order.order_id)
        self.persist(result)
        after = self.repository.transitions_for(result.order.order_id)
        self.assertEqual([t.reason for t in before], [t.reason for t in after])

    def test_the_unique_index_blocks_a_duplicate_key_at_the_database(self):
        """
        The half of idempotency that survives the process. The
        in-memory check is the fast path; this one holds when the
        process that held the memory is gone.
        """
        result = self.orchestrator.execute(request())
        self.persist(result)
        twin = self.repository.get_order(result.order.order_id)
        twin.order_id = "eo-different"
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._save_order(twin)

    def test_restore_rebuilds_the_book_and_the_guards(self):
        result = self.orchestrator.execute(request())
        self.persist(result)

        fresh, gateway, _, _ = build_fake_stack()
        summary = self.repository.restore(fresh)

        self.assertEqual(summary["orders"], 1)
        self.assertEqual(summary["transitions"], 5)
        again = fresh.execute(request())
        self.assertEqual(gateway.submit_calls, 0,
                         "a restored process resubmitted an existing order")
        self.assertEqual(again.duplicate_of, result.order.order_id)

    def test_restore_finds_orders_that_were_in_flight(self):
        result = self.orchestrator.execute(request())
        result.order.state = ExecutionOrderState.SUBMITTING
        self.persist(result)

        fresh, _, _, _ = build_fake_stack()
        summary = self.repository.restore(fresh)
        self.assertEqual(summary["in_flight"], 1)
        self.assertEqual(summary["in_flight_ids"], [result.order.order_id])

    def test_in_flight_orders_are_findable_by_the_repository_alone(self):
        result = self.orchestrator.execute(request())
        result.order.state = ExecutionOrderState.SUBMITTED
        self.persist(result)
        self.assertEqual([o.order_id for o in self.repository.in_flight_orders()],
                         [result.order.order_id])

    def test_event_keys_are_restored_so_redeliveries_stay_duplicates(self):
        result = self.orchestrator.execute(request())
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_ACKNOWLEDGED, order)
        self.orchestrator.event_log.append(event)
        self.persist(result)

        fresh, _, _, _ = build_fake_stack()
        self.repository.restore(fresh)
        self.assertIn(event.idempotency_key, fresh.events.seen_event_keys)

    def test_fills_round_trip_with_their_raw_broker_payload(self):
        result = self.orchestrator.execute(request())
        fill = fake_fill(result.order, 100.0, 100.0)
        fill.raw_broker_payload = {"venue_says": "partial", "liquidity": "add"}
        self.repository.save_execution(result.order, fills=[fill])

        loaded = self.repository.fills_for(order_id=result.order.order_id)[0]
        self.assertEqual(loaded.raw_broker_payload["venue_says"], "partial")

    def test_a_reconciliation_record_round_trips(self):
        self.orchestrator.execute(request())
        record = self.orchestrator.reconcile("fake", "fake-account", AT,
                                             internal_cash=1.0)
        self.repository.save_reconciliation(record)
        stored = self.repository.reconciliations_for("fake")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["checks_performed"], record.checks_performed)

    def test_brokers_and_accounts_round_trip(self):
        self.repository.save_broker(Broker(
            broker_id="paper", name="Paper", adapter="paper-gateway-v1",
            created_at=AT))
        self.repository.save_account(BrokerAccount(
            account_id="acct-1", broker_id="paper", name="Paper account",
            created_at=AT))
        self.assertEqual(self.repository.get_broker("paper").adapter,
                         "paper-gateway-v1")
        self.assertEqual(
            [a.account_id for a in self.repository.accounts_for("paper")],
            ["acct-1"])

    def test_mappings_round_trip_through_the_registry(self):
        self.repository.save_mapping(default_equity_mapping(
            "i-aaa", "paper", "AAA", minimum_quantity=1.0))
        registry = InstrumentRegistry(self.conn)
        self.assertEqual(registry.load("paper"), 1)
        self.assertEqual(registry.resolve("paper", "i-aaa").mapping.broker_symbol,
                         "AAA")


if __name__ == "__main__":
    unittest.main()
