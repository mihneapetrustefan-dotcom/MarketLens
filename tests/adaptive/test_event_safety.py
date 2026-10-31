"""
tests/adaptive/test_event_safety.py
-----------------------------------------------
Phase 25.9I-F: the three things event intelligence may never invent,
and the one safety rule a cooldown may never suppress.

    a numeric target      section 24
    a horizon             section 26
    an exit fraction      section 25

Each is tested twice: once behaviourally, by running a material
positive event and asserting nothing moved, and once structurally, by
asserting the FIELD does not exist. The structural test is the one that
survives a future refactor -- a behaviour can be changed by accident,
an absent field cannot be filled by accident.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.event_assessment import EventImpactAssessor
from src.adaptive.event_reassessment import (
    EventDrivenReassessmentEngine, ReassessmentPolicy,
)
from src.adaptive.event_relevance import (
    EventRelevanceClassifier, RelevancePolicy,
)
from src.adaptive.event_sources import (
    EventInstrumentResolver, InstrumentResolution,
)
from src.adaptive.thesis import PositionThesisEngine, ThesisPolicy
from src.domain.adaptive_models import MarketDataState
from src.domain.event_position_models import (
    EventEvidenceBundle, EventUrgency, PositionEventImpactAssessment,
    PreEventRiskAdvisory, ThesisRelevance,
)
from src.domain.impact_models import ImpactDirection, ImpactLevel
from src.domain.thesis_models import (
    ExpectedHorizon, InvalidationZone, PositionAction, PositionDirection,
    TargetZone, ThesisStatus,
)

UTC = timezone.utc
ENTRY = datetime(2026, 9, 26, 13, 30, tzinfo=UTC)
DAY4 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
ASSUMPTIONS = {"margin expansion": frozenset({"guidance", "earnings"})}


def _engine():
    classifier = EventRelevanceClassifier(
        EventInstrumentResolver({
            "ent-aa": InstrumentResolution("ent-aa", ("inst-aa",), "chips")}),
        policy=RelevancePolicy(assumption_event_types=ASSUMPTIONS))
    return EventDrivenReassessmentEngine(
        PositionThesisEngine(ThesisPolicy()), classifier,
        EventImpactAssessor(), ReassessmentPolicy())


def _thesis(eng):
    return eng.thesis_engine.open_thesis(
        instrument_id="inst-aa", direction=PositionDirection.LONG,
        as_of=ENTRY, entry_price=100.0, target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(94.0, 96.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=ENTRY),
        entry_notional=1000.0, event_assumptions=("margin expansion",))


def _bundle(**kw):
    base = dict(canonical_event_id="ce-1", event_type="guidance",
                event_category="corporate", as_of=DAY4,
                title="Guidance raised sharply", lifecycle_state="confirmed",
                first_reported_at=DAY4, entity_ids=("ent-aa",),
                primary_entity_id="ent-aa", instrument_ids=("inst-aa",),
                sector_ids=("chips",), total_report_count=3,
                revision_key="rev-1", quality_confidence=0.55,
                report_ids=("r1", "r2", "r3"))
    base.update(kw)
    return EventEvidenceBundle(**base)


class TestTargetFabrication(unittest.TestCase):
    """Section 24 and tests 21-23 -- no target model, so no target."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_a_material_positive_event_leaves_the_target_alone(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0)
        self.assertTrue(outcome.assessment.strengthens_thesis)
        self.assertFalse(outcome.describe()["target_changed"])
        revised = outcome.revised_thesis
        if revised is not None:
            self.assertEqual(revised.target_zone, self.thesis.target_zone)

    def test_it_asks_for_a_re_estimation_instead(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0)
        self.assertTrue(outcome.assessment.target_reestimation_required)

    def test_the_assessment_has_no_field_for_a_target(self):
        fields = set(PositionEventImpactAssessment.__dataclass_fields__)
        for forbidden in ("target_zone", "revised_target_zone", "new_target",
                          "proposed_target", "target_low", "target_high"):
            self.assertNotIn(forbidden, fields)

    def test_an_external_estimate_does_move_the_target(self):
        """The same event, with the numbers supplied from outside."""
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0, proposed_target=TargetZone(117.0, 122.0))
        revised = outcome.revised_thesis
        self.assertIsNotNone(revised)
        self.assertEqual((revised.target_zone.low, revised.target_zone.high),
                         (117.0, 122.0))

    def test_the_engine_did_not_produce_those_numbers(self):
        """117-122 appears nowhere in what the event layer computed."""
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0, proposed_target=TargetZone(117.0, 122.0))
        described = outcome.assessment.describe()
        self.assertNotIn("117", str(described))
        self.assertNotIn("122", str(described))


