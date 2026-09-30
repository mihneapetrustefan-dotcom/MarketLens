"""
tests/adaptive/test_event_reassessment.py
-----------------------------------------------------
Phase 25.9I-F: an event reaching a position, and mostly not.

The assertions that matter most are the ones where nothing happens --
five articles producing one reaction, a re-crawl producing none, a
minor headline leaving a thesis alone. A system that reacts is easy;
one that declines to react for a stated reason is the point.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.event_assessment import (
    AssessmentPolicy, EventImpactAssessor, SUBTYPE_DIRECTION,
    assessment_id_for,
)
from src.adaptive.event_reassessment import (
    ConsumptionRecord, EventDrivenReassessmentEngine, ReassessmentPolicy,
    evidence_id_for, suppression_reason,
)
from src.adaptive.event_relevance import (
    EventRelevanceClassifier, RelevancePolicy,
)
from src.adaptive.event_sources import (
    EventInstrumentResolver, InstrumentResolution, order_events,
    revision_key_for,
)
from src.adaptive.thesis import PositionThesisEngine, ThesisPolicy
from src.domain.entity_models import (
    EntityRelationship, ProvenanceKind, RelationshipType,
)
from src.domain.event_models import ParticipationRole
from src.domain.event_position_models import (
    DataIntegrityConcern, EventConsumptionStatus, EventEvidenceBundle,
    EventPositionRelation, EventUrgency, EvidenceConflictState,
    ThesisRelevance,
)
from src.domain.fusion_models import CorroborationState
from src.domain.impact_models import ImpactDirection, ImpactLevel
from src.domain.thesis_models import (
    EvidenceDirection, ExpectedHorizon, InvalidationZone, PositionAction,
    PositionDirection, TargetZone, ThesisStatus,
)

UTC = timezone.utc
ENTRY = datetime(2026, 9, 26, 13, 30, tzinfo=UTC)
DAY4 = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)

ASSUMPTIONS = {"margin expansion": frozenset({"guidance", "earnings"})}


def resolver(**extra):
    base = {"ent-aa": InstrumentResolution("ent-aa", ("inst-aa",), "chips"),
            "ent-bb": InstrumentResolution("ent-bb", ("inst-bb",), "chips"),
            "ent-far": InstrumentResolution("ent-far", ("inst-far",), "retail")}
    base.update(extra)
    return EventInstrumentResolver(base)


def engine(assumptions=ASSUMPTIONS, relationships=None,
           stale_after_seconds=None, **policy):
    classifier = EventRelevanceClassifier(
        resolver(), relationship_graph=relationships,
        policy=RelevancePolicy(assumption_event_types=assumptions))
    return EventDrivenReassessmentEngine(
        PositionThesisEngine(ThesisPolicy()), classifier,
        EventImpactAssessor(AssessmentPolicy(
            stale_after_seconds=stale_after_seconds)),
        ReassessmentPolicy(**policy))


def thesis_for(eng, instrument_id="inst-aa", assumptions=("margin expansion",)):
    return eng.thesis_engine.open_thesis(
        instrument_id=instrument_id, direction=PositionDirection.LONG,
        as_of=ENTRY, entry_price=100.0, target_zone=TargetZone(108.0, 112.0),
        invalidation_zone=InvalidationZone(94.0, 96.0),
        horizon=ExpectedHorizon(calendar_days=7, set_at=ENTRY),
        entry_notional=1000.0, event_assumptions=tuple(assumptions))


def bundle(**kw):
    base = dict(canonical_event_id="ce-1", event_type="guidance",
                event_category="corporate", as_of=DAY4, title="Guidance",
                lifecycle_state="confirmed", first_reported_at=DAY4,
                entity_ids=("ent-aa",), primary_entity_id="ent-aa",
                instrument_ids=("inst-aa",), sector_ids=("chips",),
                total_report_count=1, revision_key="rev-1",
                quality_confidence=0.55, report_ids=("rep-1",))
    base.update(kw)
    return EventEvidenceBundle(**base)


class TestEventToPositionMapping(unittest.TestCase):
    """Tests 1-4 -- how an event reaches a position, and how it does not."""

    def setUp(self):
        self.classifier = EventRelevanceClassifier(
            resolver(), policy=RelevancePolicy(assumption_event_types=ASSUMPTIONS))

    def test_a_primary_participant_is_a_direct_issuer_match(self):
        relation, role, evidence = self.classifier.classify_relation(
            bundle(), instrument_id="inst-aa")
        self.assertIs(relation, EventPositionRelation.DIRECT)
        self.assertIs(role, ParticipationRole.PRIMARY)
        self.assertTrue(relation.is_issuer)

    def test_an_unresolved_entity_fails_safely(self):
        relation, role, _ = self.classifier.classify_relation(
            bundle(entity_ids=("ent-nobody",), primary_entity_id="ent-nobody",
                   instrument_ids=(), sector_ids=()),
            instrument_id="inst-aa")
        self.assertIs(relation, EventPositionRelation.UNKNOWN)
        self.assertIsNone(role)

    def test_an_unrelated_instrument_is_unknown_not_indirect(self):
        relation, _, _ = self.classifier.classify_relation(
            bundle(), instrument_id="inst-far")
        self.assertIs(relation, EventPositionRelation.UNKNOWN)

    def test_indirect_requires_a_sourced_relationship(self):
        class Graph:
            def __init__(self, edges):
                self.edges = edges

            def get_relationships(self, entity_id):
                return self.edges

        sourced = EntityRelationship(
            from_entity_id="ent-far", to_entity_id="ent-aa",
            relationship_type=RelationshipType.SUPPLIER_OF,
            source="10-K filing", provenance_kind=ProvenanceKind.SOURCED_FACT)
        classifier = EventRelevanceClassifier(
            resolver(), relationship_graph=Graph([sourced]),
            policy=RelevancePolicy(assumption_event_types=ASSUMPTIONS))
        relation, _, evidence = classifier.classify_relation(
            bundle(), instrument_id="inst-far", entity_id="ent-far")
        self.assertIs(relation, EventPositionRelation.INDIRECT)
        self.assertIn("10-K filing", evidence)

    def test_an_inferred_relationship_is_not_enough_for_indirect(self):
        class Graph:
            def get_relationships(self, entity_id):
                return [EntityRelationship(
                    from_entity_id="ent-far", to_entity_id="ent-aa",
                    relationship_type=RelationshipType.SUPPLIER_OF,
                    source="derived", provenance_kind=ProvenanceKind.INFERRED)]

        classifier = EventRelevanceClassifier(
            resolver(), relationship_graph=Graph(),
            policy=RelevancePolicy(assumption_event_types=ASSUMPTIONS))
        relation, _, _ = classifier.classify_relation(
            bundle(), instrument_id="inst-far", entity_id="ent-far")
        self.assertIsNot(relation, EventPositionRelation.INDIRECT)

    def test_sector_relation_stays_sector(self):
        relation, _, _ = self.classifier.classify_relation(
            bundle(entity_ids=(), primary_entity_id=None, instrument_ids=()),
            instrument_id="inst-bb", sector_id="chips")
        self.assertIs(relation, EventPositionRelation.SECTOR)
        self.assertFalse(relation.is_issuer)

    def test_macro_events_are_market_wide(self):
        relation, _, _ = self.classifier.classify_relation(
            bundle(event_category="macro", event_type="inflation",
                   entity_ids=(), primary_entity_id=None, instrument_ids=(),
                   sector_ids=()),
            instrument_id="inst-aa")
        self.assertIs(relation, EventPositionRelation.MARKET_WIDE)

    def test_a_referenced_mention_is_not_participation(self):
        relation, _, reason = self.classifier.classify_relation(
            bundle(entity_ids=("ent-bb",), primary_entity_id=None),
            instrument_id="inst-bb",
            participant_roles={"ent-bb": "referenced"})
        self.assertIs(relation, EventPositionRelation.UNKNOWN)
        self.assertIn("not", reason)


class TestThesisRelevance(unittest.TestCase):
    """Section 6 -- company relevance is not thesis relevance."""

    def test_an_event_on_a_stated_assumption_is_highly_relevant(self):
        eng = engine()
        thesis = thesis_for(eng)
        relevance = eng.classifier.classify(bundle(), thesis)
        self.assertIs(relevance.thesis_relevance, ThesisRelevance.HIGH)
        self.assertEqual(relevance.affected_assumptions, ("margin expansion",))

    def test_an_event_on_no_stated_assumption_is_not_relevant(self):
        eng = engine()
        thesis = thesis_for(eng)
        relevance = eng.classifier.classify(
            bundle(event_type="product_launch"), thesis)
        self.assertIs(relevance.thesis_relevance, ThesisRelevance.NOT_RELEVANT)
        self.assertFalse(relevance.warrants_reassessment)

    def test_without_a_mapping_relevance_is_unknown_not_invented(self):
        eng = engine(assumptions={})
        thesis = thesis_for(eng)
        relevance = eng.classifier.classify(bundle(), thesis)
        self.assertIs(relevance.thesis_relevance, ThesisRelevance.UNKNOWN)

    def test_unknown_relevance_still_warrants_a_look_for_a_direct_event(self):
        """UNKNOWN and NOT_RELEVANT must not collapse into each other."""
        eng = engine(assumptions={})
        thesis = thesis_for(eng)
        relevance = eng.classifier.classify(bundle(), thesis)
        self.assertFalse(relevance.is_relevant_to_thesis)
        self.assertTrue(relevance.warrants_reassessment)

    def test_a_thesis_with_no_assumptions_is_unknown(self):
        eng = engine()
        thesis = thesis_for(eng, assumptions=())
        relevance = eng.classifier.classify(bundle(), thesis)
        self.assertIs(relevance.thesis_relevance, ThesisRelevance.UNKNOWN)


class TestDirection(unittest.TestCase):
    """Tests 5-9 -- direction is read from evidence or left unknown."""

    def setUp(self):
        self.assessor = EventImpactAssessor()

    def test_a_canonical_subtype_states_direction(self):
        for subtype, expected in SUBTYPE_DIRECTION.items():
            direction, reasons = self.assessor.direction_for(
                bundle(), subtype=subtype)
            self.assertIs(direction, expected, subtype)
            self.assertTrue(any("subtype" in r for r in reasons))

    def test_no_subtype_means_unknown_not_a_guess(self):
        direction, _ = self.assessor.direction_for(
            bundle(event_type="acquisition"))
        self.assertIs(direction, ImpactDirection.UNKNOWN)

    def test_an_acquisition_is_not_assumed_bullish(self):
        direction, _ = self.assessor.direction_for(
            bundle(event_type="acquisition", title="Company agrees to acquire"))
        self.assertIsNot(direction, ImpactDirection.POSITIVE)

    def test_layoffs_are_not_assumed_bearish(self):
        direction, _ = self.assessor.direction_for(
            bundle(event_type="layoffs", title="Company cuts 5% of staff"))
        self.assertIsNot(direction, ImpactDirection.NEGATIVE)

    def test_a_caller_may_supply_a_direction(self):
        direction, _ = self.assessor.direction_for(
            bundle(event_type="acquisition"),
            supplied=ImpactDirection.NEGATIVE)
        self.assertIs(direction, ImpactDirection.NEGATIVE)

    def test_a_caller_disagreeing_with_the_subtype_yields_mixed(self):
        direction, reasons = self.assessor.direction_for(
            bundle(), subtype="guidance_cut",
            supplied=ImpactDirection.POSITIVE)
        self.assertIs(direction, ImpactDirection.MIXED)
        self.assertTrue(any("wins" in r for r in reasons))

    def test_a_withdrawn_event_carries_no_direction(self):
        for state in ("retracted", "denied", "cancelled"):
            direction, _ = self.assessor.direction_for(
                bundle(lifecycle_state=state), subtype="guidance_raised")
            self.assertIs(direction, ImpactDirection.NEUTRAL, state)

    def test_mixed_and_unknown_reach_the_thesis_as_neutral(self):
        """25.9I-E has three directions; MIXED must not become POSITIVE."""
        eng = engine()
        thesis = thesis_for(eng)
        for supplied in (ImpactDirection.MIXED, ImpactDirection.UNKNOWN):
            relevance = eng.classifier.classify(bundle(), thesis)
            assessment = eng.assessor.assess(
                bundle(), thesis, relevance, as_of=DAY4,
                supplied_direction=supplied)
            self.assertIs(assessment.evidence_direction(),
                          EvidenceDirection.NEUTRAL)


class TestMateriality(unittest.TestCase):
    """Tests 10-12 -- a band from evidence, never a fabricated number."""

    def setUp(self):
        self.assessor = EventImpactAssessor()

    def test_an_unrelated_event_has_unknown_materiality(self):
        band, _ = self.assessor.materiality_for(
            bundle(), relation=EventPositionRelation.UNKNOWN,
            thesis_relevance=ThesisRelevance.UNKNOWN,
            direction=ImpactDirection.UNKNOWN)
        self.assertIs(band, ImpactLevel.UNKNOWN)

    def test_unknown_materiality_carries_no_score(self):
        from src.adaptive.event_assessment import MATERIALITY_SCORE
        self.assertIsNone(MATERIALITY_SCORE[ImpactLevel.UNKNOWN])

    def test_a_direct_event_on_a_stated_assumption_is_high(self):
        band, _ = self.assessor.materiality_for(
            bundle(), relation=EventPositionRelation.DIRECT,
            thesis_relevance=ThesisRelevance.HIGH,
            direction=ImpactDirection.POSITIVE, subtype="guidance_raised")
        self.assertIs(band, ImpactLevel.HIGH)

    def test_a_sector_event_cannot_reach_high_on_its_own(self):
        band, _ = self.assessor.materiality_for(
            bundle(), relation=EventPositionRelation.SECTOR,
            thesis_relevance=ThesisRelevance.HIGH,
            direction=ImpactDirection.NEGATIVE)
        self.assertIsNot(band, ImpactLevel.HIGH)

    def test_a_rumour_weighs_less_than_a_filing(self):
        common = dict(relation=EventPositionRelation.DIRECT,
                      thesis_relevance=ThesisRelevance.HIGH,
                      direction=ImpactDirection.UNKNOWN)
        rumoured, _ = self.assessor.materiality_for(
            bundle(lifecycle_state="rumored"), **common)
        confirmed, _ = self.assessor.materiality_for(
            bundle(lifecycle_state="confirmed"), **common)
        self.assertIs(rumoured, ImpactLevel.MEDIUM)
        self.assertIs(confirmed, ImpactLevel.HIGH)

    def test_an_event_touching_nothing_is_low(self):
        band, _ = self.assessor.materiality_for(
            bundle(event_type="product_launch"),
            relation=EventPositionRelation.DIRECT,
            thesis_relevance=ThesisRelevance.NOT_RELEVANT,
            direction=ImpactDirection.UNKNOWN)
        self.assertIs(band, ImpactLevel.LOW)

    def test_a_caller_may_supply_materiality(self):
        band, _ = self.assessor.materiality_for(
            bundle(), relation=EventPositionRelation.DIRECT,
            thesis_relevance=ThesisRelevance.HIGH,
            direction=ImpactDirection.POSITIVE,
            supplied=ImpactLevel.LOW)
        self.assertIs(band, ImpactLevel.LOW)

    def test_confidence_is_never_collapsed_into_one_number(self):
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(bundle(), thesis, as_of=DAY4,
                               subtype="guidance_raised")
        confidence = outcome.assessment.confidence
        described = confidence.describe()
        self.assertIn("entity_match", described)
        self.assertIn("event_extraction", described)
        # overall() is the weakest known axis, not an average.
        known = [described[a] for a in confidence.known_axes]
        if known:
            self.assertEqual(confidence.overall(), min(known))

    def test_unmeasured_confidence_stays_none(self):
        from src.domain.event_position_models import EventAssessmentConfidence
        self.assertIsNone(EventAssessmentConfidence().overall())
        self.assertEqual(EventAssessmentConfidence().completeness, 0.0)


class TestCorroboration(unittest.TestCase):
    """Tests 13-15 -- syndication is not confirmation."""

    def test_multiple_reports_do_not_fake_independence(self):
        many = bundle(total_report_count=7,
                      report_ids=tuple(f"r{n}" for n in range(7)))
        self.assertTrue(many.is_multi_report)
        self.assertFalse(many.source_independence_known)
        self.assertFalse(many.is_independently_corroborated)

    def test_a_report_count_alone_does_not_raise_materiality(self):
        assessor = EventImpactAssessor()
        common = dict(relation=EventPositionRelation.SECTOR,
                      thesis_relevance=ThesisRelevance.MEDIUM,
                      direction=ImpactDirection.NEGATIVE)
        one, _ = assessor.materiality_for(bundle(total_report_count=1), **common)
        many, _ = assessor.materiality_for(bundle(total_report_count=9), **common)
        self.assertIs(one, many)

    def test_established_independence_does_raise_it(self):
        corroborated = bundle(
            total_report_count=4, independent_source_count=3,
            source_independence_known=True,
            corroboration_state=CorroborationState.INDEPENDENTLY_CORROBORATED)
        self.assertTrue(corroborated.is_independently_corroborated)

    def test_a_count_without_established_independence_is_refused(self):
        with self.assertRaises(ValueError):
            bundle(independent_source_count=3, source_independence_known=False)


class TestNoveltyAndRevision(unittest.TestCase):
    """Tests 15-18 -- one event, one reaction; a real revision may re-trigger."""

    def setUp(self):
        self.engine = engine()
        self.thesis = thesis_for(self.engine)

    def _history(self, event, assessment_id="pea-x", **overrides):
        record = dict(
            canonical_event_id=event.canonical_event_id,
            thesis_id=self.thesis.thesis_id, revision_key=event.revision_key,
            assessment_id=assessment_id, assessed_at=DAY4,
            lifecycle_state=event.lifecycle_state,
            corroboration_state=event.corroboration_state.value,
            total_report_count=event.total_report_count,
            has_contradictions=event.has_contradictions)
        record.update(overrides)
        return [ConsumptionRecord(**record)]

    def test_a_first_sighting_is_new(self):
        status, prior = self.engine.consumption_status(bundle(), self.thesis, ())
        self.assertIs(status, EventConsumptionStatus.NEW)
        self.assertIsNone(prior)

    def test_the_same_revision_is_already_assessed(self):
        event = bundle()
        status, _ = self.engine.consumption_status(
            event, self.thesis, self._history(event))
        self.assertIs(status, EventConsumptionStatus.ALREADY_ASSESSED)
        self.assertFalse(status.permits_reassessment)

    def test_more_syndicated_copies_are_only_an_update(self):
        first = bundle(total_report_count=2, revision_key="rev-a")
        later = bundle(total_report_count=9, revision_key="rev-b")
        status, _ = self.engine.consumption_status(
            later, self.thesis, self._history(first))
        self.assertIs(status, EventConsumptionStatus.UPDATED)
        self.assertFalse(status.permits_reassessment)

    def test_a_lifecycle_change_is_a_material_revision(self):
        rumour = bundle(lifecycle_state="rumored", revision_key="rev-a")
        filed = bundle(lifecycle_state="confirmed", revision_key="rev-b")
        status, prior = self.engine.consumption_status(
            filed, self.thesis, self._history(rumour))
        self.assertIs(status, EventConsumptionStatus.MATERIAL_REVISION)
        self.assertTrue(status.permits_reassessment)
        self.assertIsNotNone(prior)

    def test_a_contradiction_appearing_is_a_material_revision(self):
        clean = bundle(revision_key="rev-a")
        contradicted = bundle(revision_key="rev-b", has_contradictions=True)
        status, _ = self.engine.consumption_status(
            contradicted, self.thesis, self._history(clean))
        self.assertIs(status, EventConsumptionStatus.MATERIAL_REVISION)

    def test_a_second_reaction_is_refused_end_to_end(self):
        event = bundle()
        outcome = self.engine.reassess(
            event, self.thesis, as_of=DAY4, subtype="guidance_raised",
            history=self._history(event))
        self.assertIsNone(outcome.evidence)
        self.assertIsNone(outcome.thesis_assessment)
        self.assertIn("no second reaction", outcome.skipped_reason)

    def test_a_material_revision_links_to_its_predecessor(self):
        rumour = bundle(lifecycle_state="rumored", revision_key="rev-a")
        filed = bundle(lifecycle_state="confirmed", revision_key="rev-b")
        outcome = self.engine.reassess(
            filed, self.thesis, as_of=DAY4, subtype="guidance_raised",
            history=self._history(rumour, assessment_id="pea-first"))
        self.assertEqual(outcome.assessment.previous_assessment_id, "pea-first")
        self.assertIsNotNone(outcome.evidence)

    def test_the_revision_key_changes_only_on_material_fields(self):
        same = revision_key_for(("ce", "reported", "single_source", 1, 0,
                                 False, "", None))
        again = revision_key_for(("ce", "reported", "single_source", 1, 0,
                                  False, "", None))
        different = revision_key_for(("ce", "confirmed", "single_source", 1, 0,
                                      False, "", None))
        self.assertEqual(same, again)
        self.assertNotEqual(same, different)


class TestFreshness(unittest.TestCase):
    """Test 18 -- reprocessing does not manufacture urgency."""

    def test_an_old_event_is_stale_and_not_urgent(self):
        eng = engine(stale_after_seconds=48 * 3600)
        thesis = thesis_for(eng)
        old = bundle(first_reported_at=DAY4 - timedelta(days=9))
        outcome = eng.reassess(old, thesis, as_of=DAY4,
                               subtype="guidance_cut", price=99.0)
        self.assertTrue(outcome.assessment.is_stale)
        self.assertIs(outcome.assessment.urgency, EventUrgency.NORMAL)

    def test_a_stale_event_does_not_request_hot(self):
        eng = engine(stale_after_seconds=48 * 3600)
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(first_reported_at=DAY4 - timedelta(days=30)), thesis,
            as_of=DAY4, subtype="guidance_cut", invalidates_assumption=True)
        self.assertIsNone(outcome.data_request)

    def test_without_a_configured_window_nothing_is_stale(self):
        self.assertIsNone(AssessmentPolicy().stale_after_seconds)
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(first_reported_at=DAY4 - timedelta(days=400)), thesis,
            as_of=DAY4, subtype="guidance_raised")
        self.assertFalse(outcome.assessment.is_stale)

    def test_occurrence_falls_back_to_first_report_and_says_so(self):
        event = bundle(event_time=None, first_reported_at=DAY4)
        self.assertEqual(event.known_occurrence_at, DAY4)
        self.assertTrue(event.occurrence_is_inferred)
        self.assertIn(DataIntegrityConcern.MISSING_EVENT_TIME,
                      bundle(integrity_concerns=(
                          DataIntegrityConcern.MISSING_EVENT_TIME,)
                      ).integrity_concerns)


class TestConflict(unittest.TestCase):
    """Tests 7, 32 -- conflict is reported, never voted on."""

    def setUp(self):
        self.assessor = EventImpactAssessor()

    def test_opposing_evidence_is_conflicted(self):
        state, _ = self.assessor.conflict_for(
            bundle(), ImpactDirection.POSITIVE,
            opposing_directions=(ImpactDirection.NEGATIVE,))
        self.assertIs(state, EvidenceConflictState.CONFLICTED)

    def test_conflict_is_not_resolved_by_majority(self):
        state, reasons = self.assessor.conflict_for(
            bundle(), ImpactDirection.POSITIVE,
            opposing_directions=(ImpactDirection.POSITIVE,
                                 ImpactDirection.POSITIVE,
                                 ImpactDirection.NEGATIVE))
        self.assertIs(state, EvidenceConflictState.CONFLICTED)
        self.assertTrue(any("counting" in r for r in reasons))

    def test_a_recorded_contradiction_is_conflicted(self):
        state, _ = self.assessor.conflict_for(
            bundle(has_contradictions=True), ImpactDirection.POSITIVE)
        self.assertIs(state, EvidenceConflictState.CONFLICTED)

    def test_conflicted_evidence_does_not_strengthen_a_thesis(self):
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(), thesis, as_of=DAY4, subtype="guidance_raised",
            opposing_directions=(ImpactDirection.NEGATIVE,))
        self.assertIs(outcome.assessment.conflict_state,
                      EvidenceConflictState.CONFLICTED)
        self.assertFalse(outcome.assessment.strengthens_thesis)
        self.assertFalse(outcome.assessment.target_reestimation_required)

    def test_conflicted_material_evidence_raises_urgency(self):
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(), thesis, as_of=DAY4, subtype="guidance_raised",
            opposing_directions=(ImpactDirection.NEGATIVE,))
        self.assertIs(outcome.assessment.urgency, EventUrgency.ELEVATED)

    def test_no_directional_evidence_is_insufficient_not_neutral(self):
        state, _ = self.assessor.conflict_for(bundle(), ImpactDirection.UNKNOWN)
        self.assertIs(state, EvidenceConflictState.INSUFFICIENT)


class TestDataIntegrity(unittest.TestCase):
    """Section 33 -- our own data problems are not bearish news."""

    def test_an_unresolved_entity_does_not_become_negative(self):
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(), thesis, as_of=DAY4,
            extra_integrity_concerns=(DataIntegrityConcern.STALE_QUOTE,))
        self.assertTrue(outcome.assessment.data_integrity_review_required)
        self.assertIsNot(outcome.assessment.direction, ImpactDirection.NEGATIVE)

    def test_integrity_concerns_are_carried_not_dropped(self):
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(), thesis, as_of=DAY4,
            extra_integrity_concerns=(
                DataIntegrityConcern.TIMESTAMP_CONFLICT,))
        self.assertIn(DataIntegrityConcern.TIMESTAMP_CONFLICT,
                      outcome.assessment.integrity_concerns)

    def test_a_missing_event_time_alone_is_not_a_review_trigger(self):
        """It is true of all 581 events here; treating it as a fault
        would flag the entire corpus and mean nothing."""
        eng = engine()
        thesis = thesis_for(eng)
        outcome = eng.reassess(
            bundle(integrity_concerns=(
                DataIntegrityConcern.MISSING_EVENT_TIME,)),
            thesis, as_of=DAY4, subtype="guidance_raised")
        self.assertFalse(outcome.assessment.data_integrity_review_required)


class TestDeterminism(unittest.TestCase):
    """Tests 39-40 -- same inputs, same ids, same order."""

    def test_assessment_ids_are_content_derived(self):
        first = assessment_id_for("ce-1", "rev-1", "th-1", DAY4)
        again = assessment_id_for("ce-1", "rev-1", "th-1", DAY4)
        other = assessment_id_for("ce-1", "rev-2", "th-1", DAY4)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_evidence_ids_are_content_derived(self):
        self.assertEqual(evidence_id_for("ce", "rev", "th"),
                         evidence_id_for("ce", "rev", "th"))
        self.assertNotEqual(evidence_id_for("ce", "rev", "th"),
                            evidence_id_for("ce", "rev2", "th"))

    def test_the_same_inputs_produce_the_same_assessment(self):
        first = engine().reassess(bundle(), thesis_for(engine()), as_of=DAY4,
                                  subtype="guidance_raised")
        second = engine().reassess(bundle(), thesis_for(engine()), as_of=DAY4,
                                   subtype="guidance_raised")
        self.assertEqual(first.assessment.assessment_id,
                         second.assessment.assessment_id)
        self.assertEqual(first.assessment.direction, second.assessment.direction)
        self.assertEqual(first.assessment.materiality,
                         second.assessment.materiality)

    def test_event_ordering_does_not_depend_on_insertion_order(self):
        early = bundle(canonical_event_id="ce-b",
                       first_reported_at=DAY4 - timedelta(hours=2))
        late = bundle(canonical_event_id="ce-a", first_reported_at=DAY4)
        self.assertEqual([b.canonical_event_id for b in order_events([late, early])],
                         [b.canonical_event_id for b in order_events([early, late])])

    def test_equal_timestamps_break_ties_on_id(self):
        first = bundle(canonical_event_id="ce-b")
        second = bundle(canonical_event_id="ce-a")
        ordered = order_events([first, second])
        self.assertEqual([b.canonical_event_id for b in ordered],
                         ["ce-a", "ce-b"])

    def test_an_undated_event_sorts_last_not_first(self):
        dated = bundle(canonical_event_id="ce-dated", first_reported_at=DAY4)
        undated = bundle(canonical_event_id="ce-undated",
                         first_reported_at=None)
        ordered = order_events([undated, dated])
        self.assertEqual(ordered[-1].canonical_event_id, "ce-undated")


class TestMultiPosition(unittest.TestCase):
    """Tests 33-35 -- one event, several theses, no bulk action."""

    def setUp(self):
        self.engine = engine(assumptions={
            "export access": frozenset({"regulatory_restriction"})})
        self.theses = [thesis_for(self.engine, instrument_id=i,
                                  assumptions=("export access",))
                       for i in ("inst-aa", "inst-bb")]
        self.event = bundle(event_type="regulatory_restriction",
                            entity_ids=(), primary_entity_id=None,
                            instrument_ids=(), sector_ids=("chips",))

    def test_every_thesis_is_assessed_independently(self):
        report, outcomes = self.engine.reassess_many(
            self.event, self.theses, as_of=DAY4,
            sector_ids={"inst-aa": "chips", "inst-bb": "chips"},
            supplied_direction=ImpactDirection.NEGATIVE)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len({o.assessment.thesis_id for o in outcomes}), 2)
        self.assertEqual(len(report.assessments), 2)

    def test_there_is_no_portfolio_wide_recommendation(self):
        report, _ = self.engine.reassess_many(
            self.event, self.theses, as_of=DAY4,
            sector_ids={"inst-aa": "chips", "inst-bb": "chips"})
        for forbidden in ("recommendation", "action", "exit_fraction",
                          "portfolio_action"):
            self.assertFalse(hasattr(report, forbidden), forbidden)
        self.assertFalse(report.is_executable)

    def test_aggregate_exposure_is_a_diagnostic(self):
        report, _ = self.engine.reassess_many(
            self.event, self.theses, as_of=DAY4,
            sector_ids={"inst-aa": "chips", "inst-bb": "chips"},
            exposures={"inst-aa": 1000.0, "inst-bb": 500.0})
        self.assertEqual(report.aggregate_notional, 1500.0)

    def test_multi_position_order_is_deterministic(self):
        first, _ = self.engine.reassess_many(
            self.event, self.theses, as_of=DAY4)
        second, _ = self.engine.reassess_many(
            self.event, list(reversed(self.theses)), as_of=DAY4)
        self.assertEqual([a.thesis_id for a in first.assessments],
                         [a.thesis_id for a in second.assessments])


class TestScheduledEvents(unittest.TestCase):
    """Tests 19-20 -- advisory, never action."""

    def setUp(self):
        self.engine = engine()
        self.thesis = thesis_for(self.engine)

    def test_an_advisory_is_produced_for_an_upcoming_event(self):
        advisory = self.engine.pre_event_advisory(
            self.thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 + timedelta(hours=18))
        self.assertIsNotNone(advisory)
        self.assertTrue(advisory.recommend_thesis_review)
        self.assertFalse(advisory.is_executable)

    def test_an_advisory_cannot_express_a_trade(self):
        advisory = self.engine.pre_event_advisory(
            self.thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 + timedelta(hours=18))
        for forbidden in ("exit_fraction", "reduce_by", "target_zone",
                          "quantity", "stop_price", "action"):
            self.assertFalse(hasattr(advisory, forbidden), forbidden)

    def test_a_past_event_produces_no_advisory(self):
        self.assertIsNone(self.engine.pre_event_advisory(
            self.thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 - timedelta(hours=1)))

    def test_an_event_inside_the_horizon_asks_for_fresher_data(self):
        advisory = self.engine.pre_event_advisory(
            self.thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 + timedelta(hours=12))
        from src.domain.adaptive_models import MarketDataState
        self.assertIs(advisory.requested_market_data_state,
                      MarketDataState.WARM)
        self.assertTrue(advisory.recommend_risk_review)

    def test_a_trading_day_horizon_is_still_understood(self):
        """`ExpectedHorizon(7)` is SEVEN TRADING DAYS with no calendar
        figure; reading calendar_days alone would drop the horizon."""
        thesis = self.engine.thesis_engine.open_thesis(
            instrument_id="inst-aa", direction=PositionDirection.LONG,
            as_of=ENTRY, entry_price=100.0,
            target_zone=TargetZone(108.0, 112.0),
            invalidation_zone=InvalidationZone(94.0, 96.0),
            horizon=ExpectedHorizon(trading_days=7, set_at=ENTRY))
        advisory = self.engine.pre_event_advisory(
            thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 + timedelta(hours=12))
        self.assertIsNotNone(advisory.horizon_days_remaining)

    def test_gap_risk_is_reported_not_hedged(self):
        advisory = self.engine.pre_event_advisory(
            self.thesis, as_of=DAY4, event_type="earnings",
            scheduled_for=DAY4 + timedelta(hours=18), gap_risk_relevant=True)
        self.assertTrue(advisory.gap_risk_relevant)
        self.assertTrue(advisory.recommend_risk_review)
        self.assertFalse(advisory.is_executable)


class TestFlags(unittest.TestCase):
    """Tests 55-56 -- off by default, dependency enforced."""

    def test_the_flag_is_off_by_default(self):
        from src.adaptive import flags as adaptive_flags
        resolved = adaptive_flags.resolve({})
        self.assertFalse(resolved.event_driven_position_reassessment_enabled)

    def test_reassessment_requires_the_thesis_flag(self):
        from src.adaptive import flags as adaptive_flags
        self.assertEqual(
            adaptive_flags.FLAG_DEPENDENCIES[
                "event_driven_position_reassessment_enabled"],
            "position_thesis_enabled")

    def test_suppression_is_reported_not_discovered_at_runtime(self):
        class Flags:
            event_driven_position_reassessment_enabled = True
            position_thesis_enabled = False
        self.assertIn("position_thesis_enabled", suppression_reason(Flags()))

        class Both:
            event_driven_position_reassessment_enabled = True
            position_thesis_enabled = True
        self.assertEqual(suppression_reason(Both()), "")

    def test_the_flag_off_is_reported_as_the_reason(self):
        class Off:
            event_driven_position_reassessment_enabled = False
            position_thesis_enabled = True
        self.assertIn("is off", suppression_reason(Off()))


if __name__ == "__main__":
    unittest.main()
