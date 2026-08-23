"""
tests/events/test_event_repository.py
------------------------------------------
Tests for event storage, versioning, status transitions, article
linking, corrections, and the internal Event API.
"""

import sys
import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.event_repository import EventRepository, initialize_event_schema
from src.domain.event_models import (
    StructuredEvent, EventParticipant, ParticipationRole, EventEvidence, EventConfidence,
    EventStatus, ExtractionTier, EventGeography, ArticleEventLink, ArticleEventRelation,
    EventCorrection,
)
from src.events.taxonomy import EventType, EventCategory, category_for

PUB = datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc)


def make_event(event_id="e1", event_type=EventType.PARTNERSHIP, entity_ids=("nvidia", "tsmc"),
               moment=PUB, article_id="a1", status=EventStatus.DETECTED,
               instruments=(), sectors=(), geography=None, syndicated=False):
    return StructuredEvent(
        event_id=event_id, event_type=event_type, category=category_for(event_type),
        title="NVIDIA announces expanded partnership with TSMC",
        description="Reported: NVIDIA announces expanded partnership with TSMC",
        participants=[EventParticipant(entity_id=e,
                                        role=ParticipationRole.PRIMARY if i == 0 else ParticipationRole.SECONDARY,
                                        resolution_confidence=0.9)
                       for i, e in enumerate(entity_ids)],
        instrument_ids=list(instruments), sector_ids=list(sectors), geography=geography,
        publication_time=moment, ingestion_time=moment, detection_time=moment,
        evidence=[EventEvidence(article_id=article_id, source_name="Reuters",
                                 published_at=moment, is_syndicated=syndicated)],
        confidence=EventConfidence(extraction_certainty=0.8, entity_resolution_confidence=0.9,
                                    source_quality=0.8, temporal_certainty=0.5),
        status=status, created_at=moment,
    )


def new_repo():
    conn = sqlite3.connect(":memory:")
    initialize_event_schema(conn)
    return EventRepository(conn)


class TestEvidenceIsMandatory(unittest.TestCase):
    """Spec §9: an event that cannot be traced to evidence must never exist."""

    def test_event_without_evidence_is_refused(self):
        repo = new_repo()
        event = make_event()
        event.evidence = []
        with self.assertRaises(ValueError):
            repo.save(event)

    def test_event_without_participants_is_refused(self):
        repo = new_repo()
        event = make_event()
        event.participants = []
        with self.assertRaises(ValueError):
            repo.save(event)

    def test_validate_reports_the_specific_problem(self):
        event = make_event()
        event.evidence = []
        self.assertIn("evidence", event.validate())


class TestSaveAndHydrate(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()

    def test_round_trip_preserves_core_fields(self):
        self.repo.save(make_event())
        loaded = self.repo.get("e1")
        self.assertEqual(loaded.event_type, EventType.PARTNERSHIP)
        self.assertEqual(loaded.category, EventCategory.CORPORATE)
        self.assertEqual(loaded.primary_entity_id(), "nvidia")
        self.assertEqual(loaded.secondary_entity_ids(), ["tsmc"])

    def test_round_trip_preserves_all_timestamps(self):
        self.repo.save(make_event())
        loaded = self.repo.get("e1")
        self.assertEqual(loaded.publication_time, PUB)
        self.assertEqual(loaded.ingestion_time, PUB)
        self.assertEqual(loaded.detection_time, PUB)

    def test_round_trip_preserves_confidence_breakdown(self):
        self.repo.save(make_event())
        loaded = self.repo.get("e1")
        self.assertAlmostEqual(loaded.confidence.extraction_certainty, 0.8, places=3)
        self.assertAlmostEqual(loaded.confidence.score(), make_event().confidence.score(), places=3)

    def test_round_trip_preserves_geography(self):
        geo = EventGeography(country="TW", facility="Hsinchu Fab")
        self.repo.save(make_event(geography=geo))
        loaded = self.repo.get("e1")
        self.assertEqual(loaded.geography.country, "TW")

    def test_multi_entity_event_stored_once(self):
        self.repo.save(make_event(entity_ids=("nvidia", "amd", "intel", "tsmc")))
        self.assertEqual(self.repo.count(), 1)
        self.assertEqual(len(self.repo.get("e1").participants), 4)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get("nope"))