class TestHorizonFabrication(unittest.TestCase):
    """Section 26 and test 24 -- no horizon model, so no extension."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_a_positive_event_does_not_extend_the_horizon(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0)
        self.assertFalse(outcome.describe()["horizon_changed"])

    def test_it_asks_for_a_re_estimation_instead(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0)
        self.assertTrue(outcome.assessment.horizon_reestimation_required)

    def test_the_assessment_has_no_field_for_a_horizon(self):
        fields = set(PositionEventImpactAssessment.__dataclass_fields__)
        for forbidden in ("horizon", "revised_horizon", "new_horizon",
                          "extra_days", "calendar_days", "trading_days"):
            self.assertNotIn(forbidden, fields)

    def test_an_external_horizon_does_apply(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0,
            proposed_horizon=ExpectedHorizon(calendar_days=12, set_at=DAY4))
        revised = outcome.revised_thesis
        self.assertIsNotNone(revised)
        self.assertEqual(revised.horizon.calendar_days, 12)


class TestPartialExitFractionFabrication(unittest.TestCase):
    """Section 25 and test 25 -- the event layer never invents 40%."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_a_negative_event_asks_for_a_review_without_a_number(self):
        outcome = self.engine.reassess(
            _bundle(title="Guidance cut"), self.thesis, as_of=DAY4,
            subtype="guidance_cut", price=99.0)
        self.assertTrue(outcome.assessment.weakens_thesis)
        self.assertTrue(outcome.assessment.partial_exit_review_required)
        self.assertIsNone(outcome.describe()["partial_exit_fraction"])

    def test_the_assessment_has_no_field_for_a_fraction(self):
        fields = set(PositionEventImpactAssessment.__dataclass_fields__)
        for forbidden in ("exit_fraction", "partial_exit_fraction", "fraction",
                          "reduce_by", "quantity", "size"):
            self.assertNotIn(forbidden, fields)

    def test_an_external_fraction_does_apply(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0, proposed_target=TargetZone(117.0, 122.0),
            exit_fraction=0.40)
        self.assertEqual(outcome.describe()["partial_exit_fraction"], 0.40)

    def test_the_thesis_policy_still_invents_no_default_fraction(self):
        self.assertIsNone(ThesisPolicy().partial_exit_fraction)


