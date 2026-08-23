"""
tests/events/test_extractor.py
-----------------------------------
Tests for the event taxonomy, tiered extraction pipeline, and event
fingerprinting — covering the scenarios spec §29 requires.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.events.taxonomy import (
    EventType, EventCategory, category_for, subtypes_for, is_valid_subtype,
    types_in_category, from_legacy_string, TYPE_TO_CATEGORY, EVENT_TYPE_RULES,
)
from src.events.extractor import EventExtractor
from src.events.fingerprint import (
    compute_event_fingerprint, is_same_event, find_matching_event,
)
from src.domain.event_models import (
    StructuredEvent, EventParticipant, ParticipationRole, EventStatus,
    EventConfidence, EventEvidence, ExtractionTier, EventGeography, EventInference,
)

PUB = datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc)
ING = datetime(2026, 8, 20, 14, 35, tzinfo=timezone.utc)


def make_article(article_id="a1", title="", summary="", source_name="Reuters",
                 published_at=PUB, ingested_at=ING, **extra):
    return {"article_id": article_id, "title": title, "summary": summary,
            "source_name": source_name, "published_at": published_at,
            "ingested_at": ingested_at, **extra}


class TestTaxonomy(unittest.TestCase):
    def test_every_event_type_has_a_category(self):
        for event_type in EventType:
            self.assertIsNotNone(category_for(event_type), f"{event_type} has no category")

    def test_all_six_categories_are_populated(self):
        for category in EventCategory:
            self.assertGreater(len(types_in_category(category)), 0, f"{category} is empty")

    def test_subtypes_are_validated_against_their_type(self):
        self.assertTrue(is_valid_subtype(EventType.EARNINGS, "earnings_beat"))
        self.assertFalse(is_valid_subtype(EventType.EARNINGS, "guidance_raised"))

    def test_none_subtype_is_always_valid(self):
        self.assertTrue(is_valid_subtype(EventType.LAYOFFS, None))

    def test_legacy_detector_strings_map_onto_the_taxonomy(self):
        self.assertEqual(from_legacy_string("EARNINGS"), EventType.EARNINGS)
        self.assertEqual(from_legacy_string("CEO_CHANGE"), EventType.MANAGEMENT_CHANGE)
        self.assertEqual(from_legacy_string("cyberattack"), EventType.CYBERSECURITY_INCIDENT)

    def test_unknown_legacy_string_returns_none_rather_than_guessing(self):
        self.assertIsNone(from_legacy_string("SOMETHING_UNKNOWN"))
        self.assertIsNone(from_legacy_string(""))

    def test_every_rule_references_a_real_event_type(self):
        for rule in EVENT_TYPE_RULES:
            self.assertIn(rule.event_type, TYPE_TO_CATEGORY)


class TestTier1RelevanceFilter(unittest.TestCase):
    def test_financial_article_passes(self):
        self.assertTrue(EventExtractor.is_potentially_relevant(
            make_article(title="Nvidia reports quarterly earnings", summary="Revenue rose.")))

    def test_irrelevant_article_is_filtered_out(self):
        self.assertFalse(EventExtractor.is_potentially_relevant(
            make_article(title="Local bakery wins pastry competition", summary="Judges praised the croissants.")))

    def test_empty_article_is_filtered_out(self):
        self.assertFalse(EventExtractor.is_potentially_relevant(make_article()))


class TestEventClassification(unittest.TestCase):
    def test_detects_earnings(self):
        results = EventExtractor.classify("Nvidia reports quarterly results with record revenue")
        self.assertIn(EventType.EARNINGS, [t for t, _, _ in results])

    def test_detects_partnership(self):
        results = EventExtractor.classify("NVIDIA announces expanded partnership with TSMC")
        self.assertIn(EventType.PARTNERSHIP, [t for t, _, _ in results])

    def test_detects_macro_event(self):
        results = EventExtractor.classify("Federal Reserve raises interest rates by 25 basis points")
        types = [t for t, _, _ in results]
        self.assertTrue(EventType.INTEREST_RATE_DECISION in types or EventType.CENTRAL_BANK_DECISION in types)

    def test_detects_geopolitical_event(self):
        results = EventExtractor.classify("US imposes export restrictions on advanced chips")
        self.assertIn(EventType.EXPORT_RESTRICTIONS, [t for t, _, _ in results])

    def test_detects_supply_chain_event(self):
        results = EventExtractor.classify("TSMC halts production at its Taiwan plant after earthquake")
        self.assertIn(EventType.FACTORY_SHUTDOWN, [t for t, _, _ in results])

    def test_negation_suppresses_false_positive(self):
        # "launches investigation" must NOT be read as a product launch.
        results = EventExtractor.classify("Regulator launches investigation into the company")
        self.assertNotIn(EventType.PRODUCT_LAUNCH, [t for t, _, _ in results])

    def test_no_match_returns_empty(self):
        self.assertEqual(EventExtractor.classify("The weather was pleasant yesterday"), [])


class TestSpecRequiredExtractionScenarios(unittest.TestCase):
    """The scenarios spec §29 lists explicitly."""

    def setUp(self):
        self.extractor = EventExtractor()

    def test_1_article_with_no_event_yields_nothing(self):
        article = make_article(title="Market commentary and general investor sentiment", summary="Analysts discussed stocks.")
        self.assertEqual(self.extractor.extract_from_article(article, entity_ids=["nvidia"]), [])

    def test_2_single_event_article(self):
        article = make_article(title="NVIDIA announces expanded partnership with TSMC")
        events = self.extractor.extract_from_article(article, entity_ids=["nvidia", "tsmc"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, EventType.PARTNERSHIP)

    def test_3_multi_event_article_produces_several_events(self):
        article = make_article(
            title="Nvidia reports quarterly results and announces share buyback program",
            summary="The company also raises guidance for the year.")
        events = self.extractor.extract_from_article(article, entity_ids=["nvidia"])
        types = {e.event_type for e in events}
        self.assertGreaterEqual(len(events), 2)
        self.assertIn(EventType.EARNINGS, types)

    def test_4_and_5_multiple_entities_with_primary_and_secondary(self):
        article = make_article(title="NVIDIA announces expanded partnership with TSMC")
        events = self.extractor.extract_from_article(article, entity_ids=["nvidia", "tsmc"])
        event = events[0]
        self.assertEqual(event.primary_entity_id(), "nvidia")
        self.assertEqual(event.secondary_entity_ids(), ["tsmc"])

    def test_12_missing_event_time_lowers_temporal_certainty(self):
        article = make_article(title="Nvidia reports quarterly results")
        event = self.extractor.extract_from_article(article, entity_ids=["nvidia"])[0]
        self.assertIsNone(event.event_time)
        self.assertEqual(event.confidence.temporal_certainty, 0.5)

    def test_13_all_four_timestamps_kept_distinct(self):
        event_time = PUB - timedelta(hours=2)
        article = make_article(title="Nvidia reports quarterly results", event_time=event_time)
        event = self.extractor.extract_from_article(article, entity_ids=["nvidia"])[0]
        self.assertEqual(event.event_time, event_time)
        self.assertEqual(event.publication_time, PUB)
        self.assertEqual(event.ingestion_time, ING)
        self.assertIsNotNone(event.detection_time)
        self.assertNotEqual(event.event_time, event.publication_time)

    def test_15_syndicated_source_does_not_count_as_independent(self):
        article = make_article(title="Nvidia reports quarterly results", is_syndicated=True)
        event = self.extractor.extract_from_article(article, entity_ids=["nvidia"])[0]
        self.assertEqual(event.independent_source_count(), 0)

    def test_17_corporate_event_extraction(self):
        article = make_article(title="Company files for bankruptcy protection")
        events = self.extractor.extract_from_article(article, entity_ids=["someco"])
        self.assertEqual(events[0].category, EventCategory.CORPORATE)

    def test_18_macro_event_extraction(self):
        article = make_article(title="Federal Reserve raises interest rates amid inflation data")
        events = self.extractor.extract_from_article(article, entity_ids=["fed"])
        self.assertIn(EventCategory.MACRO, {e.category for e in events})

    def test_19_geopolitical_event_extraction(self):
        article = make_article(title="Government imposes sanctions on the company")
        events = self.extractor.extract_from_article(article, entity_ids=["someco"])
        self.assertIn(EventCategory.GEOPOLITICAL, {e.category for e in events})

    def test_20_supply_chain_event_extraction(self):
        article = make_article(title="Factory shutdown halts production at the main plant")
        events = self.extractor.extract_from_article(article, entity_ids=["someco"])
        self.assertIn(EventCategory.SUPPLY_CHAIN, {e.category for e in events})

    def test_article_with_no_entities_yields_no_event(self):
        article = make_article(title="Nvidia reports quarterly results")
        self.assertEqual(self.extractor.extract_from_article(article, entity_ids=[]), [])


class TestFactVsInference(unittest.TestCase):
    """Spec §10 + §29: factual data and inferred interpretation must stay separate."""

    def setUp(self):
        self.extractor = EventExtractor()

    def test_description_restates_facts_without_predicting(self):
        article = make_article(title="NVIDIA announces expanded partnership with TSMC")
        event = self.extractor.extract_from_article(article, entity_ids=["nvidia", "tsmc"])[0]
        lowered = event.description.lower()
        for speculative in ("will rise", "should buy", "expected to increase", "may boost", "likely to"):
            self.assertNotIn(speculative, lowered)

    def test_structured_event_has_no_field_that_could_hold_an_interpretation(self):
        event_fields = set(StructuredEvent.__dataclass_fields__.keys())
        for forbidden in ("inference", "prediction", "interpretation", "recommendation", "signal", "forecast"):
            self.assertNotIn(forbidden, event_fields)

    def test_inference_is_a_separate_record_pointing_at_the_event(self):
        inference = EventInference(
            inference_id="inf-1", event_id="evt-1",
            statement="The partnership may increase production capacity.",
            method="analyst_rule_v1", created_at=PUB,
        )
        self.assertEqual(inference.event_id, "evt-1")
        self.assertNotIsInstance(inference, StructuredEvent)


class TestConfidence(unittest.TestCase):
    def test_score_is_weighted_mean_and_explainable(self):
        confidence = EventConfidence(
            extraction_certainty=0.8, entity_resolution_confidence=0.9,
            source_quality=1.0, temporal_certainty=1.0, corroboration=0.0,
        )
        explanation = confidence.explain()
        self.assertEqual(explanation["score"], confidence.score())
        self.assertEqual(len(explanation["components"]), 5)
        recomputed = sum(c["contribution"] for c in explanation["components"].values())
        self.assertAlmostEqual(recomputed, confidence.score(), places=3)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(EventConfidence.WEIGHTS.values()), 1.0, places=6)

    def test_bands_map_correctly(self):
        self.assertEqual(EventConfidence(extraction_certainty=1, entity_resolution_confidence=1,
                                          source_quality=1, temporal_certainty=1, corroboration=1).band().value, "high")
        self.assertEqual(EventConfidence().band().value, "low")

    def test_official_source_scores_higher_than_unclassified(self):
        participants = [EventParticipant(entity_id="x", role=ParticipationRole.PRIMARY, resolution_confidence=0.9)]
        official = EventExtractor.build_confidence(0.8, participants, "official", True)
        unknown = EventExtractor.build_confidence(0.8, participants, "unclassified", True)
        self.assertGreater(official.score(), unknown.score())

    def test_corroboration_is_honestly_zero_in_phase_4(self):
        participants = [EventParticipant(entity_id="x", role=ParticipationRole.PRIMARY)]
        confidence = EventExtractor.build_confidence(0.9, participants, "official", True)
        self.assertEqual(confidence.corroboration, 0.0)


class TestEventFingerprinting(unittest.TestCase):
    """Spec §17 + §29.21: identity must tolerate wording differences."""

    def _event(self, event_id, event_type, entity_ids, moment=PUB, title=""):
        return StructuredEvent(
            event_id=event_id, event_type=event_type, category=category_for(event_type),
            title=title,
            participants=[EventParticipant(entity_id=e,
                                            role=ParticipationRole.PRIMARY if i == 0 else ParticipationRole.SECONDARY)
                           for i, e in enumerate(entity_ids)],
            publication_time=moment,
            evidence=[EventEvidence(article_id="a1", published_at=moment)],
        )

    def test_different_wording_same_facts_fingerprints_identically(self):
        a = self._event("e1", EventType.ACQUISITION, ["x", "y"], title="Company X acquires Company Y")
        b = self._event("e2", EventType.ACQUISITION, ["x", "y"], title="Company X agrees to purchase Company Y")
        self.assertEqual(compute_event_fingerprint(a), compute_event_fingerprint(b))

    def test_entity_order_does_not_change_fingerprint(self):
        a = self._event("e1", EventType.PARTNERSHIP, ["nvidia", "tsmc"])
        b = self._event("e2", EventType.PARTNERSHIP, ["tsmc", "nvidia"])
        self.assertEqual(compute_event_fingerprint(a), compute_event_fingerprint(b))

    def test_different_event_type_fingerprints_differently(self):
        a = self._event("e1", EventType.PARTNERSHIP, ["x", "y"])
        b = self._event("e2", EventType.ACQUISITION, ["x", "y"])
        self.assertNotEqual(compute_event_fingerprint(a), compute_event_fingerprint(b))

    def test_far_apart_in_time_fingerprints_differently(self):
        a = self._event("e1", EventType.EARNINGS, ["x"], moment=PUB)
        b = self._event("e2", EventType.EARNINGS, ["x"], moment=PUB + timedelta(days=90))
        self.assertNotEqual(compute_event_fingerprint(a), compute_event_fingerprint(b))

    def test_6_duplicate_event_is_detected(self):
        a = self._event("e1", EventType.PARTNERSHIP, ["nvidia", "tsmc"])
        b = self._event("e2", EventType.PARTNERSHIP, ["nvidia", "tsmc"])
        same, reason = is_same_event(b, a)
        self.assertTrue(same)
        self.assertIn("fingerprint", reason)

    def test_near_match_across_bucket_boundary_is_still_the_same_event(self):
        a = self._event("e1", EventType.ACQUISITION, ["x", "y"], moment=PUB)
        b = self._event("e2", EventType.ACQUISITION, ["x", "y"], moment=PUB + timedelta(days=4))
        same, reason = is_same_event(b, a)
        self.assertTrue(same)

    def test_different_entities_are_not_the_same_event(self):
        a = self._event("e1", EventType.ACQUISITION, ["x", "y"])
        b = self._event("e2", EventType.ACQUISITION, ["p", "q"])
        same, _ = is_same_event(b, a)
        self.assertFalse(same)

    def test_refuses_to_merge_when_a_timestamp_is_missing(self):
        a = self._event("e1", EventType.ACQUISITION, ["x", "y"], moment=PUB)
        b = self._event("e2", EventType.ACQUISITION, ["x", "z"], moment=PUB + timedelta(days=5))
        b.publication_time = None
        b.evidence = [EventEvidence(article_id="a2")]
        same, reason = is_same_event(b, a)
        self.assertFalse(same)

    def test_find_matching_event_scans_a_candidate_set(self):
        existing = [self._event("e1", EventType.EARNINGS, ["nvidia"]),
                     self._event("e2", EventType.PARTNERSHIP, ["nvidia", "tsmc"])]
        candidate = self._event("e3", EventType.PARTNERSHIP, ["nvidia", "tsmc"])
        match, reason = find_matching_event(candidate, existing)
        self.assertIsNotNone(match)
        self.assertEqual(match.event_id, "e2")

    def test_event_never_matches_itself(self):
        event = self._event("e1", EventType.EARNINGS, ["nvidia"])
        match, _ = find_matching_event(event, [event])
        self.assertIsNone(match)


class TestBatchExtractionAndObservability(unittest.TestCase):
    def setUp(self):
        self.extractor = EventExtractor()

    def test_batch_reports_full_stats(self):
        articles = [
            make_article("a1", title="Nvidia reports quarterly results"),
            make_article("a2", title="Local bakery wins competition"),
            make_article("a3", title="Company X agrees to buy Company Y"),
        ]
        events, stats = self.extractor.extract_batch(
            articles, entity_ids_by_article={"a1": ["nvidia"], "a3": ["x", "y"]})
        self.assertEqual(stats.articles_processed, 3)
        self.assertEqual(stats.articles_filtered_out, 1)
        self.assertGreaterEqual(stats.events_detected, 2)

    def test_llm_calls_are_zero_by_construction(self):
        articles = [make_article("a1", title="Nvidia reports quarterly results")]
        _, stats = self.extractor.extract_batch(articles, entity_ids_by_article={"a1": ["nvidia"]})
        self.assertEqual(stats.llm_calls, 0)

    def test_stats_contain_no_credentials(self):
        _, stats = self.extractor.extract_batch([], {})
        serialized = str(stats.as_log_dict()).lower()
        for forbidden in ("api_key", "token", "password", "secret"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
