"""
tests/execution/test_orchestration.py
------------------------------------------
Order lifecycle, dry run, policy, routing and the paper integration
(Phase 14, spec §8, §9, §12, §20, §30, §33, §35, §37, §38, §39, §42).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.broker_models import (
    Broker, BrokerAccount, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionEventType,
    ExecutionOrderState, ExecutionRejectCode, MarketStatus,
)
from src.execution.instruments import (
    InstrumentRegistry, default_equity_mapping,
)
from src.execution.orchestrator import BrokerRegistry
from src.execution.policy import (
    MarketPolicy, PLANNED_POLICIES, POLICIES, RateLimiter,
    aggressive_limit, get_policy, idempotency_key, passive_limit,
)
from src.execution.states import (
    InvalidTransition, OrderStateMachine, apply_fill_to_order,
)
from tests.execution.helpers import (
    AT, FakeGateway, build_fake_stack, build_paper_stack, fake_fill, request,
)


class TestOrderLifecycle(unittest.TestCase):

    def setUp(self):
        self.orchestrator, self.gateway, _, self.safety = build_fake_stack()

    def test_an_accepted_order_walks_the_whole_lifecycle(self):
        """
        Every step is recorded. A gap in the transition history is a
        gap in the record of why the order was allowed.
        """
        result = self.orchestrator.execute(request())
        states = [t.to_state for t in
                  self.orchestrator.machine.transitions_for(result.order.order_id)]
        self.assertEqual(states, [
            ExecutionOrderState.VALIDATING, ExecutionOrderState.APPROVED,
            ExecutionOrderState.SUBMITTING, ExecutionOrderState.SUBMITTED,
            ExecutionOrderState.ACKNOWLEDGED])

    def test_submission_is_not_a_fill(self):
        """Spec §8: `submit_order() == fill` is the assumption to avoid."""
        result = self.orchestrator.execute(request())
        self.assertTrue(result.accepted)
        self.assertEqual(result.order.filled_quantity, 0.0)
        self.assertFalse(result.order.state.is_terminal)
        self.assertEqual(result.fills, [])

    def test_a_broker_rejection_is_terminal_and_carries_its_reason(self):
        self.gateway.reject_on_submit = ExecutionRejectCode.INSUFFICIENT_MARGIN
        result = self.orchestrator.execute(request())
        self.assertFalse(result.accepted)
        self.assertIs(result.order.state, ExecutionOrderState.REJECTED)
        self.assertIs(result.order.reject_code,
                      ExecutionRejectCode.INSUFFICIENT_MARGIN)

    def test_a_rejected_order_never_reaches_the_venue_when_validation_fails(self):
        self.orchestrator.execute(request(instrument_id="i-unmapped"))
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_timestamps_are_distinguished(self):
        """Spec §41: intent, validation, submission and acknowledgement."""
        result = self.orchestrator.execute(request())
        order = result.order
        for field in ("intent_at", "validated_at", "submitted_at",
                      "acknowledged_at"):
            self.assertIsNotNone(getattr(order, field), field)

    def test_the_six_identifiers_are_distinct_concepts(self):
        result = self.orchestrator.execute(request())
        order = result.order
        self.assertNotEqual(order.intent_id, order.order_id)
        self.assertNotEqual(order.order_id, order.client_order_id)
        self.assertNotEqual(order.client_order_id, order.broker_order_id)
        self.assertTrue(order.correlation_id)

    def test_a_partial_fill_leaves_the_order_working(self):
        result = self.orchestrator.execute(request())
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_PARTIALLY_FILLED,
                                  order)
        self.orchestrator.events.process(
            [event], {order.order_id: order},
            {event.event_id: fake_fill(order, 40.0, 100.0)})

        self.assertIs(order.state, ExecutionOrderState.PARTIALLY_FILLED)
        self.assertEqual(order.filled_quantity, 40.0)
        self.assertEqual(order.remaining, 60.0)
        self.assertTrue(order.state.is_working)

    def test_successive_partials_average_by_notional(self):
        """
        Averaging the averages would weight the fills wrongly whenever
        they differ in size.
        """
        result = self.orchestrator.execute(request())
        order = result.order
        apply_fill_to_order(order, 25.0, 100.0)
        apply_fill_to_order(order, 75.0, 108.0)
        self.assertEqual(order.filled_quantity, 100.0)
        self.assertAlmostEqual(order.average_fill_price, 106.0)

    def test_cancellation_moves_through_the_machine(self):
        result = self.orchestrator.execute(request())
        order = result.order
        event = self.gateway.emit(ExecutionEventType.ORDER_CANCELLED, order)
        self.orchestrator.events.process([event], {order.order_id: order})
        self.assertIs(order.state, ExecutionOrderState.CANCELLED)
        self.assertIsNotNone(order.terminal_at)


class TestStateMachine(unittest.TestCase):

    def setUp(self):
        self.machine = OrderStateMachine()
        self.orchestrator, self.gateway, _, _ = build_fake_stack()

    def order(self):
        return self.orchestrator.execute(request()).order

    def test_an_illegal_transition_raises_in_strict_mode(self):
        """
        CREATED straight to FILLED — forward through the lifecycle, so
        the regression guard does not catch it, but skipping the
        validation and risk gates entirely. Exactly the move the
        transition table exists to refuse.
        """
        from src.domain.broker_models import ExecutionOrder
        order = ExecutionOrder(
            order_id="o-bare", intent_id="i", broker_id="fake",
            account_id="fake-account", instrument_id="i-aaa",
            side=CanonicalOrderSide.BUY, quantity=10.0)
        self.assertIs(order.state, ExecutionOrderState.CREATED)
        with self.assertRaises(InvalidTransition):
            self.machine.apply(order, ExecutionOrderState.FILLED, at=AT,
                               strict=True)
        self.assertIs(order.state, ExecutionOrderState.CREATED,
                      "the refused transition still moved the order")

    def test_a_backwards_transition_is_ignored_before_legality_is_checked(self):
        """
        A late event describing an earlier stage is dropped as stale
        rather than raised as illegal — the two are different failures
        and the ordering of the checks says which one this is.
        """
        order = self.order()
        outcome = self.orchestrator.machine.apply(
            order, ExecutionOrderState.VALIDATING, at=AT, strict=True)
        self.assertFalse(outcome.applied)
        self.assertIn("late event", outcome.ignored_reason)

    def test_an_illegal_transition_is_ignored_for_broker_events(self):
        """
        A venue sending a nonsensical sequence is a fact to record,
        not an exception to propagate into the tick.
        """
        order = self.order()
        outcome = self.orchestrator.machine.apply(
            order, ExecutionOrderState.CREATED, at=AT, strict=False)
        self.assertFalse(outcome.applied)
        self.assertIs(order.state, ExecutionOrderState.ACKNOWLEDGED)

    def test_a_forced_transition_requires_a_reason(self):
        order = self.order()
        with self.assertRaises(ValueError):
            self.orchestrator.machine.force(
                order, ExecutionOrderState.FILLED, AT, reason="")

    def test_a_forced_transition_is_recorded_as_reconciliation(self):
        """A book that was corrected always shows that it was."""
        order = self.order()
        self.orchestrator.machine.force(
            order, ExecutionOrderState.FILLED, AT, reason="broker says filled")
        last = self.orchestrator.machine.transitions_for(order.order_id)[-1]
        self.assertTrue(last.reason.startswith("reconciliation:"))

    def test_unknown_is_not_terminal(self):
        self.assertFalse(ExecutionOrderState.UNKNOWN.is_terminal)
        self.assertTrue(ExecutionOrderState.UNKNOWN.needs_reconciliation)

    def test_every_state_has_a_transition_entry(self):
        from src.domain.broker_models import ORDER_TRANSITIONS
        for state in ExecutionOrderState:
            self.assertIn(state, ORDER_TRANSITIONS, state.value)

    def test_reconciliation_required_is_reachable_from_every_state(self):
        """A disagreement can be discovered at any point in an order's life."""
        from src.domain.broker_models import ORDER_TRANSITIONS
        for state, allowed in ORDER_TRANSITIONS.items():
            if state is ExecutionOrderState.RECONCILIATION_REQUIRED:
                continue
            self.assertIn(ExecutionOrderState.RECONCILIATION_REQUIRED,
                          allowed, state.value)