class TestInvalidation(unittest.TestCase):
    """Tests 26-29 -- invalidation is evidenced, never asserted."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_a_material_negative_event_can_invalidate(self):
        outcome = self.engine.reassess(
            _bundle(title="Guidance cut; margin thesis contradicted"),
            self.thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=99.0)
        self.assertTrue(outcome.assessment.invalidates_thesis)
        self.assertIs(outcome.assessment.urgency, EventUrgency.CRITICAL)

    def test_invalidation_produces_a_full_exit_recommendation(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=99.0)
        self.assertIs(outcome.action, PositionAction.FULL_EXIT)
        self.assertFalse(outcome.is_executable)

    def test_invalidation_requests_hot_data(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=99.0)
        self.assertIsNotNone(outcome.data_request)
        self.assertIs(outcome.data_request.desired_state, MarketDataState.HOT)

    def test_a_claimed_invalidation_without_direction_is_refused(self):
        outcome = self.engine.reassess(
            _bundle(event_type="acquisition"), self.thesis, as_of=DAY4,
            invalidates_assumption=True, price=99.0)
        self.assertFalse(outcome.assessment.invalidates_thesis)
        self.assertTrue(any("does not meet the bar" in r
                            for r in outcome.assessment.reasons))

    def test_a_sector_event_cannot_invalidate_a_company_thesis(self):
        with self.assertRaises(ValueError):
            from src.domain.event_position_models import (
                EventPositionRelation, PositionEventRelevance,
            )
            PositionEventImpactAssessment(
                assessment_id="pea-x", canonical_event_id="ce", thesis_id="th",
                instrument_id="inst", as_of=DAY4,
                relevance=PositionEventRelevance(
                    canonical_event_id="ce", thesis_id="th",
                    instrument_id="inst",
                    relation=EventPositionRelation.SECTOR),
                direction=ImpactDirection.NEGATIVE,
                materiality=ImpactLevel.HIGH, invalidates_thesis=True)

    def test_a_low_materiality_event_cannot_invalidate(self):
        from src.domain.event_position_models import (
            EventPositionRelation, PositionEventRelevance,
        )
        with self.assertRaises(ValueError):
            PositionEventImpactAssessment(
                assessment_id="pea-y", canonical_event_id="ce", thesis_id="th",
                instrument_id="inst", as_of=DAY4,
                relevance=PositionEventRelevance(
                    canonical_event_id="ce", thesis_id="th",
                    instrument_id="inst",
                    relation=EventPositionRelation.DIRECT),
                direction=ImpactDirection.NEGATIVE,
                materiality=ImpactLevel.LOW, invalidates_thesis=True)

    def test_conflicted_evidence_cannot_invalidate(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_cut",
            opposing_directions=(ImpactDirection.POSITIVE,),
            invalidates_assumption=True, price=99.0)
        self.assertFalse(outcome.assessment.invalidates_thesis)

    def test_an_assessment_cannot_both_strengthen_and_weaken(self):
        from src.domain.event_position_models import PositionEventRelevance
        with self.assertRaises(ValueError):
            PositionEventImpactAssessment(
                assessment_id="pea-z", canonical_event_id="ce", thesis_id="th",
                instrument_id="inst", as_of=DAY4,
                relevance=PositionEventRelevance(
                    canonical_event_id="ce", thesis_id="th",
                    instrument_id="inst"),
                strengthens_thesis=True, weakens_thesis=True)


class TestCooldownVersusSafety(unittest.TestCase):
    """Tests 47-48 and section 46 -- greed is restrained, safety is not."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_a_cooldown_does_not_block_an_invalidating_event(self):
        """A positive revision 10 minutes ago; then the thesis breaks."""
        outcome = self.engine.reassess(
            _bundle(title="Guidance cut; thesis contradicted"), self.thesis,
            as_of=DAY4, subtype="guidance_cut", invalidates_assumption=True,
            price=97.0, since_last_revision=timedelta(minutes=10),
            revisions_today=3)
        self.assertTrue(outcome.assessment.invalidates_thesis)
        self.assertIs(outcome.assessment.urgency, EventUrgency.CRITICAL)
        self.assertIs(outcome.action, PositionAction.FULL_EXIT)
        self.assertIs(outcome.data_request.desired_state, MarketDataState.HOT)

    def test_a_cooldown_does_restrain_a_repeated_upside_revision(self):
        """
        The cooldown is INERT by default -- `RevisionPolicy.cooldown` is
        zero and `max_revisions_per_day` is None, because no calibrated
        value exists. So this test configures one explicitly, which is
        also how a caller would have to.
        """
        from src.domain.thesis_models import RevisionPolicy
        engine = EventDrivenReassessmentEngine(
            PositionThesisEngine(ThesisPolicy(
                revision_policy=RevisionPolicy(
                    cooldown=timedelta(hours=1), max_revisions_per_day=2))),
            self.engine.classifier, EventImpactAssessor(), ReassessmentPolicy())
        thesis = _thesis(engine)
        outcome = engine.reassess(
            _bundle(), thesis, as_of=DAY4, subtype="guidance_raised",
            price=106.0, proposed_target=TargetZone(117.0, 122.0),
            since_last_revision=timedelta(minutes=5), revisions_today=5)
        self.assertIsNot(outcome.action, PositionAction.EXTEND_TARGET)
        self.assertTrue(outcome.thesis_assessment.blocked_by_policy)

    def test_the_same_cooldown_still_does_not_block_invalidation(self):
        from src.domain.thesis_models import RevisionPolicy
        engine = EventDrivenReassessmentEngine(
            PositionThesisEngine(ThesisPolicy(
                revision_policy=RevisionPolicy(
                    cooldown=timedelta(hours=1), max_revisions_per_day=2))),
            self.engine.classifier, EventImpactAssessor(), ReassessmentPolicy())
        thesis = _thesis(engine)
        outcome = engine.reassess(
            _bundle(), thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=97.0,
            since_last_revision=timedelta(minutes=5), revisions_today=5)
        self.assertIs(outcome.action, PositionAction.FULL_EXIT)