class TestVersioningAndStatus(unittest.TestCase):
    """Spec §13, §14: events evolve; historical states are never destroyed."""

    def setUp(self):
        self.repo = new_repo()

    def test_7_and_8_event_update_supersedes_without_deleting(self):
        original = make_event("e1", event_type=EventType.ACQUISITION)
        self.repo.save(original)

        confirmed = make_event("e2", event_type=EventType.ACQUISITION, article_id="a2")
        self.repo.supersede("e1", confirmed)

        self.assertEqual(self.repo.get("e1").status, EventStatus.SUPERSEDED)
        self.assertEqual(self.repo.get("e2").supersedes_event_id, "e1")
        self.assertEqual(self.repo.get("e2").version, 2)

    def test_superseded_events_excluded_from_default_queries(self):
        self.repo.save(make_event("e1"))
        self.repo.supersede("e1", make_event("e2", article_id="a2"))
        self.assertEqual(len(self.repo.query()["events"]), 1)
        self.assertEqual(len(self.repo.query(include_superseded=True)["events"]), 2)

    def test_9_and_10_status_can_move_to_contradicted_or_retracted(self):
        self.repo.save(make_event("e1"))
        self.repo.update_status("e1", EventStatus.RETRACTED)
        self.assertEqual(self.repo.get("e1").status, EventStatus.RETRACTED)

    def test_first_report_is_detected_never_confirmed(self):
        self.repo.save(make_event("e1"))
        self.assertEqual(self.repo.get("e1").status, EventStatus.DETECTED)

    def test_confirmation_is_an_explicit_transition(self):
        self.repo.save(make_event("e1"))
        self.repo.update_status("e1", EventStatus.CONFIRMED)
        self.assertEqual(self.repo.get("e1").status, EventStatus.CONFIRMED)


class TestArticleEventRelationships(unittest.TestCase):
    """Spec §15: article -> event relations are explicit, not implied."""

    def setUp(self):
        self.repo = new_repo()
        self.repo.save(make_event("e1"))

    def test_all_relation_types_can_be_recorded(self):
        for relation in ArticleEventRelation:
            self.repo.link_article(ArticleEventLink(article_id=f"a-{relation.value}",
                                                     event_id="e1", relation=relation))
        rows = self.repo._conn.execute("SELECT COUNT(*) FROM article_event_links").fetchone()[0]
        self.assertEqual(rows, len(ArticleEventRelation))

    def test_14_multiple_articles_can_support_one_event(self):
        self.repo.add_evidence("e1", EventEvidence(article_id="a2", source_name="CNBC", published_at=PUB))
        self.repo.add_evidence("e1", EventEvidence(article_id="a3", source_name="Bloomberg", published_at=PUB))
        self.assertEqual(len(self.repo.get_articles_for_event("e1")), 3)

    def test_15_syndicated_copies_do_not_inflate_independent_source_count(self):
        self.repo.add_evidence("e1", EventEvidence(article_id="a2", source_name="Reuters",
                                                    published_at=PUB, is_syndicated=True))
        self.repo.add_evidence("e1", EventEvidence(article_id="a3", source_name="CNBC", published_at=PUB))
        event = self.repo.get("e1")
        self.assertEqual(len(event.evidence), 3)
        # Reuters original + CNBC = 2 independent; the syndicated copy adds nothing.
        self.assertEqual(event.independent_source_count(), 2)

    def test_events_queryable_by_source_article(self):
        result = self.repo.query(article_id="a1")
        self.assertEqual(len(result["events"]), 1)