class TestDryRun(unittest.TestCase):

    def setUp(self):
        self.orchestrator, self.gateway, _, self.safety = build_fake_stack()

    def test_a_dry_run_never_reaches_the_venue(self):
        result = self.orchestrator.dry_run(request())
        self.assertTrue(result.would_submit)
        self.assertFalse(result.actually_submitted)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_a_dry_run_result_cannot_claim_submission(self):
        from src.domain.broker_models import DryRunResult
        with self.assertRaises(ValueError):
            DryRunResult(correlation_id="c", intent_id="i", broker_id="b",
                         account_id="a", instrument_id="ins",
                         side=CanonicalOrderSide.BUY, actually_submitted=True)

    def test_it_builds_the_request_that_would_have_been_sent(self):
        result = self.orchestrator.dry_run(request())
        self.assertEqual(result.broker_request["symbol"], "AAA")
        self.assertEqual(result.broker_request["quantity"], 100.0)
        self.assertTrue(result.broker_request["client_order_id"])

    def test_a_failing_dry_run_explains_every_reason(self):
        self.gateway.market = MarketStatus.CLOSED
        result = self.orchestrator.dry_run(
            request(instrument_id="i-unmapped", data_is_stale=True))
        self.assertFalse(result.would_submit)
        self.assertFalse(result.mapping_passed)
        self.assertGreaterEqual(len(result.validation.findings), 3)

    def test_the_rendered_output_always_says_not_submitted(self):
        rendered = self.orchestrator.dry_run(request()).render()
        self.assertIn("Actually Submitted: NO", rendered)
        self.assertIn("DRY RUN RESULT", rendered)

    def test_a_dry_run_creates_no_order(self):
        self.orchestrator.dry_run(request())
        self.assertEqual(self.orchestrator.orders, {})

    def test_a_dry_run_does_not_consume_the_idempotency_key(self):
        """Otherwise checking an order would prevent placing it."""
        self.orchestrator.dry_run(request())
        result = self.orchestrator.execute(request())
        self.assertTrue(result.accepted)
        self.assertIsNone(result.duplicate_of)