class TestNonExecutability(unittest.TestCase):
    """Test 30 -- nothing this phase produces is an instruction."""

    def setUp(self):
        self.engine = _engine()
        self.thesis = _thesis(self.engine)

    def test_every_outcome_is_non_executable(self):
        for kwargs in ({"subtype": "guidance_raised"},
                       {"subtype": "guidance_cut"},
                       {"subtype": "guidance_cut",
                        "invalidates_assumption": True}):
            outcome = self.engine.reassess(
                _bundle(), self.thesis, as_of=DAY4, price=100.0, **kwargs)
            self.assertFalse(outcome.is_executable)
            self.assertFalse(outcome.assessment.is_executable)
            if outcome.reassessment_request:
                self.assertFalse(outcome.reassessment_request.is_executable)

    def test_the_thesis_assessment_is_also_non_executable(self):
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=99.0)
        self.assertFalse(outcome.thesis_assessment.is_executable)

    def test_is_executable_cannot_be_set_to_true(self):
        """It is a property with no setter, not a mutable field."""
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_raised")
        with self.assertRaises(AttributeError):
            outcome.assessment.is_executable = True

    def test_a_hot_request_does_not_make_anything_execution_ready(self):
        """Section 31 -- HOT requested is not realtime available."""
        outcome = self.engine.reassess(
            _bundle(), self.thesis, as_of=DAY4, subtype="guidance_cut",
            invalidates_assumption=True, price=99.0)
        self.assertIs(outcome.data_request.desired_state, MarketDataState.HOT)
        # The request says what is WANTED. 25.9I-C owns what is granted,
        # and this account has never served a realtime marker.
        self.assertFalse(hasattr(outcome.data_request, "granted"))
        self.assertFalse(hasattr(outcome.data_request, "entitlement"))
        self.assertFalse(outcome.is_executable)


class TestFailsSafely(unittest.TestCase):
    """Tests 45-46, 50 -- missing inputs produce refusals, not guesses."""

    def setUp(self):
        self.engine = _engine()

    def test_a_closed_thesis_refuses_reassessment(self):
        thesis = _thesis(self.engine)
        # A CLOSED thesis holds no exposure, which 25.9I-E enforces, so
        # the fixture has to be a genuinely closed one.
        closed = type(thesis)(**{**thesis.__dict__,
                                 "status": ThesisStatus.CLOSED,
                                 "remaining_fraction": 0.0,
                                 "realized_fraction": 1.0})
        outcome = self.engine.reassess(_bundle(), closed, as_of=DAY4,
                                       subtype="guidance_cut")
        self.assertIsNone(outcome.evidence)
        self.assertIsNone(outcome.thesis_assessment)
        self.assertIn("closed", outcome.skipped_reason)

    def test_an_unmapped_instrument_produces_no_evidence(self):
        thesis = self.engine.thesis_engine.open_thesis(
            instrument_id="inst-nowhere", direction=PositionDirection.LONG,
            as_of=ENTRY, entry_price=100.0,
            target_zone=TargetZone(108.0, 112.0),
            invalidation_zone=InvalidationZone(94.0, 96.0),
            horizon=ExpectedHorizon(calendar_days=7, set_at=ENTRY),
            event_assumptions=("margin expansion",))
        outcome = self.engine.reassess(_bundle(), thesis, as_of=DAY4,
                                       subtype="guidance_raised")
        self.assertIsNone(outcome.evidence)
        self.assertIs(outcome.assessment.materiality, ImpactLevel.UNKNOWN)

    def test_unknown_materiality_never_becomes_evidence(self):
        thesis = _thesis(self.engine)
        outcome = self.engine.reassess(
            _bundle(entity_ids=(), primary_entity_id=None, instrument_ids=(),
                    sector_ids=(), event_category="corporate"),
            thesis, as_of=DAY4)
        self.assertIs(outcome.assessment.materiality, ImpactLevel.UNKNOWN)
        self.assertFalse(outcome.assessment.is_actionable_evidence)

    def test_evidence_with_no_materiality_cannot_clear_a_threshold(self):
        from src.domain.thesis_models import EvidenceDirection, ThesisEvidence
        evidence = ThesisEvidence(
            evidence_id="e-1", direction=EvidenceDirection.POSITIVE,
            as_of=DAY4, materiality=None)
        self.assertFalse(evidence.is_material_information)


if __name__ == "__main__":
    unittest.main()
