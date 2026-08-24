"""
tests/fusion/test_fusion_engine.py
---------------------------------------
Tests for the Phase 5 fusion pipeline.

Includes EVERY critical failure case from spec §39 — the cases where a
wrong answer would silently corrupt the factual record — plus the
scenario list from §38. False merges are tested aggressively.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.fusion_models import (
    EventReport, CanonicalEvent, FusionDecisionState, SourceCategory, SourceLineage,
    LineageRelation, CorroborationState, ContradictionType, EventLifecycleState,
    EventRelationType, is_valid_transition, ConsolidatedAttribute, AttributeValue,
)
from src.domain.event_models import (
    StructuredEvent, EventParticipant, ParticipationRole, EventEvidence,
    EventConfidence, EventGeography, ExtractionTier,
)
from src.events.taxonomy import EventType, category_for
from src.fusion.engine import FusionEngine
from src.fusion.blocking import BlockingIndex, blocking_keys_for_report
from src.fusion import scoring
from src.fusion import corroboration as corr
from src.fusion.clustering import ClusterEngine

JAN = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
FEB = datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc)


def make_report(report_id, event_type=EventType.PARTNERSHIP, entity_ids=("nvidia", "tsmc"),
                moment=JAN, title="NVIDIA expands AI chip partnership with TSMC",
                source_name="Reuters", source_category=SourceCategory.MAJOR_FINANCIAL_PRESS,
                lineage_relation=None, attributes=None, description=None, geography=None):
    se = StructuredEvent(
        event_id=f"evt-{report_id}", event_type=event_type, category=category_for(event_type),
        title=title, description=description or f"Reported: {title}",
        participants=[EventParticipant(entity_id=e,
                                        role=ParticipationRole.PRIMARY if i == 0 else ParticipationRole.SECONDARY,
                                        resolution_confidence=0.9)
                       for i, e in enumerate(entity_ids)],
        geography=geography,
        publication_time=moment, ingestion_time=moment, detection_time=moment,
        evidence=[EventEvidence(article_id=f"a-{report_id}", source_name=source_name, published_at=moment)],
        confidence=EventConfidence(extraction_certainty=0.8, entity_resolution_confidence=0.9,
                                    source_quality=0.8, temporal_certainty=0.5),
        attributes=attributes or {}, created_at=moment,
    )
    lineage = SourceLineage(report_id=report_id, relation=lineage_relation,
                             observed_at=moment) if lineage_relation else None
    return EventReport(report_id=report_id, structured_event=se, source_category=source_category,
                        lineage=lineage, created_at=moment)


class TestCriticalFailureCases(unittest.TestCase):
    """Spec §39 — the six cases where a wrong answer corrupts the record."""

    def setUp(self):
        self.engine = FusionEngine()

    def test_case_1_different_targets_different_months_must_not_merge(self):
        """X acquires Y in January vs X acquires Z in February — MUST stay separate."""
        jan = make_report("r1", EventType.ACQUISITION, ("companyx", "companyy"), JAN,
                           title="Company X to acquire Company Y")
        feb = make_report("r2", EventType.ACQUISITION, ("companyx", "companyz"), FEB,
                           title="Company X to acquire Company Z")

        event_a, _ = self.engine.process_report(jan)
        event_b, decision = self.engine.process_report(feb)

        self.assertNotEqual(event_a.canonical_event_id, event_b.canonical_event_id)
        self.assertEqual(decision.state, FusionDecisionState.DIFFERENT_EVENT)
        self.assertEqual(len(self.engine.canonical_events), 2)

    def test_case_2_same_partners_six_months_apart_are_not_the_same_event(self):
        later = JAN + timedelta(days=180)
        first = make_report("r1", EventType.PARTNERSHIP, ("nvidia", "tsmc"), JAN)
        second = make_report("r2", EventType.PARTNERSHIP, ("nvidia", "tsmc"), later,
                              title="NVIDIA and TSMC announce a new joint venture")

        event_a, _ = self.engine.process_report(first)
        event_b, decision = self.engine.process_report(second)

        self.assertNotEqual(event_a.canonical_event_id, event_b.canonical_event_id)
        self.assertEqual(decision.state, FusionDecisionState.DIFFERENT_EVENT)
        # Blocking rejects this pair before scoring is even reached — the
        # cheaper path. Assert the outcome, not which layer produced it.
        self.assertTrue(decision.reason)

    def test_case_3_ten_syndicated_copies_are_not_ten_confirmations(self):
        reports = [make_report("r0", lineage_relation=LineageRelation.ORIGINAL_REPORT, source_name="Reuters")]
        for i in range(1, 10):
            reports.append(make_report(
                f"r{i}", moment=JAN + timedelta(minutes=i * 10),
                source_name=f"Aggregator {i}", source_category=SourceCategory.AGGREGATOR,
                lineage_relation=LineageRelation.SYNDICATES,
            ))

        events, _ = self.engine.process_batch(reports)
        event = self.engine.canonical_events[reports[0].canonical_event_id]

        self.assertEqual(event.total_report_count, 10)
        # Exactly ONE independent source, despite ten articles.
        self.assertEqual(event.independent_source_count, 1)
        self.assertNotEqual(event.corroboration_state, CorroborationState.INDEPENDENTLY_CORROBORATED)

    def test_case_4_denial_is_preserved_alongside_the_original_reporting(self):
        reported = make_report("r1", title="Company X to acquire Company Y",
                                entity_ids=("companyx", "companyy"), event_type=EventType.ACQUISITION)
        denial = make_report("r2", title="Company X denies acquisition discussions",
                              description="Company X denies it is in talks.",
                              entity_ids=("companyx", "companyy"), event_type=EventType.ACQUISITION,
                              moment=JAN + timedelta(days=1), source_name="Bloomberg")

        event, _ = self.engine.process_report(reported)
        event, _ = self.engine.process_report(denial)

        self.assertTrue(event.has_contradictions)
        self.assertEqual(event.corroboration_state, CorroborationState.CONTRADICTED)
        # BOTH reports survive — the original reporting is not erased.
        self.assertEqual(len(event.report_ids), 2)
        contradictions = self.engine._contradictions_for(event.canonical_event_id)
        self.assertTrue(any(c.contradiction_type == ContradictionType.DIRECT_DENIAL for c in contradictions))

    def test_case_5_correction_preserves_both_original_and_corrected_state(self):
        report = make_report("r1")
        _, decision = self.engine.process_report(report)
        original_state = decision.state

        correction = self.engine.correct_decision(
            decision.decision_id, FusionDecisionState.DIFFERENT_EVENT,
            reason="analyst determined these are separate occurrences", corrected_by="analyst@example.com")

        original = next(d for d in self.engine.decisions if d.decision_id == decision.decision_id)
        self.assertEqual(original.state, original_state)                       # original intact
        self.assertEqual(original.corrected_by_decision_id, correction.decision_id)
        self.assertEqual(correction.state, FusionDecisionState.DIFFERENT_EVENT)
        self.assertIn("analyst@example.com", correction.reason)

    def test_case_6_conflicting_values_are_both_preserved_with_provenance(self):
        first = make_report("r1", event_type=EventType.ACQUISITION, entity_ids=("companyx", "companyy"),
                             title="Company X to acquire Company Y",
                             attributes={"deal_value": "$4B"}, source_name="Reuters")
        second = make_report("r2", event_type=EventType.ACQUISITION, entity_ids=("companyx", "companyy"),
                              title="Company X to acquire Company Y",
                              moment=JAN + timedelta(hours=6),
                              attributes={"deal_value": "$5B"}, source_name="Bloomberg")

        event, _ = self.engine.process_report(first)
        event2, decision = self.engine.process_report(second)

        # Whether merged or flagged, BOTH values must survive with provenance
        # and the conflict must be visible — never silently resolved.
        all_contradictions = self.engine.contradictions
        conflict_visible = bool(all_contradictions) or decision.state == FusionDecisionState.NEEDS_REVIEW
        self.assertTrue(conflict_visible, "a value conflict must be surfaced, not silently resolved")

        stored_values = set()
        for ev in self.engine.canonical_events.values():
            attr = ev.attributes.get("deal_value")
            if attr:
                stored_values.update(str(v.value) for v in attr.values)
        self.assertIn("$4B", stored_values)
        self.assertIn("$5B", stored_values)


class TestFusionOfGenuinelySameEvent(unittest.TestCase):
    """Spec §38.1-3: the same event, differently worded/sourced/timestamped, SHOULD fuse."""

    def setUp(self):
        self.engine = FusionEngine()

    def test_1_same_event_different_wording_fuses(self):
        a = make_report("r1", title="NVIDIA expands AI chip partnership with TSMC")
        b = make_report("r2", title="NVIDIA and TSMC increase AI chip production collaboration",
                         moment=JAN + timedelta(hours=3), source_name="CNBC")

        event_a, _ = self.engine.process_report(a)
        event_b, decision = self.engine.process_report(b)

        self.assertEqual(event_a.canonical_event_id, event_b.canonical_event_id)
        self.assertEqual(decision.state, FusionDecisionState.SAME_EVENT)
        self.assertEqual(event_b.total_report_count, 2)

    def test_2_same_event_from_the_other_side_fuses(self):
        a = make_report("r1", entity_ids=("nvidia", "tsmc"), title="NVIDIA expands partnership with TSMC")
        b = make_report("r2", entity_ids=("tsmc", "nvidia"),
                         title="TSMC confirms expanded NVIDIA production agreement",
                         moment=JAN + timedelta(hours=5), source_name="Nikkei")

        event_a, _ = self.engine.process_report(a)
        event_b, decision = self.engine.process_report(b)
        self.assertEqual(event_a.canonical_event_id, event_b.canonical_event_id)

    def test_3_minor_timestamp_difference_still_fuses(self):
        a = make_report("r1")
        b = make_report("r2", moment=JAN + timedelta(days=2), source_name="CNBC")
        event_a, _ = self.engine.process_report(a)
        event_b, _ = self.engine.process_report(b)
        self.assertEqual(event_a.canonical_event_id, event_b.canonical_event_id)

    def test_17_same_entities_different_event_type_never_fuses(self):
        a = make_report("r1", EventType.PARTNERSHIP, ("nvidia", "tsmc"))
        b = make_report("r2", EventType.LITIGATION, ("nvidia", "tsmc"),
                         moment=JAN + timedelta(hours=2), title="NVIDIA sues TSMC over contract terms")
        event_a, _ = self.engine.process_report(a)
        event_b, decision = self.engine.process_report(b)
        self.assertNotEqual(event_a.canonical_event_id, event_b.canonical_event_id)
        self.assertEqual(decision.state, FusionDecisionState.DIFFERENT_EVENT)

    def test_23_idempotent_ingestion(self):
        report = make_report("r1")
        for _ in range(5):
            self.engine.process_report(report)
        self.assertEqual(len(self.engine.canonical_events), 1)
        self.assertEqual(len(self.engine.reports), 1)


class TestCorroborationVsConfirmation(unittest.TestCase):
    """Spec §13, §14 — these are different axes and must not collapse."""

    def setUp(self):
        self.engine = FusionEngine()

    def test_6_two_independent_originals_are_corroborated(self):
        a = make_report("r1", source_name="Reuters", lineage_relation=LineageRelation.ORIGINAL_REPORT)
        b = make_report("r2", source_name="Bloomberg", moment=JAN + timedelta(hours=2),
                         lineage_relation=LineageRelation.ORIGINAL_REPORT)
        self.engine.process_report(a)
        event, _ = self.engine.process_report(b)
        self.assertEqual(event.corroboration_state, CorroborationState.INDEPENDENTLY_CORROBORATED)
        self.assertEqual(event.independent_source_count, 2)

    def test_7_unknown_lineage_is_never_counted_as_independent(self):
        a = make_report("r1", source_name="Site A")   # no lineage -> unknown
        b = make_report("r2", source_name="Site B", moment=JAN + timedelta(hours=1))
        self.engine.process_report(a)
        event, _ = self.engine.process_report(b)
        self.assertEqual(event.independent_source_count, 0)
        self.assertEqual(event.corroboration_state, CorroborationState.MULTI_SOURCE)

    def test_official_filing_yields_confirmation_not_mere_corroboration(self):
        a = make_report("r1", source_name="Reuters")
        b = make_report("r2", source_name="SEC filing", moment=JAN + timedelta(hours=4),
                         source_category=SourceCategory.REGULATORY_FILING)
        self.engine.process_report(a)
        event, _ = self.engine.process_report(b)
        self.assertEqual(event.corroboration_state, CorroborationState.OFFICIALLY_CONFIRMED)
        self.assertEqual(event.lifecycle_state, EventLifecycleState.CONFIRMED)

    def test_single_source_stays_single_source(self):
        event, _ = self.engine.process_report(make_report("r1"))
        self.assertEqual(event.corroboration_state, CorroborationState.SINGLE_SOURCE)

    def test_12_retraction_outranks_prior_reporting(self):
        a = make_report("r1")
        b = make_report("r2", title="Correction: earlier partnership report retracted",
                         description="We retract the earlier report.",
                         moment=JAN + timedelta(days=1), source_name="Reuters")
        self.engine.process_report(a)
        event, _ = self.engine.process_report(b)
        self.assertEqual(event.corroboration_state, CorroborationState.RETRACTED)
        self.assertEqual(event.lifecycle_state, EventLifecycleState.RETRACTED)

    def test_11_cancellation_is_detected(self):
        a = make_report("r1", EventType.ACQUISITION, ("x", "y"), title="X to acquire Y")
        b = make_report("r2", EventType.ACQUISITION, ("x", "y"),
                         title="X and Y call off the deal",
                         description="The transaction was called off.",
                         moment=JAN + timedelta(days=2))
        self.engine.process_report(a)
        event, _ = self.engine.process_report(b)
        contradictions = self.engine._contradictions_for(event.canonical_event_id)
        self.assertTrue(any(c.contradiction_type == ContradictionType.CANCELLATION for c in contradictions))


class TestAttributeProvenance(unittest.TestCase):
    """Spec §10 — no silent overwrites, ever."""

    def test_every_reported_value_keeps_its_own_provenance(self):
        attr = ConsolidatedAttribute(name="deal_value")
        attr.add(AttributeValue(value="$4B", report_id="r1", source_name="Reuters",
                                 source_category=SourceCategory.MAJOR_FINANCIAL_PRESS, reported_at=JAN))
        attr.add(AttributeValue(value="$5B", report_id="r2", source_name="SEC filing",
                                 source_category=SourceCategory.REGULATORY_FILING, reported_at=FEB))

        self.assertEqual(len(attr.values), 2)
        self.assertTrue(attr.has_conflict())
        self.assertEqual(set(str(v) for v in attr.distinct_values()), {"$4B", "$5B"})

    def test_current_best_prefers_authority_but_does_not_delete_the_other(self):
        attr = ConsolidatedAttribute(name="deal_value")
        attr.add(AttributeValue(value="$4B", report_id="r1", source_name="Reuters",
                                 source_category=SourceCategory.MAJOR_FINANCIAL_PRESS, reported_at=JAN))
        attr.add(AttributeValue(value="$5B", report_id="r2", source_name="SEC filing",
                                 source_category=SourceCategory.REGULATORY_FILING, reported_at=JAN))
        self.assertEqual(attr.current_best().value, "$5B")
        self.assertEqual(len(attr.values), 2)   # nothing destroyed

    def test_agreeing_values_are_not_a_conflict(self):
        attr = ConsolidatedAttribute(name="deal_value")
        attr.add(AttributeValue(value="$4B", report_id="r1", reported_at=JAN))
        attr.add(AttributeValue(value="$4B", report_id="r2", reported_at=JAN))
        self.assertFalse(attr.has_conflict())


class TestBlockingScalability(unittest.TestCase):
    """Spec §4, §30 — never compare everything with everything."""

    def test_24_large_candidate_set_stays_bounded(self):
        engine = FusionEngine(max_candidates=10)
        for i in range(200):
            engine.process_report(make_report(
                f"r{i}", entity_ids=(f"company{i}",), moment=JAN + timedelta(days=i % 30)))
        probe = make_report("probe", entity_ids=("company5",), moment=JAN + timedelta(days=5))
        candidates = engine.index.find_candidates(probe, max_candidates=10)
        self.assertLessEqual(len(candidates), 10)

    def test_blocking_keys_cover_every_participating_entity(self):
        report = make_report("r1", entity_ids=("nvidia", "tsmc", "amd"))
        keys = blocking_keys_for_report(report)
        for entity in ("nvidia", "tsmc", "amd"):
            self.assertTrue(any(entity in key for key in keys))

    def test_unrelated_events_never_share_a_block(self):
        index = BlockingIndex()
        engine = FusionEngine()
        event, _ = engine.process_report(make_report("r1", entity_ids=("nvidia",)))
        index.add(event)
        unrelated = make_report("r2", entity_ids=("unrelatedco",))
        self.assertEqual(index.find_candidates(unrelated), [])

    def test_index_removal_works(self):
        index = BlockingIndex()
        engine = FusionEngine()
        event, _ = engine.process_report(make_report("r1"))
        index.add(event)
        self.assertEqual(index.size(), 1)
        index.remove(event.canonical_event_id)
        self.assertEqual(index.size(), 0)


class TestFusionScoreExplainability(unittest.TestCase):
    """Spec §6 — every fusion decision must be explainable."""

    def test_score_components_sum_to_the_total(self):
        engine = FusionEngine()
        engine.process_report(make_report("r1"))
        _, decision = engine.process_report(make_report("r2", moment=JAN + timedelta(hours=2)))
        explanation = decision.score.explain()
        recomputed = sum(c["contribution"] for c in explanation["components"].values())
        self.assertAlmostEqual(recomputed, decision.score.score(), places=3)

    def test_weights_sum_to_one(self):
        from src.domain.fusion_models import FusionScore
        self.assertAlmostEqual(sum(FusionScore.WEIGHTS.values()), 1.0, places=6)

    def test_every_decision_carries_a_human_readable_reason(self):
        engine = FusionEngine()
        _, d1 = engine.process_report(make_report("r1"))
        _, d2 = engine.process_report(make_report("r2", entity_ids=("other",), moment=FEB))
        self.assertTrue(d1.reason)
        self.assertTrue(d2.reason)

    def test_missing_timestamp_yields_unresolved_not_a_merge(self):
        engine = FusionEngine()
        engine.process_report(make_report("r1"))
        undated = make_report("r2", moment=JAN + timedelta(hours=1))
        undated.structured_event.publication_time = None
        undated.structured_event.event_time = None
        _, decision = engine.process_report(undated)
        self.assertNotEqual(decision.state, FusionDecisionState.SAME_EVENT)


class TestTimelineAndReconstruction(unittest.TestCase):
    """Spec §17, §35 — the evolution of information must be reconstructable."""

    def setUp(self):
        self.engine = FusionEngine()

    def test_14_timeline_records_every_stage(self):
        self.engine.process_report(make_report("r1", lineage_relation=LineageRelation.ORIGINAL_REPORT))
        event, _ = self.engine.process_report(make_report(
            "r2", moment=JAN + timedelta(hours=2), source_name="Bloomberg",
            lineage_relation=LineageRelation.ORIGINAL_REPORT))

        timeline = self.engine.timeline_for(event.canonical_event_id)
        self.assertGreaterEqual(len(timeline), 3)
        self.assertEqual(timeline, sorted(timeline, key=lambda e: e.occurred_at))

    def test_26_historical_state_is_reconstructable(self):
        self.engine.process_report(make_report("r1"))
        event, _ = self.engine.process_report(make_report("r2", moment=JAN + timedelta(hours=2),
                                                            source_name="CNBC"))
        now = datetime.now(timezone.utc)
        past = self.engine.state_as_of(event.canonical_event_id, now - timedelta(days=1))
        present = self.engine.state_as_of(event.canonical_event_id, now + timedelta(seconds=1))

        self.assertEqual(past["entry_count"], 0)             # nothing was known a day ago
        self.assertGreater(present["entry_count"], 0)


class TestEventClustering(unittest.TestCase):
    """Spec §20, §21, §22 — clusters group events, never merge them."""

    def setUp(self):
        self.engine = FusionEngine()
        self.clusterer = ClusterEngine()

    def _events(self):
        return dict(self.engine.canonical_events)

    def test_15_related_events_cluster_without_merging(self):
        partnership, _ = self.engine.process_report(make_report(
            "r1", EventType.PARTNERSHIP, ("nvidia", "tsmc"), JAN))
        production, _ = self.engine.process_report(make_report(
            "r2", EventType.PRODUCTION_INCREASE, ("nvidia", "tsmc"), JAN + timedelta(days=20),
            title="NVIDIA and TSMC increase production capacity"))

        self.assertNotEqual(partnership.canonical_event_id, production.canonical_event_id)

        events = self._events()
        c1 = self.clusterer.assign(partnership, events)
        c2 = self.clusterer.assign(production, events)
        self.assertEqual(c1.cluster_id, c2.cluster_id)     # same story
        self.assertEqual(len(c1.event_ids), 2)             # two distinct events inside it

    def test_16_unrelated_events_do_not_cluster(self):
        a, _ = self.engine.process_report(make_report("r1", entity_ids=("nvidia", "tsmc")))
        b, _ = self.engine.process_report(make_report("r2", entity_ids=("bankco",), moment=JAN,
                                                       title="BankCo announces partnership with FintechCo"))
        events = self._events()
        c1 = self.clusterer.assign(a, events)
        c2 = self.clusterer.assign(b, events)
        self.assertNotEqual(c1.cluster_id, c2.cluster_id)

    def test_27_cluster_can_be_split(self):
        a, _ = self.engine.process_report(make_report("r1", entity_ids=("nvidia", "tsmc"), moment=JAN))
        b, _ = self.engine.process_report(make_report(
            "r2", EventType.CONTRACT, ("nvidia", "tsmc"), JAN + timedelta(days=10),
            title="NVIDIA wins contract with TSMC"))
        events = self._events()
        cluster = self.clusterer.assign(a, events)
        self.clusterer.assign(b, events)
        self.assertEqual(len(cluster.event_ids), 2)

        new_cluster = self.clusterer.split_cluster(
            cluster.cluster_id, [b.canonical_event_id], events)
        self.assertIsNotNone(new_cluster)
        self.assertEqual(len(cluster.event_ids), 1)
        self.assertEqual(len(new_cluster.event_ids), 1)

    def test_cluster_span_is_computed(self):
        a, _ = self.engine.process_report(make_report("r1", moment=JAN))
        b, _ = self.engine.process_report(make_report(
            "r2", EventType.CONTRACT, ("nvidia", "tsmc"), JAN + timedelta(days=30),
            title="NVIDIA wins contract with TSMC"))
        events = self._events()
        cluster = self.clusterer.assign(a, events)
        self.clusterer.assign(b, events)
        self.assertGreater(cluster.span_days(), 25)


class TestEventRelationsAndInference(unittest.TestCase):
    """Spec §19, §36 — causal claims are never asserted automatically."""

    def setUp(self):
        self.clusterer = ClusterEngine()

    def test_causal_relation_without_a_source_is_marked_inference(self):
        relation = self.clusterer.add_relation("e1", "e2", EventRelationType.CAUSES, is_inference=False)
        self.assertTrue(relation.is_inference)   # forced, despite the caller's claim

    def test_sourced_relation_can_be_a_fact(self):
        relation = self.clusterer.add_relation(
            "e1", "e2", EventRelationType.SUPERSEDES, source="company filing",
            method="direct_source", is_inference=False)
        self.assertFalse(relation.is_inference)

    def test_temporal_inference_never_emits_causation(self):
        engine = FusionEngine()
        a, _ = engine.process_report(make_report("r1", moment=JAN))
        b, _ = engine.process_report(make_report(
            "r2", EventType.CONTRACT, ("nvidia", "tsmc"), JAN + timedelta(days=10),
            title="NVIDIA wins contract with TSMC"))
        relations = self.clusterer.infer_temporal_relations([a, b])
        for relation in relations:
            self.assertNotEqual(relation.relation_type, EventRelationType.CAUSES)
            self.assertTrue(relation.is_inference)

    def test_facts_and_inferences_are_counted_separately(self):
        self.clusterer.add_relation("e1", "e2", EventRelationType.RELATED_TO, is_inference=True)
        self.clusterer.add_relation("e3", "e4", EventRelationType.SUPERSEDES,
                                     source="filing", is_inference=False)
        self.assertEqual(self.clusterer.inference_count(), 1)
        self.assertEqual(self.clusterer.fact_count(), 1)


class TestLifecycleStateMachine(unittest.TestCase):
    """Spec §16 — transitions are validated, not assumed."""

    def test_valid_forward_transitions(self):
        self.assertTrue(is_valid_transition(EventLifecycleState.REPORTED, EventLifecycleState.CORROBORATED))
        self.assertTrue(is_valid_transition(EventLifecycleState.CORROBORATED, EventLifecycleState.CONFIRMED))

    def test_terminal_states_do_not_move_on(self):
        self.assertFalse(is_valid_transition(EventLifecycleState.COMPLETED, EventLifecycleState.REPORTED))
        self.assertFalse(is_valid_transition(EventLifecycleState.CANCELLED, EventLifecycleState.CONFIRMED))

    def test_retraction_is_reachable_from_anywhere(self):
        for state in (EventLifecycleState.RUMORED, EventLifecycleState.REPORTED,
                       EventLifecycleState.CONFIRMED, EventLifecycleState.COMPLETED):
            self.assertTrue(is_valid_transition(state, EventLifecycleState.RETRACTED))

    def test_retracted_cannot_be_retracted_again(self):
        self.assertFalse(is_valid_transition(EventLifecycleState.RETRACTED, EventLifecycleState.RETRACTED))


class TestReviewCasesAndObservability(unittest.TestCase):
    def test_contradiction_opens_a_review_case(self):
        engine = FusionEngine()
        engine.process_report(make_report("r1", EventType.ACQUISITION, ("x", "y"), title="X to acquire Y"))
        engine.process_report(make_report("r2", EventType.ACQUISITION, ("x", "y"),
                                           title="X denies acquisition discussions",
                                           description="X denies the talks.",
                                           moment=JAN + timedelta(days=1)))
        self.assertTrue(any(rc.reason.value == "contradiction" for rc in engine.review_cases))

    def test_batch_stats_are_complete_and_credential_free(self):
        engine = FusionEngine()
        reports = [make_report("r1"), make_report("r2", moment=JAN + timedelta(hours=1)),
                    make_report("r3", entity_ids=("other",), moment=FEB)]
        _, stats = engine.process_batch(reports)
        self.assertEqual(stats.reports_processed, 3)
        self.assertEqual(stats.llm_assisted_cases, 0)
        serialized = str(stats.as_log_dict()).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, serialized)

    def test_quality_confidence_is_not_market_importance(self):
        """Spec §25: confidence answers 'is this correctly represented', not 'does it matter'."""
        engine = FusionEngine()
        event, _ = engine.process_report(make_report("r1"))
        self.assertTrue(hasattr(event, "quality_confidence"))
        for forbidden in ("importance", "impact", "market_impact", "signal"):
            self.assertNotIn(forbidden, CanonicalEvent.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