class TestIdempotencyKey(unittest.TestCase):

    def key(self, **overrides):
        base = dict(account_id="a", instrument_id="i",
                    side=CanonicalOrderSide.BUY, quantity=100.0,
                    order_type=CanonicalOrderType.MARKET,
                    time_in_force=CanonicalTimeInForce.DAY,
                    intent_id="int-1", intent_version=1)
        base.update(overrides)
        return idempotency_key(**base)

    def test_the_same_inputs_give_the_same_key(self):
        self.assertEqual(self.key(), self.key())

    def test_float_noise_does_not_change_the_key(self):
        """
        A quantity that round-trips through the database as
        100.00000000000001 must not silently defeat the guard.
        """
        self.assertEqual(self.key(quantity=100.0),
                         self.key(quantity=100.00000000000001))

    def test_every_identifying_field_changes_the_key(self):
        baseline = self.key()
        for field, value in (
            ("account_id", "b"), ("instrument_id", "j"),
            ("side", CanonicalOrderSide.SELL), ("quantity", 101.0),
            ("order_type", CanonicalOrderType.LIMIT),
            ("time_in_force", CanonicalTimeInForce.GTC),
            ("limit_price", 99.0), ("stop_price", 95.0),
            ("intent_id", "int-2"), ("intent_version", 2),
        ):
            self.assertNotEqual(baseline, self.key(**{field: value}), field)

    def test_the_client_order_id_is_derived_from_the_key(self):
        """
        A retry must send the SAME client id, or a venue that
        deduplicates on it cannot.
        """
        from src.execution.policy import client_order_id
        self.assertEqual(client_order_id(self.key()),
                         client_order_id(self.key()))


class TestExecutionPolicy(unittest.TestCase):

    def test_market_is_the_default_and_needs_no_price(self):
        decision = MarketPolicy().decide(CanonicalOrderSide.BUY, None)
        self.assertIs(decision.order_type, CanonicalOrderType.MARKET)

    def test_a_limit_policy_without_a_price_falls_back_to_market(self):
        decision = get_policy("limit").decide(CanonicalOrderSide.BUY, None)
        self.assertIs(decision.order_type, CanonicalOrderType.MARKET)
        self.assertIn("no reference price", decision.rationale)

    def test_a_passive_buy_sits_below_the_reference(self):
        decision = passive_limit(10.0).decide(CanonicalOrderSide.BUY, 100.0)
        self.assertLess(decision.limit_price, 100.0)

    def test_a_passive_sell_sits_above_the_reference(self):
        decision = passive_limit(10.0).decide(CanonicalOrderSide.SELL, 100.0)
        self.assertGreater(decision.limit_price, 100.0)

    def test_an_aggressive_buy_crosses_upward(self):
        decision = aggressive_limit(10.0).decide(CanonicalOrderSide.BUY, 100.0)
        self.assertGreater(decision.limit_price, 100.0)

    def test_an_unknown_policy_raises_rather_than_defaulting(self):
        """A typo must not silently execute a different strategy."""
        with self.assertRaises(ValueError):
            get_policy("markt")

    def test_planned_policies_are_named_but_not_registered(self):
        for name in PLANNED_POLICIES:
            self.assertNotIn(name, POLICIES)
            with self.assertRaises(ValueError) as caught:
                get_policy(name)
            self.assertIn("not implemented", str(caught.exception))

    def test_the_policy_shapes_the_order(self):
        orchestrator, gateway, _, _ = build_fake_stack()
        result = orchestrator.execute(request(policy="limit"))
        self.assertIs(result.order.order_type, CanonicalOrderType.LIMIT)
        self.assertIsNotNone(result.order.limit_price)


