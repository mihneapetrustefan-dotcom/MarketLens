"""
tests/adaptive/test_data_attention.py
-------------------------------------------------
Phase 25.9I-C: COLD/WARM/HOT, and the four things "realtime" was hiding.

The assertions that matter most are the negative ones -- HOT plus
delayed is not ready, a cap is not an entitlement, capacity is not a
target -- because each of them is a way the system could have looked
like it was working while pricing an action off a fifteen-minute-old
quote.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.attention import (
    AttentionAllocator, AttentionConfig, request_from_recommendation,
)
from src.adaptive.billing import BillingUsage, MarketDataBillingGovernor
from src.adaptive.entitlement import (
    ChannelAvailability, EntitlementVerifier, MarkerObservation,
    MarketDataChannel, SubscriptionRegistry, entitlement_from_marker,
    entitlement_report,
)
from src.adaptive.exchange_map import ExchangeMap, InstrumentVenue
from src.adaptive.request_ledger import coalesce
from src.domain.adaptive_models import (
    MarketDataState, RealtimeActivationReason, RealtimeDeactivationReason,
)
from src.domain.data_attention_models import (
    AttentionPriority, DataAttentionRequest, InstrumentDataStatus,
)
from src.domain.market_data_billing_models import (
    BillingPeriod, CapState, EntitlementStatus,
)
from src.domain.market_data_models import MarketDataAvailability
from src.domain.paper_models import DataFreshness
from src.domain.thesis_models import PositionAction, PositionActionRecommendation

UTC = timezone.utc
NOON = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
OCTOBER = BillingPeriod(2026, 10)


def _map(**venues) -> ExchangeMap:
    return ExchangeMap({name: InstrumentVenue(name, exchange, conid=f"c-{name}",
                                              source="test")
                        for name, exchange in venues.items()})


def _allocator(config=None, **venues) -> AttentionAllocator:
    return AttentionAllocator(MarketDataBillingGovernor(), _map(**venues),
                              config or AttentionConfig())


class TestExecutionFreshness(unittest.TestCase):
    """Sections 49.20-49.25 and section 7 -- the hard invariant."""

    def test_cold_is_content_with_delayed_data(self):
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.COLD,
            availability=MarketDataAvailability.DELAYED,
            freshness=DataFreshness.AGING, age_seconds=900.0)
        ready, reason = status.execution_freshness_ready()
        self.assertFalse(ready)
        self.assertIn("execution requires a HOT desired state", reason)

    def test_hot_plus_delayed_is_not_ready(self):
        """The state all four Phase 25.9H sessions were in."""
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.DELAYED,
            entitlement=EntitlementStatus.DELAYED,
            freshness=DataFreshness.FRESH, age_seconds=2.0)
        ready, reason = status.execution_freshness_ready()
        self.assertFalse(ready)
        self.assertIn("not realtime_verified", reason)

    def test_hot_plus_verified_realtime_plus_fresh_is_ready(self):
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.AVAILABLE,
            entitlement=EntitlementStatus.REALTIME_VERIFIED,
            freshness=DataFreshness.FRESH, age_seconds=3.0)
        ready, reason = status.execution_freshness_ready()
        self.assertTrue(ready, reason)

    def test_a_stale_stream_on_a_hot_instrument_is_not_ready(self):
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.AVAILABLE,
            entitlement=EntitlementStatus.REALTIME_VERIFIED,
            freshness=DataFreshness.FRESH, age_seconds=1.0,
            stream_active=True, stream_stale=True)
        ready, reason = status.execution_freshness_ready()
        self.assertFalse(ready)
        self.assertIn("stale", reason)

    def test_an_old_quote_is_not_ready_even_when_marked_realtime(self):
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.AVAILABLE,
            entitlement=EntitlementStatus.REALTIME_VERIFIED,
            freshness=DataFreshness.FRESH, age_seconds=600.0)
        self.assertFalse(status.execution_freshness_ready()[0])

    def test_an_unmeasured_age_is_not_ready(self):
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.AVAILABLE,
            entitlement=EntitlementStatus.REALTIME_VERIFIED,
            freshness=DataFreshness.FRESH, age_seconds=None)
        ready, reason = status.execution_freshness_ready()
        self.assertFalse(ready)
        self.assertIn("never measured", reason)

    def test_a_future_quote_is_not_ready(self):
        """The 2026-09-21 case: a host clock 674s behind the venue."""
        status = InstrumentDataStatus(
            "a", desired_state=MarketDataState.HOT,
            availability=MarketDataAvailability.AVAILABLE,
            entitlement=EntitlementStatus.REALTIME_VERIFIED,
            freshness=DataFreshness.FRESH, age_seconds=-674.0)
        ready, reason = status.execution_freshness_ready()
        self.assertFalse(ready)
        self.assertIn("future", reason)


class TestDeduplication(unittest.TestCase):
    """Sections 36, 37, 49.28-49.29 -- four requesters, one charge."""

    def _requests(self):
        return [
            DataAttentionRequest("a", MarketDataState.WARM, "thesis",
                                 "target proximity",
                                 AttentionPriority.POSITION_MONITOR),
            DataAttentionRequest("a", MarketDataState.HOT, "events",
                                 "material negative event",
                                 AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.MATERIAL_NEGATIVE_EVENT),
            DataAttentionRequest("a", MarketDataState.COLD, "screen",
                                 "broad scan"),
            DataAttentionRequest("a", MarketDataState.WARM, "risk",
                                 "periodic review",
                                 AttentionPriority.POSITION_MONITOR),
        ]

    def test_four_requests_become_one_activation(self):
        merged = coalesce(self._requests())
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].requester_count, 4)

    def test_the_strongest_state_wins(self):
        self.assertIs(coalesce(self._requests())[0].state, MarketDataState.HOT)

    def test_the_most_urgent_priority_wins(self):
        self.assertIs(coalesce(self._requests())[0].priority,
                      AttentionPriority.POSITION_EXIT)

    def test_every_reason_survives_the_merge(self):
        reasons = coalesce(self._requests())[0].reasons
        for expected in ("target proximity", "material negative event",
                         "broad scan", "periodic review"):
            self.assertIn(expected, reasons)

    def test_the_merge_is_order_independent(self):
        forward = coalesce(self._requests())
        backward = coalesce(list(reversed(self._requests())))
        self.assertEqual(forward, backward)

    def test_one_activation_is_billed_once_not_four_times(self):
        allocator = _allocator(a="NASDAQ")
        usage = BillingUsage(policy=allocator.governor.policy)
        plan = allocator.plan(self._requests(), as_of=NOON, period=OCTOBER,
                              usage=usage)
        self.assertEqual(len(plan.decisions), 1)
        self.assertEqual(plan.decisions[0].estimated_cost_usd, 0.01)


class TestHotCapacityIsACeiling(unittest.TestCase):
    """Sections 14, 43, 49.30-49.33 -- capacity is not a target."""

    def test_unused_hot_capacity_is_a_normal_outcome(self):
        allocator = _allocator(AttentionConfig(max_hot_symbols=5), a="NASDAQ")
        plan = allocator.plan(
            [DataAttentionRequest("a", MarketDataState.HOT, "thesis", "exit",
                                  AttentionPriority.POSITION_EXIT,
                                  RealtimeActivationReason.EXIT_CANDIDATE)],
            as_of=NOON, period=OCTOBER)
        self.assertEqual(len(plan.hot), 1)
        self.assertEqual(plan.unused_hot_capacity, 4)

    def test_no_production_default_for_max_hot_symbols(self):
        self.assertIsNone(AttentionConfig().max_hot_symbols)
        self.assertIsNone(AttentionConfig().hot_ttl_seconds)

    def test_a_session_of_five_does_not_produce_five_hot(self):
        """Section 49.32 -- admitted is not the same as watched closely."""
        venues = {f"i{n}": "NASDAQ" for n in range(5)}
        allocator = _allocator(AttentionConfig(max_hot_symbols=3), **venues)
        requests = [DataAttentionRequest(f"i{n}", MarketDataState.COLD,
                                         "screen", "admitted to the session")
                    for n in range(5)]
        plan = allocator.plan(requests, as_of=NOON, period=OCTOBER)
        self.assertEqual(len(plan.hot), 0)
        self.assertEqual(len(plan.cold), 5)
        self.assertEqual(plan.unused_hot_capacity, 3)

    def test_a_parent_universe_of_many_does_not_go_hot(self):
        """Section 49.33 -- 130 screened candidates stay COLD."""
        venues = {f"i{n}": "NYSE" for n in range(130)}
        allocator = _allocator(AttentionConfig(max_hot_symbols=2), **venues)
        requests = [DataAttentionRequest(f"i{n}", MarketDataState.COLD,
                                         "screen", "parent universe")
                    for n in range(130)]
        plan = allocator.plan(requests, as_of=NOON, period=OCTOBER)
        self.assertEqual(len(plan.hot), 0)
        self.assertEqual(len(plan.cold), 130)

    def test_overflow_is_downgraded_in_priority_order(self):
        allocator = _allocator(AttentionConfig(max_hot_symbols=1),
                               exit_name="NASDAQ", entry_name="NASDAQ")
        plan = allocator.plan([
            DataAttentionRequest("entry_name", MarketDataState.HOT, "scan",
                                 "possible entry",
                                 AttentionPriority.ENTRY_CANDIDATE,
                                 RealtimeActivationReason.ENTRY_CANDIDATE),
            DataAttentionRequest("exit_name", MarketDataState.HOT, "thesis",
                                 "exit now", AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.EXIT_CANDIDATE),
        ], as_of=NOON, period=OCTOBER)
        granted = {d.instrument_id: d.granted_state for d in plan.decisions}
        # The open position's exit takes the only HOT slot (section 30).
        self.assertIs(granted["exit_name"], MarketDataState.HOT)
        self.assertIs(granted["entry_name"], MarketDataState.WARM)

    def test_a_cheap_exchange_does_not_make_everything_hot(self):
        """Section 43 -- even at the cap, attention stays allocated."""
        venues = {f"i{n}": "NYSE" for n in range(20)}
        allocator = _allocator(AttentionConfig(max_hot_symbols=2), **venues)
        usage = BillingUsage(policy=allocator.governor.policy)
        usage.record("NYSE", OCTOBER, 5000, NOON)      # long past the cap
        requests = [DataAttentionRequest(f"i{n}", MarketDataState.COLD,
                                         "screen", "capped venue")
                    for n in range(20)]
        plan = allocator.plan(requests, as_of=NOON, period=OCTOBER, usage=usage)
        self.assertEqual(len(plan.hot), 0)
        self.assertEqual(plan.unused_hot_capacity, 2)


class TestAttentionSafety(unittest.TestCase):
    """Sections 11, 20, 30 -- who is served, and what is refused."""

    def test_an_unattributable_instrument_is_never_escalated(self):
        allocator = _allocator(known="NASDAQ")
        plan = allocator.plan([
            DataAttentionRequest("unknown_one", MarketDataState.HOT, "thesis",
                                 "exit", AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.EXIT_CANDIDATE)],
            as_of=NOON, period=OCTOBER)
        self.assertIs(plan.decisions[0].granted_state, MarketDataState.COLD)
        self.assertIn("no known billing exchange",
                      plan.decisions[0].downgrade_reason)

    def test_a_budget_refusal_for_an_exit_is_surfaced_not_swallowed(self):
        governor = MarketDataBillingGovernor(monthly_budget_usd=0.001)
        allocator = AttentionAllocator(governor, _map(a="NYSE"),
                                       AttentionConfig())
        plan = allocator.plan([
            DataAttentionRequest("a", MarketDataState.HOT, "thesis",
                                 "invalidation breached",
                                 AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.INVALIDATION_PROXIMITY)],
            as_of=NOON, period=OCTOBER)
        self.assertTrue(any("BUDGET_CONFLICT" in note for note in plan.notes))
        # The desired state is NOT quietly downgraded to delayed data.
        self.assertIs(plan.decisions[0].granted_state, MarketDataState.HOT)

    def test_priority_ranks_position_safety_above_research(self):
        self.assertLess(AttentionPriority.POSITION_EXIT.rank,
                        AttentionPriority.ENTRY_CANDIDATE.rank)
        self.assertLess(AttentionPriority.POSITION_MONITOR.rank,
                        AttentionPriority.BROAD_SCREENING.rank)
        self.assertTrue(AttentionPriority.POSITION_EXIT.is_position_safety)
        self.assertFalse(AttentionPriority.BROAD_SCREENING.is_position_safety)

    def test_a_hot_request_must_name_its_activation_reason(self):
        with self.assertRaises(ValueError):
            DataAttentionRequest("a", MarketDataState.HOT, "someone", "because")

    def test_an_anonymous_request_is_refused(self):
        with self.assertRaises(ValueError):
            DataAttentionRequest("a", MarketDataState.COLD, "", "no requester")


class TestTransitionsAndDeactivation(unittest.TestCase):
    """Sections 31, 49.26-49.27 -- HOT does not stay HOT by forgetting."""

    def test_a_transition_records_its_reason(self):
        allocator = _allocator(a="NASDAQ")
        plan = allocator.plan([
            DataAttentionRequest("a", MarketDataState.HOT, "thesis",
                                 "exit candidate",
                                 AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.EXIT_CANDIDATE)],
            as_of=NOON, period=OCTOBER)
        transitions = allocator.transitions_for(plan)
        self.assertEqual(len(transitions), 1)
        self.assertIs(transitions[0].activation_reason,
                      RealtimeActivationReason.EXIT_CANDIDATE)
        self.assertIn("exit candidate", transitions[0].note)

    def test_deactivation_steps_down_with_a_recorded_reason(self):
        allocator = _allocator(a="NASDAQ")
        transitions = allocator.deactivate(
            {"a": MarketDataState.HOT},
            {"a": RealtimeDeactivationReason.POSITION_CLOSED}, as_of=NOON)
        self.assertEqual(len(transitions), 1)
        self.assertIs(transitions[0].new_state, MarketDataState.COLD)
        self.assertIs(transitions[0].deactivation_reason,
                      RealtimeDeactivationReason.POSITION_CLOSED)

    def test_deactivating_something_already_cold_does_nothing(self):
        allocator = _allocator(a="NASDAQ")
        self.assertEqual(allocator.deactivate(
            {"a": MarketDataState.COLD},
            {"a": RealtimeDeactivationReason.SESSION_ENDED}, as_of=NOON), ())

    def test_a_ttl_expires_a_hot_grant(self):
        allocator = _allocator(AttentionConfig(hot_ttl_seconds=300), a="NASDAQ")
        plan = allocator.plan([
            DataAttentionRequest("a", MarketDataState.HOT, "thesis", "exit",
                                 AttentionPriority.POSITION_EXIT,
                                 RealtimeActivationReason.EXIT_CANDIDATE)],
            as_of=NOON, period=OCTOBER)
        self.assertEqual(allocator.expired(plan, now=NOON), ())
        self.assertEqual(
            allocator.expired(plan, now=NOON + timedelta(seconds=301)), ("a",))


class TestEntitlement(unittest.TestCase):
    """Sections 17, 22, 50 -- a cap is not an entitlement."""

    def setUp(self):
        self.verifier = EntitlementVerifier(_map(a="NASDAQ", b="NASDAQ",
                                                 c="NYSE"))

    def test_the_marker_mapping(self):
        self.assertIs(entitlement_from_marker("R"),
                      EntitlementStatus.REALTIME_VERIFIED)
        self.assertIs(entitlement_from_marker("D"), EntitlementStatus.DELAYED)
        self.assertIs(entitlement_from_marker("Z"), EntitlementStatus.DELAYED)
        self.assertIs(entitlement_from_marker(""), EntitlementStatus.UNKNOWN)
        self.assertIs(entitlement_from_marker(None), EntitlementStatus.UNKNOWN)

    def test_a_realtime_marker_verifies_the_exchange(self):
        result = self.verifier.verify(
            [MarkerObservation("a", "R", observed_at=NOON)])
        self.assertTrue(result["NASDAQ"].is_execution_grade)

    def test_a_delayed_marker_leaves_it_unverified(self):
        result = self.verifier.verify(
            [MarkerObservation("a", "D", observed_at=NOON)])
        self.assertFalse(result["NASDAQ"].is_execution_grade)

    def test_cap_reached_with_delayed_data_is_an_unmet_expectation(self):
        """Section 50.34 -- the single most important assertion here."""
        result = self.verifier.verify(
            [MarkerObservation("a", "D", observed_at=NOON)],
            cap_states={"NASDAQ": CapState.CAP_REACHED},
            expected_after_cap={"NASDAQ": EntitlementStatus.REALTIME_VERIFIED})
        entitlement = result["NASDAQ"]
        self.assertIs(entitlement.cap_state, CapState.CAP_REACHED)
        self.assertTrue(entitlement.policy_expects_realtime)
        self.assertFalse(entitlement.is_execution_grade)
        self.assertTrue(entitlement.expectation_unmet)

    def test_a_mixed_venue_takes_the_pessimistic_reading(self):
        result = self.verifier.verify([
            MarkerObservation("a", "R", observed_at=NOON),
            MarkerObservation("b", "D", observed_at=NOON)])
        self.assertFalse(result["NASDAQ"].is_execution_grade)

    def test_verification_without_a_timestamp_cannot_be_execution_grade(self):
        result = self.verifier.verify([MarkerObservation("a", "R")])
        self.assertFalse(result["NASDAQ"].is_execution_grade)

    def test_the_contract_refuses_an_untimed_verification(self):
        from src.domain.market_data_billing_models import ExchangeEntitlement
        with self.assertRaises(ValueError):
            ExchangeEntitlement("NASDAQ",
                                observed=EntitlementStatus.REALTIME_VERIFIED)

    def test_the_report_separates_policy_from_observation(self):
        result = self.verifier.verify(
            [MarkerObservation("a", "D", observed_at=NOON)],
            cap_states={"NASDAQ": CapState.CAP_REACHED},
            expected_after_cap={"NASDAQ": EntitlementStatus.REALTIME_VERIFIED})
        report = entitlement_report(result, OCTOBER)
        self.assertEqual(report["realtime_verified_exchanges"], [])
        self.assertEqual(report["expectation_unmet_exchanges"], ["NASDAQ"])
        self.assertFalse(report["account_realtime_verified"])

    def test_one_exchange_verifying_says_nothing_about_another(self):
        result = self.verifier.verify([
            MarkerObservation("a", "R", observed_at=NOON),
            MarkerObservation("c", "D", observed_at=NOON)])
        self.assertTrue(result["NASDAQ"].is_execution_grade)
        self.assertFalse(result["NYSE"].is_execution_grade)


class TestStreamingAndRecovery(unittest.TestCase):
    """Sections 16, 33, 34, 50.38-50.41 -- audited honestly, fails closed."""

    def test_the_audited_transport_has_no_streaming_channel(self):
        availability = ChannelAvailability()
        self.assertTrue(availability.snapshot_supported)
        self.assertFalse(availability.stream_supported)
        self.assertFalse(availability.supports(MarketDataChannel.STREAM))

    def test_hot_falls_back_to_snapshot_and_says_so(self):
        channel, reason = ChannelAvailability().channel_for(MarketDataState.HOT)
        self.assertIs(channel, MarketDataChannel.SNAPSHOT)
        self.assertIn("no streaming channel", reason)

    def test_hot_prefers_streaming_where_it_exists(self):
        channel, _ = ChannelAvailability(stream_supported=True).channel_for(
            MarketDataState.HOT)
        self.assertIs(channel, MarketDataChannel.STREAM)

    def test_a_gateway_restart_discards_the_subscription_belief(self):
        registry = SubscriptionRegistry()
        registry.open("a", MarketDataChannel.SNAPSHOT, NOON)
        registry.open("b", MarketDataChannel.SNAPSHOT, NOON)
        result = registry.invalidate("gateway restarted")
        self.assertEqual(result["dropped_subscriptions"], 2)
        self.assertEqual(registry.active, {})
        self.assertEqual(registry.generation, 1)

    def test_reconciliation_asks_for_the_desired_set_to_be_reopened(self):
        registry = SubscriptionRegistry()
        registry.open("a", MarketDataChannel.SNAPSHOT, NOON)
        registry.invalidate("restart")
        result = registry.reconcile(
            {"a": MarketDataState.HOT, "b": MarketDataState.COLD},
            {"a": EntitlementStatus.REALTIME_VERIFIED},
            ChannelAvailability(), now=NOON)
        self.assertEqual(result["to_open"], ["a"])
        self.assertEqual(result["to_close"], [])

    def test_a_stale_subscription_is_detected(self):
        registry = SubscriptionRegistry()
        registry.open("a", MarketDataChannel.SNAPSHOT, NOON)
        registry.note_data("a", NOON)
        self.assertEqual(registry.stale(NOON + timedelta(seconds=60), 300.0), ())
        self.assertEqual(registry.stale(NOON + timedelta(seconds=600), 300.0),
                         ("a",))

    def test_a_hot_instrument_without_realtime_is_flagged_not_ready(self):
        registry = SubscriptionRegistry()
        registry.open("a", MarketDataChannel.SNAPSHOT, NOON)
        registry.note_data("a", NOON)
        result = registry.reconcile(
            {"a": MarketDataState.HOT}, {"a": EntitlementStatus.DELAYED},
            ChannelAvailability(), now=NOON)
        self.assertEqual(result["hot_desired_but_not_execution_grade"], ["a"])

    def test_a_stale_stream_makes_a_verified_instrument_not_ready(self):
        registry = SubscriptionRegistry()
        registry.open("a", MarketDataChannel.SNAPSHOT, NOON)
        registry.note_data("a", NOON)
        result = registry.reconcile(
            {"a": MarketDataState.HOT},
            {"a": EntitlementStatus.REALTIME_VERIFIED},
            ChannelAvailability(), now=NOON + timedelta(seconds=900))
        self.assertEqual(result["hot_desired_but_not_execution_grade"], ["a"])

    def test_reconciliation_touches_no_order_path(self):
        registry = SubscriptionRegistry()
        result = registry.reconcile({}, {}, ChannelAvailability(), now=NOON)
        for forbidden in ("order", "intent", "fill", "position"):
            self.assertFalse(any(forbidden in str(key).lower()
                                 for key in result))


class TestThesisInterface(unittest.TestCase):
    """Section 29 -- I-E can ask, without importing execution."""

    def _recommendation(self, action):
        from src.domain.thesis_models import TargetZone
        # 25.9I-E requires a revised zone on a target change, so the
        # fixture supplies one rather than the contract being relaxed.
        revised = (TargetZone(117.0, 122.0)
                   if action in (PositionAction.EXTEND_TARGET,
                                 PositionAction.REDUCE_TARGET) else None)
        return PositionActionRecommendation(
            recommendation_id="rec-1", instrument_id="a", action=action,
            at=NOON, thesis_id="th-1", reasons=("because",),
            revised_target_zone=revised,
            exit_fraction=0.4 if action is PositionAction.PARTIAL_EXIT else None)

    def test_an_exit_recommendation_asks_for_hot(self):
        request = request_from_recommendation(
            self._recommendation(PositionAction.FULL_EXIT))
        self.assertIs(request.desired_state, MarketDataState.HOT)
        self.assertIs(request.priority, AttentionPriority.POSITION_EXIT)
        self.assertIs(request.activation_reason,
                      RealtimeActivationReason.EXIT_CANDIDATE)

    def test_a_partial_exit_also_asks_for_hot(self):
        request = request_from_recommendation(
            self._recommendation(PositionAction.PARTIAL_EXIT))
        self.assertIs(request.desired_state, MarketDataState.HOT)

    def test_a_hold_does_not_ask_for_hot(self):
        request = request_from_recommendation(
            self._recommendation(PositionAction.HOLD))
        self.assertIs(request.desired_state, MarketDataState.WARM)

    def test_extending_a_target_does_not_earn_realtime_data(self):
        """25.9I-E refuses to extend on price alone; do not reward it here."""
        request = request_from_recommendation(
            self._recommendation(PositionAction.EXTEND_TARGET))
        self.assertIsNot(request.desired_state, MarketDataState.HOT)

    def test_the_lineage_is_carried_not_derived(self):
        request = request_from_recommendation(
            self._recommendation(PositionAction.FULL_EXIT))
        self.assertEqual(request.thesis_id, "th-1")


if __name__ == "__main__":
    unittest.main()
