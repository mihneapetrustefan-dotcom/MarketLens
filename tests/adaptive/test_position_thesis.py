"""
tests/adaptive/test_position_thesis.py
---------------------------------------------------
Phase 25.9I-E: a thesis may evolve, and it may not drift.

The tests that matter most here are the refusals: price alone must not
raise a target, time alone must not extend a horizon, and a cooldown
must never be able to suppress an exit. Each of those is a rule that
looks obvious written down and disappears quietly under a deadline.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.risk_budget import RiskConfig
from src.adaptive.thesis import (
    CapitalReleaseRecommendation, PositionThesisEngine, ThesisEngineError,
    ThesisPolicy,
)
from src.domain.allocation_models import RiskEstimateStatus
from src.domain.adaptive_models import MarketDataState
from src.domain.thesis_models import (
    EvidenceDirection, ExpectedHorizon, InvalidationZone, LEGAL_THESIS_TRANSITIONS,
    PositionAction, PositionDirection, PositionThesis, RevisionPolicy, TargetZone,
    ThesisEvidence, ThesisRevisionReason, ThesisStatus, transition_allowed,
)

UTC = timezone.utc
DAY0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def day(n, hour=14):
    return datetime(2026, 9, 1 + n, hour, 0, tzinfo=UTC)


def engine(**policy):
    revision_policy = policy.pop("revision_policy", None)
    return PositionThesisEngine(
        ThesisPolicy(revision_policy=revision_policy or RevisionPolicy(),
                     **policy), RiskConfig())


def thesis_of(eng=None, **overrides):
    eng = eng or engine()
    base = dict(
        as_of=DAY0, direction=PositionDirection.LONG,
        target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(93.0, 94.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=DAY0),
        entry_price=100.0, entry_notional=50.0, summary="entry",
        session_snapshot_id="su-x", admission_decision_id="adm-x",
        allocation_plan_id="alloc-x")
    base.update(overrides)
    return eng.open_thesis("us_and_intl-demo", **base)


def evidence(name="ev", direction=EvidenceDirection.POSITIVE, materiality=0.8,
             at=None, invalidates=False, confidence=None):
    return ThesisEvidence(name, direction, at or day(4), summary="fixture",
                          materiality=materiality, confidence=confidence,
                          invalidates_thesis=invalidates, source="test")


class TestCreationAndLifecycle(unittest.TestCase):

    def test_creation_is_deterministic(self):
        first, second = thesis_of(), thesis_of()
        self.assertEqual(first.thesis_id, second.thesis_id)
        self.assertEqual(first.status, ThesisStatus.ACTIVE)
        self.assertEqual((first.remaining_fraction, first.realized_fraction),
                         (1.0, 0.0))

    def test_the_thesis_records_why_it_was_opened(self):
        thesis = thesis_of()
        self.assertEqual(thesis.initial_summary, "entry")
        self.assertEqual(thesis.initial_horizon.calendar_days, 7)
        self.assertEqual(thesis.session_snapshot_id, "su-x")
        self.assertEqual(thesis.allocation_plan_id, "alloc-x")

    def test_legal_transitions_are_allowed(self):
        self.assertTrue(transition_allowed(ThesisStatus.ACTIVE,
                                           ThesisStatus.PARTIALLY_EXITED))
        self.assertTrue(transition_allowed(ThesisStatus.PARTIALLY_EXITED,
                                           ThesisStatus.CLOSED))

    def test_closed_is_terminal(self):
        self.assertEqual(LEGAL_THESIS_TRANSITIONS[ThesisStatus.CLOSED], ())
        self.assertFalse(transition_allowed(ThesisStatus.CLOSED,
                                            ThesisStatus.ACTIVE))

    def test_a_closed_thesis_cannot_still_hold_exposure(self):
        with self.assertRaises(ValueError):
            PositionThesis(
                thesis_id="t", instrument_id="i",
                direction=PositionDirection.LONG, created_at=DAY0,
                target_zone=TargetZone(108, 112),
                invalidation_zone=InvalidationZone(93, 94),
                horizon=ExpectedHorizon(calendar_days=7, set_at=DAY0),
                status=ThesisStatus.CLOSED, remaining_fraction=0.5,
                realized_fraction=0.5)

    def test_a_partially_exited_thesis_must_have_realized_something(self):
        with self.assertRaises(ValueError):
            PositionThesis(
                thesis_id="t", instrument_id="i",
                direction=PositionDirection.LONG, created_at=DAY0,
                target_zone=TargetZone(108, 112),
                invalidation_zone=InvalidationZone(93, 94),
                horizon=ExpectedHorizon(calendar_days=7, set_at=DAY0),
                status=ThesisStatus.PARTIALLY_EXITED)

    def test_a_closed_thesis_cannot_be_reassessed(self):
        thesis = thesis_of()
        closed, _ = engine().apply_partial_exit(
            thesis, 0.5, day(2), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        closed2, _ = engine().apply_partial_exit(
            closed, 0.5, day(3), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        self.assertIs(closed2.status, ThesisStatus.CLOSED)
        with self.assertRaises(ThesisEngineError):
            engine().reassess(closed2, day(4))


class TestZonesAndHorizon(unittest.TestCase):

    def test_target_zone_validation(self):
        with self.assertRaises(ValueError):
            TargetZone(112.0, 108.0)
        with self.assertRaises(ValueError):
            TargetZone(-1.0, 10.0)

    def test_invalidation_must_sit_below_a_long_target(self):
        with self.assertRaises(ValueError):
            thesis_of(invalidation_zone=InvalidationZone(109.0, 111.0))

    def test_horizon_validation_and_expiry(self):
        horizon = ExpectedHorizon(calendar_days=7, set_at=DAY0)
        self.assertFalse(horizon.is_expired_at(day(6)))
        self.assertTrue(horizon.is_expired_at(day(8)))
        with self.assertRaises(ValueError):
            ExpectedHorizon(calendar_days=0, set_at=DAY0)

    def test_fractions_must_reconcile(self):
        with self.assertRaises(ValueError):
            PositionThesis(
                thesis_id="t", instrument_id="i",
                direction=PositionDirection.LONG, created_at=DAY0,
                target_zone=TargetZone(108, 112),
                invalidation_zone=InvalidationZone(93, 94),
                horizon=ExpectedHorizon(calendar_days=7, set_at=DAY0),
                remaining_fraction=0.7, realized_fraction=0.2)


class TestPriceAloneChangesNothing(unittest.TestCase):
    """§12 — the rule the whole phase is built around."""

    def test_price_near_target_gives_hold_and_a_reassessment_request(self):
        assessment = engine().reassess(thesis_of(), day(2), price=109.0)
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertIs(assessment.required_state, MarketDataState.WARM)
        self.assertTrue(any("cannot extend a target" in r
                            for r in assessment.reasons))

    def test_price_alone_cannot_extend_a_target(self):
        assessment = engine().reassess(
            thesis_of(), day(2), price=109.0,
            evidence=[evidence("ev-noise", materiality=None)],
            proposed_target=TargetZone(120.0, 125.0))
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertIsNone(assessment.revised_thesis)
        self.assertTrue(assessment.blocked_by_policy)

    def test_unmeasured_materiality_cannot_clear_a_threshold(self):
        eng = engine(minimum_extension_materiality=0.5)
        assessment = eng.reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-vague", materiality=None)],
            proposed_target=TargetZone(120.0, 125.0))
        self.assertIs(assessment.action, PositionAction.HOLD)

    def test_weak_materiality_below_the_threshold_is_refused(self):
        eng = engine(minimum_extension_materiality=0.6)
        assessment = eng.reassess(
            thesis_of(), day(4), evidence=[evidence("ev-weak", materiality=0.2)],
            proposed_target=TargetZone(120.0, 125.0))
        self.assertIs(assessment.action, PositionAction.HOLD)


class TestTimeDecay(unittest.TestCase):
    """§9 — a seven-day thesis does not become a fourteen-day thesis."""

    def test_an_expired_horizon_without_evidence_recommends_exit(self):
        assessment = engine().reassess(thesis_of(), day(8), price=101.0)
        self.assertIs(assessment.action, PositionAction.FULL_EXIT)
        self.assertIs(assessment.revised_thesis.status, ThesisStatus.EXPIRED)
        self.assertTrue(any("does not extend itself" in r
                            for r in assessment.reasons))

    def test_the_horizon_is_not_silently_extended(self):
        assessment = engine().reassess(thesis_of(), day(8), price=101.0)
        self.assertEqual(assessment.revised_thesis.horizon.calendar_days, 7)

    def test_an_expiry_policy_of_hold_still_refuses_to_extend(self):
        eng = engine(expiry_action=PositionAction.HOLD)
        assessment = eng.reassess(thesis_of(eng), day(8), price=101.0)
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertIs(assessment.required_state, MarketDataState.WARM)

    def test_material_evidence_at_expiry_permits_a_revision(self):
        assessment = engine().reassess(
            thesis_of(), day(8), price=109.0,
            evidence=[evidence("ev-strong", materiality=0.9, at=day(8))],
            proposed_horizon=ExpectedHorizon(calendar_days=12, set_at=DAY0))
        self.assertIsNotNone(assessment.revised_thesis)
        self.assertEqual(assessment.revised_thesis.horizon.calendar_days, 12)


class TestEvidenceDrivenRevision(unittest.TestCase):

    def test_material_positive_evidence_can_extend_a_target(self):
        assessment = engine().reassess(
            thesis_of(), day(4), evidence=[evidence()],
            proposed_target=TargetZone(117.0, 122.0))
        self.assertIs(assessment.action, PositionAction.EXTEND_TARGET)
        self.assertEqual(assessment.revised_thesis.target_zone.low, 117.0)
        self.assertTrue(assessment.revision.extends_target)

    def test_raising_a_target_while_loosening_protection_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            engine().reassess(
                thesis_of(), day(4), evidence=[evidence()],
                proposed_target=TargetZone(117.0, 122.0),
                proposed_invalidation=InvalidationZone(85.0, 86.0))
        self.assertIn("separate", str(caught.exception))

    def test_raising_a_target_with_tighter_protection_is_allowed(self):
        assessment = engine().reassess(
            thesis_of(), day(4), evidence=[evidence()],
            proposed_target=TargetZone(117.0, 122.0),
            proposed_invalidation=InvalidationZone(101.0, 102.0))
        self.assertTrue(assessment.revision.tightens_protection)

    def test_a_target_can_be_reduced_on_negative_evidence(self):
        assessment = engine().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-bad", EvidenceDirection.NEGATIVE, 0.6)],
            proposed_target=TargetZone(104.0, 107.0))
        self.assertIs(assessment.action, PositionAction.REDUCE_TARGET)
        self.assertEqual(assessment.revised_thesis.target_zone.high, 107.0)

    def test_protection_can_be_tightened_on_its_own(self):
        assessment = engine().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-risk", EvidenceDirection.NEUTRAL, 0.5)],
            proposed_invalidation=InvalidationZone(97.0, 98.0))
        self.assertIs(assessment.action, PositionAction.TIGHTEN_STOP)

    def test_invalidating_evidence_recommends_a_full_exit(self):
        assessment = engine().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-dead", EvidenceDirection.NEGATIVE, 0.9,
                               invalidates=True)])
        self.assertIs(assessment.action, PositionAction.FULL_EXIT)
        self.assertIs(assessment.revised_thesis.status, ThesisStatus.INVALIDATED)

    def test_negative_evidence_without_a_proposal_holds_and_asks_to_look(self):
        assessment = engine().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-soft", EvidenceDirection.NEGATIVE, 0.4)])
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertIs(assessment.required_state, MarketDataState.WARM)

    def test_positive_evidence_does_not_automatically_extend_anything(self):
        assessment = engine().reassess(thesis_of(), day(4),
                                       evidence=[evidence()])
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertIsNone(assessment.revised_thesis)

    def test_a_price_breaching_invalidation_exits_regardless_of_evidence(self):
        assessment = engine().reassess(thesis_of(), day(3), price=93.5)
        self.assertIs(assessment.action, PositionAction.FULL_EXIT)
        self.assertIs(assessment.revised_thesis.status, ThesisStatus.INVALIDATED)


class TestPartialExitAccounting(unittest.TestCase):
    """§27 — fractions are of the ORIGINAL, and they always reconcile."""

    def test_a_partial_exit_updates_both_fractions(self):
        revised, revision = engine().apply_partial_exit(
            thesis_of(), 0.4, day(4),
            reason=ThesisRevisionReason.MATERIAL_POSITIVE_EVENT)
        self.assertAlmostEqual(revised.remaining_fraction, 0.6)
        self.assertAlmostEqual(revised.realized_fraction, 0.4)
        self.assertIs(revised.status, ThesisStatus.PARTIALLY_EXITED)
        self.assertEqual(revision.previous.thesis_id, revision.revised.previous_thesis_id)

    def test_successive_partial_exits_are_of_the_original(self):
        eng = engine()
        first, _ = eng.apply_partial_exit(
            thesis_of(), 0.4, day(4), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        second, _ = eng.apply_partial_exit(
            first, 0.3, day(5), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        self.assertAlmostEqual(second.remaining_fraction, 0.3)
        self.assertAlmostEqual(second.realized_fraction, 0.7)

    def test_notional_follows_the_fractions(self):
        revised, _ = engine().apply_partial_exit(
            thesis_of(), 0.4, day(4), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        self.assertAlmostEqual(revised.remaining_notional, 30.0)
        self.assertAlmostEqual(revised.realized_notional, 20.0)

    def test_exiting_the_whole_remainder_closes_the_thesis(self):
        eng = engine()
        first, _ = eng.apply_partial_exit(
            thesis_of(), 0.5, day(4), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        closed, _ = eng.apply_partial_exit(
            first, 0.5, day(5), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        self.assertIs(closed.status, ThesisStatus.CLOSED)
        self.assertEqual(closed.remaining_fraction, 0.0)

    def test_a_fraction_of_zero_or_one_is_refused(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ThesisEngineError):
                engine().apply_partial_exit(
                    thesis_of(), bad, day(4),
                    reason=ThesisRevisionReason.MODEL_REASSESSMENT)

    def test_exiting_more_than_remains_is_refused(self):
        eng = engine()
        first, _ = eng.apply_partial_exit(
            thesis_of(), 0.7, day(4), reason=ThesisRevisionReason.MODEL_REASSESSMENT)
        with self.assertRaises(ThesisEngineError):
            eng.apply_partial_exit(
                first, 0.5, day(5), reason=ThesisRevisionReason.MODEL_REASSESSMENT)

    def test_a_partial_exit_through_reassessment_frees_capital(self):
        assessment = engine().reassess(
            thesis_of(), day(4), evidence=[evidence()], exit_fraction=0.4)
        self.assertIs(assessment.action, PositionAction.PARTIAL_EXIT)
        self.assertIsNotNone(assessment.capital_release)
        self.assertAlmostEqual(assessment.capital_release.released_notional, 20.0)

    def test_capital_release_is_only_a_statement(self):
        release = CapitalReleaseRecommendation("i", 0.4, 20.0)
        for forbidden in ("allocate", "reallocate", "execute"):
            self.assertFalse(hasattr(release, forbidden))


class TestAntiOverreaction(unittest.TestCase):
    """§23 and §24 — restrain greed, never restrain safety."""

    def cooled(self):
        return engine(revision_policy=RevisionPolicy(
            min_revision_interval=timedelta(hours=6)))

    def test_cooldown_withholds_a_second_upside_revision(self):
        assessment = self.cooled().reassess(
            thesis_of(), day(4), evidence=[evidence("ev-2")],
            proposed_target=TargetZone(118.0, 123.0),
            since_last_revision=timedelta(minutes=10))
        self.assertIs(assessment.action, PositionAction.HOLD)
        self.assertTrue(assessment.blocked_by_policy)

    def test_cooldown_does_not_block_an_invalidation_exit(self):
        assessment = self.cooled().reassess(
            thesis_of(), day(4),
            evidence=[evidence("ev-urgent", EvidenceDirection.NEGATIVE, 0.95,
                               invalidates=True)],
            since_last_revision=timedelta(minutes=10))
        self.assertIs(assessment.action, PositionAction.FULL_EXIT)
        self.assertFalse(assessment.blocked_by_policy)

    def test_cooldown_does_not_block_a_price_based_invalidation(self):
        assessment = self.cooled().reassess(
            thesis_of(), day(4), price=93.0,
            since_last_revision=timedelta(minutes=1))
        self.assertIs(assessment.action, PositionAction.FULL_EXIT)

    def test_a_revision_frequency_cap_withholds_further_upside(self):
        eng = engine(revision_policy=RevisionPolicy(max_revisions_per_day=1))
        assessment = eng.reassess(
            thesis_of(), day(4), evidence=[evidence()],
            proposed_target=TargetZone(118.0, 123.0), revisions_today=1)
        self.assertIs(assessment.action, PositionAction.HOLD)

    def test_the_default_policy_is_inert(self):
        policy = ThesisPolicy()
        self.assertEqual(policy.revision_policy.min_revision_interval,
                         timedelta(0))
        self.assertIsNone(policy.partial_exit_fraction)


class TestRiskUpgrade(unittest.TestCase):
    """§26 — a thesis turns I-D's PARTIAL estimate into a MEASURED one."""

    def test_an_invalidation_level_produces_a_measured_estimate(self):
        estimate = engine().risk_estimate(thesis_of())
        self.assertIs(estimate.status, RiskEstimateStatus.MEASURED)
        self.assertAlmostEqual(estimate.risk_per_dollar, 0.06, places=6)

    def test_without_a_thesis_volatility_gives_only_partial(self):
        from src.adaptive.risk_budget import PositionRiskEstimator
        estimate = PositionRiskEstimator().estimate("i", daily_volatility=0.02)
        self.assertIs(estimate.status, RiskEstimateStatus.PARTIAL)

    def test_a_reference_price_overrides_the_entry_price(self):
        estimate = engine().risk_estimate(thesis_of(), reference_price=120.0)
        self.assertIs(estimate.status, RiskEstimateStatus.MEASURED)
        self.assertGreater(estimate.risk_per_dollar, 0.2)

    def test_no_expected_return_or_confidence_is_invented(self):
        thesis = thesis_of()
        self.assertIsNone(thesis.expected_return)
        self.assertIsNone(thesis.confidence)


