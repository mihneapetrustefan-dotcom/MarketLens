"""
tests/entities/test_relationships.py
-----------------------------------------
Tests for relationship provenance (fact vs inference, no fabrication)
and corporate identity continuity across renames/mergers/delistings.
"""

import sys
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.entities.relationships import RelationshipGraph, IdentityHistory
from src.domain.entity_models import (
    EntityRelationship, RelationshipType, ProvenanceKind,
    IdentityChange, IdentityChangeType,
)

T2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
T2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)
T2026 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def rel(from_id, to_id, rel_type=RelationshipType.SUPPLIER_OF, source="SEC 10-K filing",
        kind=ProvenanceKind.SOURCED_FACT, valid_until=None, observed_at=T2025):
    return EntityRelationship(
        from_entity_id=from_id, to_entity_id=to_id, relationship_type=rel_type,
        source=source, provenance_kind=kind, observed_at=observed_at, valid_until=valid_until,
    )


class TestProvenanceIsMandatory(unittest.TestCase):
    """Spec §17/§18: relationships must never be fabricated or source-less."""

    def test_relationship_without_source_cannot_be_created(self):
        with self.assertRaises(ValueError):
            EntityRelationship(from_entity_id="a", to_entity_id="b",
                                relationship_type=RelationshipType.PARTNER_OF, source="")

    def test_whitespace_only_source_is_rejected(self):
        with self.assertRaises(ValueError):
            EntityRelationship(from_entity_id="a", to_entity_id="b",
                                relationship_type=RelationshipType.PARTNER_OF, source="   ")

    def test_valid_source_is_accepted_and_recorded(self):
        relationship = rel("nvidia", "tsmc", source="NVIDIA annual report 2025")
        self.assertEqual(relationship.source, "NVIDIA annual report 2025")
        self.assertTrue(relationship.is_fact)

    def test_graph_starts_empty_nothing_is_invented(self):
        """No relationship exists until a real source supplies one."""
        graph = RelationshipGraph()
        self.assertEqual(graph.count, 0)
        self.assertEqual(graph.get_related_entity_ids("nvidia"), set())


class TestFactVsInferenceSeparation(unittest.TestCase):
    def setUp(self):
        self.graph = RelationshipGraph([
            rel("nvidia", "tsmc", source="SEC filing", kind=ProvenanceKind.SOURCED_FACT),
            rel("nvidia", "amd", RelationshipType.COMPETITOR_OF,
                source="sector-overlap heuristic", kind=ProvenanceKind.INFERRED),
        ])

    def test_both_are_stored(self):
        self.assertEqual(self.graph.count, 2)
        self.assertEqual(self.graph.fact_count, 1)
        self.assertEqual(self.graph.inference_count, 1)

    def test_facts_only_query_excludes_inference(self):
        facts = self.graph.get_relationships("nvidia", facts_only=True)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].to_entity_id, "tsmc")

    def test_default_query_includes_both_but_each_is_labelled(self):
        results = self.graph.get_relationships("nvidia")
        self.assertEqual(len(results), 2)
        kinds = {r.provenance_kind for r in results}
        self.assertEqual(kinds, {ProvenanceKind.SOURCED_FACT, ProvenanceKind.INFERRED})


class TestRelationshipQuerying(unittest.TestCase):
    def setUp(self):
        self.graph = RelationshipGraph([
            rel("nvidia", "tsmc", RelationshipType.CUSTOMER_OF),
            rel("nvidia", "amd", RelationshipType.COMPETITOR_OF),
            rel("parent-co", "sub-co", RelationshipType.PARENT_OF),
        ])

    def test_finds_relationships_where_entity_is_the_source(self):
        self.assertEqual(len(self.graph.get_relationships("nvidia")), 2)

    def test_finds_relationships_where_entity_is_the_target(self):
        results = self.graph.get_relationships("tsmc")
        self.assertEqual(len(results), 1)

    def test_inverse_can_be_excluded(self):
        self.assertEqual(len(self.graph.get_relationships("tsmc", include_inverse=False)), 0)

    def test_filter_by_relationship_type(self):
        results = self.graph.get_relationships("nvidia", relationship_type=RelationshipType.COMPETITOR_OF)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].to_entity_id, "amd")

    def test_inverse_type_mapping_for_directed_relationships(self):
        # parent-co is PARENT_OF sub-co, so from sub-co's side it is SUBSIDIARY_OF.
        results = self.graph.get_relationships("sub-co", relationship_type=RelationshipType.SUBSIDIARY_OF)
        self.assertEqual(len(results), 1)

    def test_related_entity_ids(self):
        self.assertEqual(self.graph.get_related_entity_ids("nvidia"), {"tsmc", "amd"})

    def test_expired_relationship_excluded_from_later_query(self):
        graph = RelationshipGraph([rel("a", "b", valid_until=T2025)])
        self.assertEqual(len(graph.get_relationships("a", as_of=T2024)), 1)
        self.assertEqual(len(graph.get_relationships("a", as_of=T2026)), 0)