class TestRateLimiting(unittest.TestCase):

    def test_no_limit_means_no_refusal(self):
        limiter = RateLimiter(None)
        for _ in range(1000):
            self.assertTrue(limiter.allow(AT))

    def test_the_budget_is_enforced(self):
        limiter = RateLimiter(2)
        for _ in range(2):
            self.assertTrue(limiter.allow(AT))
            limiter.record(AT)
        self.assertFalse(limiter.allow(AT))

    def test_the_window_slides(self):
        limiter = RateLimiter(2)
        limiter.record(AT)
        limiter.record(AT)
        self.assertTrue(limiter.allow(AT + timedelta(seconds=61)))

    def test_it_refuses_rather_than_sleeping(self):
        """A blocking limiter inside a batch job looks like a hang."""
        limiter = RateLimiter(1)
        limiter.record(AT)
        self.assertIs(limiter.check(AT), ExecutionRejectCode.RATE_LIMITED)


class TestMultiBrokerRouting(unittest.TestCase):
    """Spec §37, §38, §39."""

    def setUp(self):
        self.registry = BrokerRegistry()
        self.instruments = InstrumentRegistry()
        for broker_id in ("alpha", "beta"):
            gateway = FakeGateway(broker_id)
            self.registry.register(
                Broker(broker_id=broker_id, name=broker_id.title()),
                gateway,
                [BrokerAccount(account_id=f"{broker_id}-1", broker_id=broker_id,
                               name="One"),
                 BrokerAccount(account_id=f"{broker_id}-2", broker_id=broker_id,
                               name="Two")])
            self.instruments.register(
                default_equity_mapping("i-aaa", broker_id, "AAA"))
        self.instruments.register(
            default_equity_mapping("i-only-alpha", "alpha", "ONLY"))

    def test_several_brokers_coexist(self):
        self.assertEqual(self.registry.ids(), ["alpha", "beta"])

    def test_each_broker_holds_several_accounts(self):
        self.assertEqual(len(self.registry.get("alpha").accounts), 2)
        self.assertIsNotNone(self.registry.account("beta", "beta-2"))
        self.assertIsNone(self.registry.account("beta", "alpha-1"))

    def test_candidate_venues_come_from_the_mappings(self):
        self.assertEqual(
            self.registry.brokers_for_instrument(self.instruments, "i-aaa"),
            ["alpha", "beta"])
        self.assertEqual(
            self.registry.brokers_for_instrument(self.instruments,
                                                 "i-only-alpha"),
            ["alpha"])

    def test_routing_lists_candidates_and_does_not_choose(self):
        """
        Preferred venue, fallback and asset-class routing are future
        work. A chooser here would have to invent a policy nobody has
        specified.
        """
        candidates = self.registry.brokers_for_instrument(
            self.instruments, "i-aaa")
        self.assertIsInstance(candidates, list)
        self.assertGreater(len(candidates), 1)

    def test_a_broker_id_mismatch_is_refused_at_registration(self):
        with self.assertRaises(ValueError):
            self.registry.register(
                Broker(broker_id="gamma", name="G"), FakeGateway("delta"))