class TestRevisionHistory(unittest.TestCase):

    def test_a_full_lifecycle_keeps_every_predecessor(self):
        eng = engine()
        r0 = thesis_of(eng)
        r1, rev1 = eng.apply_partial_exit(
            r0, 0.4, day(4), reason=ThesisRevisionReason.MATERIAL_POSITIVE_EVENT)
        a2 = eng.reassess(r1, day(5), evidence=[evidence("ev-b", at=day(5))],
                          proposed_target=TargetZone(117.0, 122.0))
        r2 = a2.revised_thesis
        a3 = eng.reassess(r2, day(6),
                          evidence=[evidence("ev-c", EvidenceDirection.NEGATIVE,
                                             0.7, at=day(6))],
                          proposed_invalidation=InvalidationZone(103.0, 104.0))
        r3 = a3.revised_thesis
        a4 = eng.reassess(r3, day(7), evidence=[
            evidence("ev-d", EvidenceDirection.NEGATIVE, 0.9, at=day(7),
                     invalidates=True)])
        r4 = a4.revised_thesis

        chain = [r0, r1, r2, r3, r4]
        for earlier, later in zip(chain, chain[1:]):
            self.assertEqual(later.previous_thesis_id, earlier.thesis_id)
            self.assertEqual(later.revision, earlier.revision + 1)
        self.assertAlmostEqual(r4.remaining_fraction, 0.6)
        self.assertAlmostEqual(r4.realized_fraction, 0.4)
        self.assertIs(r4.status, ThesisStatus.INVALIDATED)
        # The original is untouched by everything that followed it.
        self.assertEqual(r0.target_zone.low, 108.0)
        self.assertEqual(r0.remaining_fraction, 1.0)

    def test_revision_ids_are_deterministic(self):
        first = engine().reassess(thesis_of(), day(4), evidence=[evidence()],
                                  proposed_target=TargetZone(117.0, 122.0))
        second = engine().reassess(thesis_of(), day(4), evidence=[evidence()],
                                   proposed_target=TargetZone(117.0, 122.0))
        self.assertEqual(first.revision.revision_id, second.revision.revision_id)
        self.assertEqual(first.revised_thesis.thesis_id,
                         second.revised_thesis.thesis_id)

    def test_evidence_references_accumulate(self):
        assessment = engine().reassess(
            thesis_of(), day(4), evidence=[evidence("ev-1"), evidence("ev-2")],
            proposed_target=TargetZone(117.0, 122.0))
        self.assertEqual(set(assessment.revised_thesis.evidence_references),
                         {"ev-1", "ev-2"})


if __name__ == "__main__":
    unittest.main()