class TestIdentityContinuity(unittest.TestCase):
    """Spec §19: identity changes must never destroy historical continuity."""

    def setUp(self):
        self.history = IdentityHistory()

    def test_rename_preserves_the_stable_entity_id(self):
        self.history.record(IdentityChange(
            entity_id="meta", change_type=IdentityChangeType.RENAME,
            effective_at=T2025, previous_value="Facebook", new_value="Meta Platforms",
        ))
        changes = self.history.changes_for("meta")
        self.assertEqual(len(changes), 1)
        # The id is unchanged — every historical link still resolves.
        self.assertEqual(changes[0].entity_id, "meta")

    def test_name_as_of_returns_the_historical_name(self):
        self.history.record(IdentityChange(
            entity_id="meta", change_type=IdentityChangeType.RENAME,
            effective_at=T2025, previous_value="Facebook", new_value="Meta Platforms",
        ))
        self.assertEqual(self.history.name_as_of("meta", T2024, "Meta Platforms"), "Facebook")
        self.assertEqual(self.history.name_as_of("meta", T2026, "Meta Platforms"), "Meta Platforms")

    def test_ticker_change_history(self):
        self.history.record(IdentityChange(
            entity_id="meta", change_type=IdentityChangeType.TICKER_CHANGE,
            effective_at=T2025, previous_value="FB", new_value="META",
        ))
        self.assertEqual(self.history.ticker_as_of("meta", T2024, "META"), "FB")
        self.assertEqual(self.history.ticker_as_of("meta", T2026, "META"), "META")

    def test_delisting_marks_inactive_without_deleting_the_entity(self):
        self.history.record(IdentityChange(
            entity_id="oldco", change_type=IdentityChangeType.DELISTING, effective_at=T2025,
        ))
        self.assertTrue(self.history.is_active_as_of("oldco", T2024))   # historical data still valid
        self.assertFalse(self.history.is_active_as_of("oldco", T2026))
        self.assertEqual(len(self.history.changes_for("oldco")), 1)      # record preserved

    def test_acquisition_links_to_successor_without_deleting_original(self):
        self.history.record(IdentityChange(
            entity_id="target-co", change_type=IdentityChangeType.ACQUISITION,
            effective_at=T2025, related_entity_id="acquirer-co", source="press release",
        ))
        self.assertEqual(self.history.successor_of("target-co"), "acquirer-co")
        self.assertEqual(len(self.history.changes_for("target-co")), 1)

    def test_entity_with_no_changes_keeps_its_current_identity(self):
        self.assertEqual(self.history.name_as_of("nvidia", T2024, "Nvidia"), "Nvidia")
        self.assertTrue(self.history.is_active_as_of("nvidia", T2026))
        self.assertIsNone(self.history.successor_of("nvidia"))

    def test_changes_returned_in_chronological_order(self):
        self.history.record(IdentityChange(entity_id="x", change_type=IdentityChangeType.TICKER_CHANGE, effective_at=T2026))
        self.history.record(IdentityChange(entity_id="x", change_type=IdentityChangeType.RENAME, effective_at=T2024))
        dates = [c.effective_at for c in self.history.changes_for("x")]
        self.assertEqual(dates, sorted(dates))

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            IdentityChange(entity_id="x", change_type=IdentityChangeType.RENAME,
                            effective_at=datetime(2025, 1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
