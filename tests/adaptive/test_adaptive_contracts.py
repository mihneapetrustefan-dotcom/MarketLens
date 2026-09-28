"""
tests/adaptive/test_adaptive_contracts.py
------------------------------------------------------
Phase 25.9I-A: the invariants the contracts are supposed to enforce.

Each test here names a rule from the phase specification and tries to
break it. A contract that cannot be violated in a test is a contract
that will not be violated in 25.9I-B either.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.adaptive_models import (
    AdmissionOutcome, AllocationConstraint, AllocationRejectionReason,
    BudgetPolicy, CapitalAllocationDecision, CapitalAllocationProposal,
    CashReserve, DecisionLineage, EligibleInstrument, ExecutionEconomics,
    MarketDataBudgetState, MarketDataCostEstimate, MarketDataRequirement,
    MarketDataState, MarketDataStateTransition, OpportunityAdmissionDecision,
    OpportunityAssessment, OpportunityScore, OpportunityStatus,
    OpportunityValidity, RealtimeActivationReason, RealtimeDeactivationReason,
    RiskBudget, SessionUniverse,
)
from src.domain.thesis_models import (
    EVIDENCE_BEARING_REASONS, EventRiskExposure, EventRiskKind, ExpectedHorizon,
    InvalidationZone, PositionAction, PositionActionRecommendation,
    PositionDirection, PositionThesis, PositionThesisRevision, RevisionPolicy,
    TargetZone, ThesisRevisionReason,
)

UTC = timezone.utc


def at(hour=14, minute=0, day=28):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def thesis(**overrides):
    base = dict(
        thesis_id="th-1", instrument_id="us_and_intl-aapl",
        direction=PositionDirection.LONG, created_at=at(),
        target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(94.0, 96.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=at()),
        entry_price=100.0)
    base.update(overrides)
    return PositionThesis(**base)


class TestSessionUniverseCapacity(unittest.TestCase):
    """§3.A -- 31 is a ceiling, never a quota."""

    def test_zero_members_is_valid(self):
        universe = SessionUniverse(session_date="2026-09-28", max_capacity=31)
        self.assertTrue(universe.is_empty)
        self.assertEqual(universe.occupied, 0)
        self.assertEqual(universe.unused_capacity, 31)

    def test_partially_filled_is_valid_and_unused_capacity_is_not_a_fault(self):
        universe = SessionUniverse(
            session_date="2026-09-28", max_capacity=31,
            members=tuple("sym-%d" % i for i in range(13)),
            parent_universe_size=120)
        self.assertEqual(universe.occupied, 13)
        self.assertEqual(universe.unused_capacity, 18)

    def test_full_is_valid(self):
        members = tuple("sym-%d" % i for i in range(31))
        self.assertEqual(
            SessionUniverse("2026-09-28", 31, members).unused_capacity, 0)

    def test_over_capacity_is_refused(self):
        members = tuple("sym-%d" % i for i in range(32))
        with self.assertRaises(ValueError):
            SessionUniverse("2026-09-28", 31, members)

    def test_capacity_is_configurable_not_fixed_at_31(self):
        self.assertEqual(SessionUniverse("2026-09-28", 5).max_capacity, 5)
        self.assertEqual(SessionUniverse("2026-09-28", 120).max_capacity, 120)

    def test_duplicate_members_are_refused(self):
        with self.assertRaises(ValueError):
            SessionUniverse("2026-09-28", 31, ("a", "a"))


class TestOpportunityContracts(unittest.TestCase):

    def test_rejection_must_carry_reasons(self):
        with self.assertRaises(ValueError):
            OpportunityAssessment(
                assessment_id="a1", instrument_id="i1", session_date="2026-09-28",
                score=OpportunityScore(0.1),
                validity=OpportunityValidity(as_of=at()),
                status=OpportunityStatus.REJECTED)

    def test_expiry_takes_the_earliest_deadline(self):
        validity = OpportunityValidity(
            as_of=at(14), expires_at=at(20), max_age=timedelta(hours=2))
        self.assertEqual(validity.deadline(), at(16))
        self.assertFalse(validity.is_expired_at(at(15)))
        self.assertTrue(validity.is_expired_at(at(17)))

    def test_an_unreached_target_does_not_extend_validity(self):
        # §10: expiry is set when the assessment is made. There is no
        # setter that moves it; a new assessment is required.
        validity = OpportunityValidity(as_of=at(), max_age=timedelta(days=7))
        self.assertTrue(validity.is_expired_at(at(day=28) + timedelta(days=8)))

    def test_capacity_reached_is_distinct_from_declined(self):
        decision = OpportunityAdmissionDecision(
            decision_id="d1", assessment_id="a1", instrument_id="i1",
            outcome=AdmissionOutcome.CAPACITY_REACHED, decided_at=at(),
            capacity_at_decision=31, occupied_at_decision=31)
        self.assertFalse(decision.admitted)
        self.assertIsNot(decision.outcome, AdmissionOutcome.DECLINED)

    def test_eligibility_window_is_respected(self):
        instrument = EligibleInstrument(
            instrument_id="i1", eligible_from=at(day=20), eligible_until=at(day=27))
        self.assertTrue(instrument.is_eligible_at(at(day=25)))
        self.assertFalse(instrument.is_eligible_at(at(day=28)))

    def test_score_keeps_its_components(self):
        score = OpportunityScore(0.62, {"edge": 0.4, "liquidity": 0.22})
        self.assertEqual(score.component("liquidity"), 0.22)
        self.assertIsNone(score.component("absent"))


class TestMarketDataState(unittest.TestCase):
    """§4.2 and §3.J -- nothing goes HOT without saying why."""

    def test_escalation_requires_an_activation_reason(self):
        with self.assertRaises(ValueError):
            MarketDataStateTransition(
                instrument_id="i1", previous_state=MarketDataState.COLD,
                new_state=MarketDataState.HOT, at=at())

    def test_deescalation_requires_a_deactivation_reason(self):
        with self.assertRaises(ValueError):
            MarketDataStateTransition(
                instrument_id="i1", previous_state=MarketDataState.HOT,
                new_state=MarketDataState.COLD, at=at())

    def test_legal_transitions_are_accepted(self):
        escalate = MarketDataStateTransition(
            instrument_id="i1", previous_state=MarketDataState.COLD,
            new_state=MarketDataState.HOT, at=at(),
            activation_reason=RealtimeActivationReason.EXIT_CANDIDATE)
        self.assertTrue(escalate.is_escalation)
        relax = MarketDataStateTransition(
            instrument_id="i1", previous_state=MarketDataState.HOT,
            new_state=MarketDataState.WARM, at=at(),
            deactivation_reason=RealtimeDeactivationReason.DECISION_COMPLETE)
        self.assertFalse(relax.is_escalation)

    def test_a_transition_must_change_state(self):
        with self.assertRaises(ValueError):
            MarketDataStateTransition(
                instrument_id="i1", previous_state=MarketDataState.WARM,
                new_state=MarketDataState.WARM, at=at(),
                activation_reason=RealtimeActivationReason.RISK_OVERRIDE)

    def test_hot_requirement_cannot_permit_delayed_data(self):
        with self.assertRaises(ValueError):
            MarketDataRequirement(state=MarketDataState.HOT, permits_delayed=True)

    def test_budget_state_never_reports_negative_remaining(self):
        state = MarketDataBudgetState(month="2026-09", budget_usd=20.0,
                                      spent_usd=25.0)
        self.assertEqual(state.remaining_usd, 0.0)
        self.assertTrue(state.is_exhausted)

    def test_cost_estimate_totals_are_deterministic(self):
        estimate = MarketDataCostEstimate(
            instrument_count=31, requests=12000, fixed_cost_usd=14.5,
            variable_cost_usd=0.0)
        self.assertEqual(estimate.total_usd, 14.5)
        self.assertEqual(estimate.total_usd, estimate.total_usd)


class TestCapitalAllocation(unittest.TestCase):
    """§3.B and §3.C -- cash is an answer; zero is an answer."""

    def test_cash_may_exceed_deployed_capital(self):
        proposal = CapitalAllocationProposal(
            proposal_id="p1", as_of=at(), capital_available_usd=100.0,
            decisions=(CapitalAllocationDecision("aapl", 28.0),
                       CapitalAllocationDecision("jpm", 15.0)),
            cash_reserves=(CashReserve(57.0, reason="no qualifying opportunity"),))
        self.assertEqual(proposal.deployed_usd, 43.0)
        self.assertEqual(proposal.reserved_usd, 57.0)
        self.assertEqual(proposal.uncommitted_usd, 0.0)

    def test_allocating_nothing_at_all_is_valid(self):
        proposal = CapitalAllocationProposal(
            proposal_id="p2", as_of=at(), capital_available_usd=100.0)
        self.assertEqual(proposal.deployed_usd, 0.0)
        self.assertEqual(proposal.funded_count, 0)
        self.assertEqual(proposal.uncommitted_usd, 100.0)

    def test_zero_allocation_to_a_qualified_opportunity_is_valid(self):
        decision = CapitalAllocationDecision(
            "aapl", 0.0,
            rejection_reasons=(AllocationRejectionReason.SECTOR_LIMIT,))
        self.assertFalse(decision.is_funded)
        proposal = CapitalAllocationProposal(
            proposal_id="p3", as_of=at(), capital_available_usd=100.0,
            decisions=(decision,))
        self.assertEqual(proposal.funded_count, 0)

    def test_overspending_is_refused(self):
        with self.assertRaises(ValueError):
            CapitalAllocationProposal(
                proposal_id="p4", as_of=at(), capital_available_usd=100.0,
                decisions=(CapitalAllocationDecision("aapl", 80.0),),
                cash_reserves=(CashReserve(30.0),))

    def test_an_instrument_may_appear_only_once(self):
        with self.assertRaises(ValueError):
            CapitalAllocationProposal(
                proposal_id="p5", as_of=at(), capital_available_usd=100.0,
                decisions=(CapitalAllocationDecision("aapl", 10.0),
                           CapitalAllocationDecision("aapl", 10.0)))

    def test_risk_budget_cannot_be_over_allocated(self):
        with self.assertRaises(ValueError):
            RiskBudget(total_risk_usd=100.0, allocated_risk_usd=101.0)
        self.assertEqual(RiskBudget(100.0, 40.0).remaining_risk_usd, 60.0)

    def test_fractional_constraints_are_bounded(self):
        AllocationConstraint("sector_cap", "sector", 0.25)
        with self.assertRaises(ValueError):
            AllocationConstraint("sector_cap", "sector", 1.5)


class TestExecutionEconomics(unittest.TestCase):

    def test_absent_components_are_not_zero(self):
        economics = ExecutionEconomics(expected_gross_edge=0.05)
        self.assertIsNone(economics.total_cost)
        self.assertFalse(economics.is_complete)
        # A gross edge with no measured costs nets to itself, and the
        # incompleteness is visible rather than hidden in the number.
        self.assertEqual(economics.expected_net_edge, 0.05)

    def test_unknown_gross_edge_gives_no_net_edge(self):
        economics = ExecutionEconomics(spread_cost=0.01)
        self.assertIsNone(economics.expected_net_edge)

    def test_complete_economics_nets_out(self):
        economics = ExecutionEconomics(
            expected_gross_edge=0.05, spread_cost=0.01, slippage_estimate=0.005,
            commission_estimate=0.002, market_data_cost_estimate=0.001,
            other_execution_cost=0.0)
        self.assertTrue(economics.is_complete)
        self.assertAlmostEqual(economics.expected_net_edge, 0.032, places=10)

    def test_negative_costs_are_refused(self):
        with self.assertRaises(ValueError):
            ExecutionEconomics(expected_gross_edge=0.05, spread_cost=-0.01)


class TestPositionThesis(unittest.TestCase):

    def test_target_and_invalidation_must_sit_on_opposite_sides(self):
        with self.assertRaises(ValueError):
            thesis(invalidation_zone=InvalidationZone(109.0, 111.0))

    def test_short_thesis_mirrors_the_geometry(self):
        short = thesis(direction=PositionDirection.SHORT,
                       target_zone=TargetZone(88.0, 92.0),
                       invalidation_zone=InvalidationZone(104.0, 106.0))
        self.assertIs(short.direction, PositionDirection.SHORT)
        with self.assertRaises(ValueError):
            thesis(direction=PositionDirection.SHORT,
                   target_zone=TargetZone(108.0, 112.0),
                   invalidation_zone=InvalidationZone(94.0, 96.0))

    def test_a_revision_must_name_its_predecessor(self):
        with self.assertRaises(ValueError):
            thesis(thesis_id="th-2", revision=2)

    def test_target_zone_validates_its_bounds(self):
        with self.assertRaises(ValueError):
            TargetZone(112.0, 108.0)
        with self.assertRaises(ValueError):
            TargetZone(0.0, 10.0)

    def test_invalidation_zone_validates_its_bounds(self):
        with self.assertRaises(ValueError):
            InvalidationZone(96.0, 94.0)

    def test_horizon_expiry(self):
        horizon = ExpectedHorizon(calendar_days=7, set_at=at())
        self.assertFalse(horizon.is_expired_at(at(day=30)))
        self.assertTrue(horizon.is_expired_at(at() + timedelta(days=8)))


class TestThesisRevisionRules(unittest.TestCase):
    """§3.H and §3.I -- the two rules that matter after entry."""

    def revision(self, reason, target, invalidation=InvalidationZone(94.0, 96.0)):
        previous = thesis()
        revised = thesis(thesis_id="th-2", revision=2, previous_thesis_id="th-1",
                         target_zone=target, invalidation_zone=invalidation)
        return PositionThesisRevision(
            revision_id="rev-1", reason=reason, at=at(16),
            previous=previous, revised=revised)

    def test_price_movement_alone_cannot_raise_a_target(self):
        with self.assertRaises(ValueError) as caught:
            self.revision(ThesisRevisionReason.TARGET_APPROACH,
                          TargetZone(117.0, 122.0))
        self.assertIn("evidence", str(caught.exception))

    def test_a_material_event_may_raise_a_target(self):
        revision = self.revision(ThesisRevisionReason.MATERIAL_POSITIVE_EVENT,
                                 TargetZone(117.0, 122.0))
        self.assertTrue(revision.extends_target)

    def test_raising_a_target_may_not_loosen_protection(self):
        with self.assertRaises(ValueError) as caught:
            self.revision(ThesisRevisionReason.MATERIAL_POSITIVE_EVENT,
                          TargetZone(117.0, 122.0),
                          InvalidationZone(88.0, 90.0))
        self.assertIn("separate", str(caught.exception))

    def test_raising_a_target_while_tightening_protection_is_allowed(self):
        revision = self.revision(ThesisRevisionReason.MATERIAL_POSITIVE_EVENT,
                                 TargetZone(117.0, 122.0),
                                 InvalidationZone(103.0, 105.0))
        self.assertTrue(revision.extends_target)
        self.assertTrue(revision.tightens_protection)

    def test_reducing_a_target_needs_no_special_evidence(self):
        revision = self.revision(ThesisRevisionReason.TIME_DECAY,
                                 TargetZone(104.0, 106.0))
        self.assertFalse(revision.extends_target)

    def test_lineage_is_preserved_across_revisions(self):
        revision = self.revision(ThesisRevisionReason.MODEL_REASSESSMENT,
                                 TargetZone(109.0, 113.0))
        self.assertEqual(revision.previous.thesis_id, "th-1")
        self.assertEqual(revision.revised.previous_thesis_id, "th-1")
        self.assertEqual(revision.revised.revision, 2)

    def test_a_revision_may_not_change_instrument_or_direction(self):
        previous = thesis()
        other = thesis(thesis_id="th-2", revision=2, previous_thesis_id="th-1",
                       instrument_id="us_and_intl-vz")
        with self.assertRaises(ValueError):
            PositionThesisRevision(revision_id="r", reason=ThesisRevisionReason.TIME_DECAY,
                                   at=at(16), previous=previous, revised=other)

    def test_revision_numbers_must_increase(self):
        previous = thesis()
        same = thesis(thesis_id="th-2", revision=1, previous_thesis_id="th-1")
        with self.assertRaises(ValueError):
            PositionThesisRevision(revision_id="r", reason=ThesisRevisionReason.TIME_DECAY,
                                   at=at(16), previous=previous, revised=same)

    def test_evidence_bearing_reasons_exclude_price_only_reasons(self):
        self.assertNotIn(ThesisRevisionReason.TARGET_APPROACH, EVIDENCE_BEARING_REASONS)
        self.assertNotIn(ThesisRevisionReason.TIME_DECAY, EVIDENCE_BEARING_REASONS)


class TestPositionActionRecommendation(unittest.TestCase):
    """§4.4 -- partial exits must say how much, and stay recommendations."""

    def test_partial_exit_requires_a_size(self):
        with self.assertRaises(ValueError):
            PositionActionRecommendation(
                recommendation_id="r1", instrument_id="i1",
                action=PositionAction.PARTIAL_EXIT, at=at())

    def test_partial_exit_fraction_must_be_within_zero_to_one(self):
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                PositionActionRecommendation(
                    recommendation_id="r1", instrument_id="i1",
                    action=PositionAction.PARTIAL_EXIT, at=at(), exit_fraction=bad)

    def test_a_partial_exit_of_everything_is_a_full_exit(self):
        with self.assertRaises(ValueError):
            PositionActionRecommendation(
                recommendation_id="r1", instrument_id="i1",
                action=PositionAction.PARTIAL_EXIT, at=at(), exit_fraction=1.0)

    def test_forty_percent_exit_is_expressible(self):
        recommendation = PositionActionRecommendation(
            recommendation_id="r1", instrument_id="i1",
            action=PositionAction.PARTIAL_EXIT, at=at(), exit_fraction=0.4,
            reasons=("target zone reached on a material event",))
        self.assertTrue(recommendation.changes_size)
        self.assertEqual(recommendation.exit_fraction, 0.4)

    def test_hold_carries_no_size(self):
        with self.assertRaises(ValueError):
            PositionActionRecommendation(
                recommendation_id="r1", instrument_id="i1",
                action=PositionAction.HOLD, at=at(), exit_fraction=0.5)

    def test_target_and_stop_changes_must_carry_their_new_zones(self):
        for action in (PositionAction.EXTEND_TARGET, PositionAction.REDUCE_TARGET):
            with self.assertRaises(ValueError):
                PositionActionRecommendation(
                    recommendation_id="r1", instrument_id="i1", action=action, at=at())
        with self.assertRaises(ValueError):
            PositionActionRecommendation(
                recommendation_id="r1", instrument_id="i1",
                action=PositionAction.TIGHTEN_STOP, at=at())

    def test_a_recommendation_carries_no_execution_surface(self):
        recommendation = PositionActionRecommendation(
            recommendation_id="r1", instrument_id="i1",
            action=PositionAction.HOLD, at=at())
        for forbidden in ("is_executable", "venue", "account_id", "broker_id",
                          "order_id"):
            self.assertFalse(hasattr(recommendation, forbidden),
                             "%s must not exist on a recommendation" % forbidden)


class TestRevisionPolicy(unittest.TestCase):
    """§11 -- the anti-overreaction limits, inactive by default."""

    def test_default_policy_is_permissive_and_invents_no_thresholds(self):
        policy = RevisionPolicy()
        permitted, reason = policy.permits(has_reason=True)
        self.assertTrue(permitted)
        self.assertEqual(reason, "")
        self.assertEqual(policy.min_materiality, 0.0)
        self.assertIsNone(policy.max_revisions_per_day)

    def test_an_unmeasured_threshold_input_fails_closed(self):
        policy = RevisionPolicy(min_materiality=0.02)
        permitted, reason = policy.permits(materiality=None)
        self.assertFalse(permitted)
        self.assertIn("not measured", reason)

    def test_interval_and_frequency_limits(self):
        policy = RevisionPolicy(min_revision_interval=timedelta(hours=4),
                                max_revisions_per_day=2)
        self.assertFalse(policy.permits(since_last_revision=timedelta(hours=1))[0])
        self.assertTrue(policy.permits(since_last_revision=timedelta(hours=5))[0])
        self.assertFalse(policy.permits(since_last_revision=timedelta(hours=5),
                                        revisions_today=2)[0])

    def test_a_reason_is_required_when_configured(self):
        self.assertFalse(RevisionPolicy().permits(has_reason=False)[0])


class TestLineage(unittest.TestCase):

    def test_lineage_records_only_the_steps_that_happened(self):
        lineage = DecisionLineage(lineage_id="ln-1", session_date="2026-09-28",
                                  universe_decision_id="u1")
        self.assertEqual(set(lineage.links()), {"universe_decision_id"})

    def test_extending_a_lineage_leaves_the_original_intact(self):
        lineage = DecisionLineage(lineage_id="ln-1", universe_decision_id="u1")
        extended = lineage.with_step(order_intent_id="oi-1")
        self.assertEqual(set(lineage.links()), {"universe_decision_id"})
        self.assertEqual(set(extended.links()),
                         {"universe_decision_id", "order_intent_id"})

    def test_unknown_steps_are_refused(self):
        with self.assertRaises(ValueError):
            DecisionLineage(lineage_id="ln-1").with_step(made_up_id="x")


class TestEventRisk(unittest.TestCase):

    def test_event_exposure_validates_its_fields(self):
        exposure = EventRiskExposure(
            instrument_id="i1", kind=EventRiskKind.EARNINGS,
            expected_at=at(day=30), confidence=0.9, expected_move=0.06)
        self.assertIs(exposure.kind, EventRiskKind.EARNINGS)
        with self.assertRaises(ValueError):
            EventRiskExposure(instrument_id="i1", kind=EventRiskKind.EARNINGS,
                              expected_move=-0.01)


class TestTimestampDiscipline(unittest.TestCase):

    def test_naive_timestamps_are_refused(self):
        naive = datetime(2026, 9, 28, 14, 0)
        with self.assertRaises(ValueError):
            OpportunityValidity(as_of=naive)
        with self.assertRaises(ValueError):
            CapitalAllocationProposal(proposal_id="p", as_of=naive,
                                      capital_available_usd=1.0)


if __name__ == "__main__":
    unittest.main()