class TestPaperIntegration(unittest.TestCase):
    """Spec §30, §71 question 2: Phase 13 still works, through Phase 14."""

    def setUp(self):
        self.stack = build_paper_stack()
        self.orchestrator = self.stack["orchestrator"]
        self.gateway = self.stack["gateway"]
        self.instrument = self.stack["instrument"]
        self.bars = self.stack["bars"]

    def paper_request(self, **overrides):
        base = dict(instrument_id=self.instrument, broker_id="paper",
                    account_id="acct-1", now=self.bars[-3].timestamp)
        base.update(overrides)
        return request(**base)

    def test_an_order_reaches_the_paper_executor_and_fills(self):
        result = self.orchestrator.execute(self.paper_request())
        self.assertTrue(result.accepted)

        moment = self.bars[-2].timestamp
        self.gateway.set_market_context(
            prices={self.instrument: 100.0},
            available_cash=self.stack["ledger"].cash)
        fills = self.gateway.try_fill(result.order, moment)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, 100.0)
        self.assertGreater(fills[0].commission, 0.0)

    def test_the_fill_carries_the_phase_12_cost_and_slippage_versions(self):
        """Spec §30: the models are reused, not reimplemented."""
        result = self.orchestrator.execute(self.paper_request())
        self.gateway.set_market_context(available_cash=100_000.0)
        fill = self.gateway.try_fill(result.order, self.bars[-2].timestamp)[0]
        payload = fill.raw_broker_payload
        self.assertEqual(payload["execution_model_version"], "paper-exec-v1")
        self.assertEqual(payload["venue"], "paper")
        self.assertIsNotNone(fill.reference_price)

    def test_the_paper_ledger_is_the_phase_13_ledger(self):
        before = self.stack["ledger"].cash
        result = self.orchestrator.execute(self.paper_request())
        self.gateway.set_market_context(available_cash=before)
        self.gateway.try_fill(result.order, self.bars[-2].timestamp)
        self.assertLess(self.stack["ledger"].cash, before,
                        "the fill did not reach the Phase 13 ledger")

    def test_the_gateway_declares_only_what_paper_can_do(self):
        capability = self.gateway.get_capabilities()
        self.assertTrue(capability.supports_market_orders)
        self.assertFalse(capability.supports_bracket_orders)
        self.assertFalse(capability.supports_order_modification)
        self.assertNotIn(CanonicalTimeInForce.FOK, capability.times_in_force)

    def test_an_unsupported_time_in_force_is_refused_not_downgraded(self):
        result = self.orchestrator.execute(
            self.paper_request(time_in_force=CanonicalTimeInForce.FOK))
        self.assertFalse(result.accepted)
        self.assertIn(ExecutionRejectCode.UNSUPPORTED_TIME_IN_FORCE,
                      result.validation.codes)

    def test_market_status_comes_from_the_phase_12_calendar(self):
        status = self.gateway.market_status(self.instrument,
                                            self.bars[-2].timestamp)
        self.assertIs(status, MarketStatus.OPEN)
        self.assertIs(self.gateway.market_status("i-nonexistent", AT),
                      MarketStatus.UNKNOWN)

    def test_an_instrument_with_no_data_is_unknown_not_open(self):
        result = self.orchestrator.execute(
            self.paper_request(instrument_id="i-nodata"))
        self.assertFalse(result.accepted)

    def test_the_venue_book_is_restored_so_recovery_reconciles_clean(self):
        """
        The paper venue is in-process and forgets on restart, unlike a
        real broker that keeps its own state across ours. Without
        restoring it, every recovered order would look like one the
        venue had never heard of — a mismatch caused by the adapter
        forgetting rather than by any real disagreement.
        """
        result = self.orchestrator.execute(self.paper_request())
        order = result.order

        fresh = build_paper_stack(conn=self.stack["conn"])
        fresh["orchestrator"].seed(orders=[order])
        dirty = fresh["orchestrator"].reconcile("paper", "acct-1",
                                                self.bars[-2].timestamp)
        self.assertFalse(dirty.is_clean,
                         "the un-restored venue should not have known this order")

        restored = fresh["gateway"].restore_orders([order])
        self.assertEqual(restored, 1)
        clean = fresh["orchestrator"].reconcile("paper", "acct-1",
                                                self.bars[-2].timestamp)
        self.assertTrue(clean.is_clean, [m.detail for m in clean.mismatches])

    def test_only_working_orders_return_to_the_venue_book(self):
        result = self.orchestrator.execute(self.paper_request())
        result.order.state = ExecutionOrderState.FILLED
        fresh = build_paper_stack(conn=self.stack["conn"])
        self.assertEqual(fresh["gateway"].restore_orders([result.order]), 0)

    def test_the_full_trace_survives_the_boundary(self):
        result = self.orchestrator.execute(self.paper_request())
        self.gateway.set_market_context(available_cash=100_000.0)
        self.gateway.try_fill(result.order, self.bars[-2].timestamp)
        trace = self.orchestrator.trace(result.order.order_id)

        self.assertEqual(trace["signal_id"], "sig-1")
        self.assertEqual(trace["strategy_id"], "strat-1")
        self.assertEqual(trace["environment"], "paper")
        self.assertTrue(trace["states"])


if __name__ == "__main__":
    unittest.main()