class TestHumanCorrections(unittest.TestCase):
    """Spec §27: corrections must be auditable, stored alongside the original."""

    def test_correction_is_recorded_without_erasing_the_original(self):
        repo = new_repo()
        repo.save(make_event("e1"))
        repo.save_correction(EventCorrection(
            correction_id="c1", event_id="e1", field_name="event_type",
            old_value="partnership", new_value="contract",
            corrected_by="analyst@example.com", reason="Reread the filing.",
        ))
        corrections = repo.get_corrections("e1")
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0].old_value, "partnership")
        self.assertEqual(corrections[0].corrected_by, "analyst@example.com")
        self.assertIsNotNone(corrections[0].corrected_at)


class TestEventApi(unittest.TestCase):
    """Spec §24: the internal Event API."""

    def setUp(self):
        self.repo = new_repo()
        for i in range(8):
            self.repo.save(make_event(
                f"e{i}",
                event_type=EventType.EARNINGS if i % 2 == 0 else EventType.PARTNERSHIP,
                entity_ids=("nvidia",) if i < 4 else ("tesla",),
                moment=PUB - timedelta(hours=i),
                article_id=f"a{i}",
                instruments=("nasdaq-nvda",) if i == 0 else (),
                sectors=("technology",) if i == 1 else (),
                geography=EventGeography(country="US") if i == 2 else None,
            ))

    def test_latest_events_newest_first(self):
        events = self.repo.query(limit=3)["events"]
        times = [e.publication_time for e in events]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_by_entity(self):
        result = self.repo.query(entity_id="tesla", limit=50)
        self.assertEqual(len(result["events"]), 4)

    def test_by_event_type(self):
        result = self.repo.query(event_type=EventType.EARNINGS, limit=50)
        self.assertTrue(all(e.event_type == EventType.EARNINGS for e in result["events"]))

    def test_by_category(self):
        result = self.repo.query(category=EventCategory.CORPORATE, limit=50)
        self.assertEqual(len(result["events"]), 8)

    def test_by_instrument(self):
        self.assertEqual(len(self.repo.query(instrument_id="nasdaq-nvda")["events"]), 1)

    def test_by_sector(self):
        self.assertEqual(len(self.repo.query(sector_id="technology")["events"]), 1)

    def test_by_geography(self):
        self.assertEqual(len(self.repo.query(country="US")["events"]), 1)

    def test_by_date_range(self):
        result = self.repo.query(published_after=PUB - timedelta(hours=3), limit=50)
        self.assertEqual(len(result["events"]), 4)

    def test_by_minimum_confidence(self):
        high_bar = self.repo.query(min_confidence=0.99, limit=50)
        self.assertEqual(len(high_bar["events"]), 0)

    def test_cursor_pagination_covers_everything_without_repeats(self):
        seen, cursor = [], None
        while True:
            page = self.repo.query(limit=3, cursor=cursor)
            seen.extend(e.event_id for e in page["events"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 8)
        self.assertEqual(len(set(seen)), 8)

    def test_limit_is_hard_capped(self):
        self.assertLessEqual(len(self.repo.query(limit=99999)["events"]), 500)


class TestDuplicateCandidateBounding(unittest.TestCase):
    def test_candidate_set_is_bounded_by_type_and_time(self):
        repo = new_repo()
        repo.save(make_event("same-type-near", event_type=EventType.EARNINGS, moment=PUB, article_id="a1"))
        repo.save(make_event("same-type-far", event_type=EventType.EARNINGS,
                              moment=PUB - timedelta(days=60), article_id="a2"))
        repo.save(make_event("other-type", event_type=EventType.LAYOFFS, moment=PUB, article_id="a3"))

        candidate = make_event("new", event_type=EventType.EARNINGS, moment=PUB, article_id="a4")
        candidates = repo.find_candidate_events(candidate)
        ids = {c.event_id for c in candidates}
        self.assertIn("same-type-near", ids)
        self.assertNotIn("same-type-far", ids)
        self.assertNotIn("other-type", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
