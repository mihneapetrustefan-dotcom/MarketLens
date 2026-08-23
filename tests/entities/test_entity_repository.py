"""
tests/entities/test_entity_repository.py
---------------------------------------------
Tests for the entity persistence layer, including coexistence with the
existing application's tables and provider-disagreement handling.
"""

import sys
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.entity_schema import initialize_entity_schema
from src.data_access.entity_repository import EntityRepository
from src.domain.entity_models import (
    EntityAlias, EntityIdentifier, EntityMention, EntityRelationship,
    IdentityChange, SectorClassification,
    EntityType, AliasType, IdentifierType, MentionRelevance,
    ResolutionMethod, RelationshipType, ProvenanceKind, IdentityChangeType,
)

T2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def new_repo():
    conn = sqlite3.connect(":memory:")
    initialize_entity_schema(conn)
    return EntityRepository(conn)


class TestAliasPersistence(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()

    def test_save_and_find_alias(self):
        self.repo.save_alias(EntityAlias(
            entity_id="nvidia", entity_type=EntityType.COMPANY,
            alias="NVIDIA Corporation", normalized_alias="nvidia",
        ))
        found = self.repo.find_by_normalized_alias("nvidia")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].entity_id, "nvidia")

    def test_ambiguity_flag_round_trips(self):
        self.repo.save_alias(EntityAlias(
            entity_id="apple", entity_type=EntityType.COMPANY,
            alias="Apple", normalized_alias="apple", ambiguity_risk=True,
        ))
        self.assertTrue(self.repo.find_by_normalized_alias("apple")[0].ambiguity_risk)

    def test_multiple_entities_can_share_a_normalized_alias(self):
        for entity_id in ("company-a", "company-b"):
            self.repo.save_alias(EntityAlias(
                entity_id=entity_id, entity_type=EntityType.COMPANY,
                alias="Shared Name", normalized_alias="shared name",
            ))
        self.assertEqual(len(self.repo.find_by_normalized_alias("shared name")), 2)

    def test_historical_alias_validity_window_preserved(self):
        self.repo.save_alias(EntityAlias(
            entity_id="meta", entity_type=EntityType.COMPANY, alias="Facebook",
            normalized_alias="facebook", alias_type=AliasType.HISTORICAL_NAME,
            valid_until=T2025,
        ))
        loaded = self.repo.find_by_normalized_alias("facebook")[0]
        self.assertEqual(loaded.alias_type, AliasType.HISTORICAL_NAME)
        self.assertEqual(loaded.valid_until, T2025)


class TestIdentifierPersistence(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()

    def test_save_and_find_ticker(self):
        self.repo.save_identifier(EntityIdentifier(
            entity_id="nvidia", entity_type=EntityType.COMPANY,
            identifier_type=IdentifierType.TICKER, value="NVDA",
        ))
        found = self.repo.find_by_identifier(IdentifierType.TICKER, "NVDA")
        self.assertEqual(found[0].entity_id, "nvidia")

    def test_shared_ticker_returns_both_entities(self):
        for entity_id in ("electrica", "estee-lauder"):
            self.repo.save_identifier(EntityIdentifier(
                entity_id=entity_id, entity_type=EntityType.COMPANY,
                identifier_type=IdentifierType.TICKER, value="EL",
            ))
        self.assertEqual(len(self.repo.find_by_identifier(IdentifierType.TICKER, "EL")), 2)

    def test_isin_and_ticker_kept_in_separate_namespaces(self):
        self.repo.save_identifier(EntityIdentifier(
            entity_id="nvidia", entity_type=EntityType.COMPANY,
            identifier_type=IdentifierType.ISIN, value="US67066G1040",
        ))
        self.assertEqual(len(self.repo.find_by_identifier(IdentifierType.TICKER, "US67066G1040")), 0)
        self.assertEqual(len(self.repo.find_by_identifier(IdentifierType.ISIN, "US67066G1040")), 1)


class TestMentionPersistence(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()

    def test_save_and_retrieve_mentions_for_article(self):
        self.repo.save_mention(EntityMention(
            article_id="a1", entity_id="nvidia", entity_type=EntityType.COMPANY,
            mention_text="NVIDIA", relevance=MentionRelevance.PRIMARY,
            confidence=Decimal("0.98"), method=ResolutionMethod.EXACT_NAME,
        ))
        mentions = self.repo.get_mentions_for_article("a1")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].relevance, MentionRelevance.PRIMARY)
        self.assertEqual(mentions[0].confidence, Decimal("0.98"))

    def test_multiple_entities_in_one_article(self):
        for entity_id, relevance in [("nvidia", MentionRelevance.PRIMARY), ("tsmc", MentionRelevance.SECONDARY)]:
            self.repo.save_mention(EntityMention(
                article_id="a1", entity_id=entity_id, entity_type=EntityType.COMPANY,
                mention_text=entity_id, relevance=relevance,
            ))
        self.assertEqual(len(self.repo.get_mentions_for_article("a1")), 2)

    def test_reverse_lookup_articles_for_entity(self):
        for article_id in ("a1", "a2"):
            self.repo.save_mention(EntityMention(
                article_id=article_id, entity_id="nvidia", entity_type=EntityType.COMPANY,
                mention_text="NVIDIA",
            ))
        self.assertEqual(set(self.repo.get_articles_for_entity("nvidia")), {"a1", "a2"})

    def test_relevance_filter_restricts_to_primary(self):
        self.repo.save_mention(EntityMention(article_id="a1", entity_id="nvidia",
                                              entity_type=EntityType.COMPANY, mention_text="NVIDIA",
                                              relevance=MentionRelevance.PRIMARY))
        self.repo.save_mention(EntityMention(article_id="a2", entity_id="nvidia",
                                              entity_type=EntityType.COMPANY, mention_text="NVIDIA",
                                              relevance=MentionRelevance.MENTIONED))
        primary_only = self.repo.get_articles_for_entity("nvidia", min_relevance=MentionRelevance.PRIMARY)
        self.assertEqual(primary_only, ["a1"])

    def test_saving_same_mention_twice_is_idempotent(self):
        mention = EntityMention(article_id="a1", entity_id="nvidia",
                                 entity_type=EntityType.COMPANY, mention_text="NVIDIA")
        self.repo.save_mention(mention)
        self.repo.save_mention(mention)
        self.assertEqual(len(self.repo.get_mentions_for_article("a1")), 1)


class TestRelationshipPersistence(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()

    def test_save_and_retrieve_with_provenance(self):
        self.repo.save_relationship(EntityRelationship(
            from_entity_id="nvidia", to_entity_id="tsmc",
            relationship_type=RelationshipType.CUSTOMER_OF,
            source="NVIDIA 10-K 2025", observed_at=T2025,
        ))
        relationships = self.repo.get_relationships("nvidia")
        self.assertEqual(len(relationships), 1)
        self.assertEqual(relationships[0].source, "NVIDIA 10-K 2025")
        self.assertTrue(relationships[0].is_fact)

    def test_facts_only_filter_excludes_inference(self):
        self.repo.save_relationship(EntityRelationship(
            from_entity_id="a", to_entity_id="b", relationship_type=RelationshipType.COMPETITOR_OF,
            source="sector heuristic", provenance_kind=ProvenanceKind.INFERRED,
        ))
        self.assertEqual(len(self.repo.get_relationships("a")), 1)
        self.assertEqual(len(self.repo.get_relationships("a", facts_only=True)), 0)

    def test_same_relationship_from_two_sources_stored_separately(self):
        for source in ("SEC filing", "press release"):
            self.repo.save_relationship(EntityRelationship(
                from_entity_id="a", to_entity_id="b",
                relationship_type=RelationshipType.PARTNER_OF, source=source,
            ))
        self.assertEqual(len(self.repo.get_relationships("a")), 2)


class TestIdentityChangePersistence(unittest.TestCase):
    def test_save_and_retrieve_changes_chronologically(self):
        repo = new_repo()
        repo.save_identity_change(IdentityChange(
            entity_id="meta", change_type=IdentityChangeType.RENAME, effective_at=T2025,
            previous_value="Facebook", new_value="Meta Platforms", source="press release",
        ))
        changes = repo.get_identity_changes("meta")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].previous_value, "Facebook")

    def test_acquisition_retrievable_from_both_sides(self):
        repo = new_repo()
        repo.save_identity_change(IdentityChange(
            entity_id="target", change_type=IdentityChangeType.ACQUISITION,
            effective_at=T2025, related_entity_id="acquirer",
        ))
        self.assertEqual(len(repo.get_identity_changes("target")), 1)
        self.assertEqual(len(repo.get_identity_changes("acquirer")), 1)


class TestSectorClassificationConflicts(unittest.TestCase):
    """Spec §16: conflicting provider classifications must not silently overwrite each other."""

    def setUp(self):
        self.repo = new_repo()

    def test_two_providers_disagreeing_are_both_stored(self):
        self.repo.save_sector_classification(SectorClassification(
            entity_id="nvidia", sector_id="technology", source="internal", is_canonical=True,
        ))
        self.repo.save_sector_classification(SectorClassification(
            entity_id="nvidia", sector_id="semiconductors", source="provider-x",
        ))
        classifications = self.repo.get_sector_classifications("nvidia")
        self.assertEqual(len(classifications), 2)
        sectors = {c.sector_id for c in classifications}
        self.assertEqual(sectors, {"technology", "semiconductors"})

    def test_canonical_classification_identifiable(self):
        self.repo.save_sector_classification(SectorClassification(
            entity_id="nvidia", sector_id="technology", source="internal", is_canonical=True,
        ))
        self.repo.save_sector_classification(SectorClassification(
            entity_id="nvidia", sector_id="semiconductors", source="provider-x",
        ))
        canonical = self.repo.get_canonical_sector("nvidia")
        self.assertEqual(canonical.sector_id, "technology")

    def test_no_canonical_returns_none_rather_than_guessing(self):
        self.repo.save_sector_classification(SectorClassification(
            entity_id="x", sector_id="a", source="provider-1",
        ))
        self.assertIsNone(self.repo.get_canonical_sector("x"))

    def test_resaving_same_source_updates_rather_than_duplicating(self):
        for sector in ("technology", "semiconductors"):
            self.repo.save_sector_classification(SectorClassification(
                entity_id="nvidia", sector_id=sector, source="provider-x",
            ))
        classifications = self.repo.get_sector_classifications("nvidia")
        self.assertEqual(len(classifications), 1)
        self.assertEqual(classifications[0].sector_id, "semiconductors")


class TestCoexistenceWithExistingTables(unittest.TestCase):
    """The entity schema must not disturb anything the running application already uses."""

    def test_existing_recommendation_data_survives_entity_schema_creation(self):
        from recommendation_log import RecommendationLog
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            rec_log = RecommendationLog(db_path)
            rec_log.log_recommendations([{"entity": "Nvidia", "recommendation": "BUY", "confidence_score": 0.9}])
            rec_log.close()

            conn = sqlite3.connect(db_path)
            initialize_entity_schema(conn)
            conn.close()

            rec_log_after = RecommendationLog(db_path)
            rows = rec_log_after.load_all()
            rec_log_after.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["entity"], "Nvidia")
        finally:
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
