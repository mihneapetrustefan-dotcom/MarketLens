"""
src/data_access/entity_repository.py
-----------------------------------------
Internal Data Access Layer for the Phase 3 entity tables.

The only code writing SQL against entity_aliases / entity_identifiers /
entity_mentions / entity_relationships / entity_identity_changes /
entity_sector_classifications.

SECTOR CLASSIFICATION NOTE (spec §16): conflicting classifications from
different providers are stored SIDE BY SIDE, keyed by source — a new
provider's opinion never silently overwrites an existing one. Exactly
one may be marked canonical.
"""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict

from src.domain.entity_models import (
    EntityAlias, EntityIdentifier, EntityMention, EntityRelationship,
    IdentityChange, SectorClassification,
    EntityType, AliasType, IdentifierType, MentionRelevance,
    ResolutionMethod, RelationshipType, ProvenanceKind, IdentityChangeType,
)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


class EntityRepository:
    """Persistence and fast lookup for entity aliases, identifiers, mentions, relationships and identity history."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # ---------------- aliases ----------------

    def save_alias(self, alias: EntityAlias) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_aliases
            (entity_id, entity_type, alias, normalized_alias, alias_type, ambiguity_risk, valid_from, valid_until)
            VALUES (?,?,?,?,?,?,?,?)
        """, (alias.entity_id, alias.entity_type.value, alias.alias, alias.normalized_alias,
               alias.alias_type.value, int(alias.ambiguity_risk),
               _iso(alias.valid_from), _iso(alias.valid_until)))
        self._conn.commit()

    def find_by_normalized_alias(self, normalized_alias: str) -> List[EntityAlias]:
        rows = self._conn.execute(
            "SELECT * FROM entity_aliases WHERE normalized_alias = ?", (normalized_alias,)
        ).fetchall()
        return [self._row_to_alias(r) for r in rows]

    def list_aliases(self) -> List[EntityAlias]:
        rows = self._conn.execute("SELECT * FROM entity_aliases").fetchall()
        return [self._row_to_alias(r) for r in rows]

    @staticmethod
    def _row_to_alias(row: sqlite3.Row) -> EntityAlias:
        return EntityAlias(
            entity_id=row["entity_id"], entity_type=EntityType(row["entity_type"]),
            alias=row["alias"], normalized_alias=row["normalized_alias"],
            alias_type=AliasType(row["alias_type"]), ambiguity_risk=bool(row["ambiguity_risk"]),
            valid_from=_parse_iso(row["valid_from"]), valid_until=_parse_iso(row["valid_until"]),
        )

    # ---------------- identifiers ----------------

    def save_identifier(self, identifier: EntityIdentifier) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_identifiers
            (entity_id, entity_type, identifier_type, value, provider, active)
            VALUES (?,?,?,?,?,?)
        """, (identifier.entity_id, identifier.entity_type.value, identifier.identifier_type.value,
               identifier.value, identifier.provider, int(identifier.active)))
        self._conn.commit()

    def find_by_identifier(self, identifier_type: IdentifierType, value: str) -> List[EntityIdentifier]:
        rows = self._conn.execute(
            "SELECT * FROM entity_identifiers WHERE identifier_type = ? AND value = ?",
            (identifier_type.value, value),
        ).fetchall()
        return [EntityIdentifier(
            entity_id=r["entity_id"], entity_type=EntityType(r["entity_type"]),
            identifier_type=IdentifierType(r["identifier_type"]), value=r["value"],
            provider=r["provider"], active=bool(r["active"]),
        ) for r in rows]

    def list_identifiers(self) -> List[EntityIdentifier]:
        rows = self._conn.execute("SELECT * FROM entity_identifiers").fetchall()
        return [EntityIdentifier(
            entity_id=r["entity_id"], entity_type=EntityType(r["entity_type"]),
            identifier_type=IdentifierType(r["identifier_type"]), value=r["value"],
            provider=r["provider"], active=bool(r["active"]),
        ) for r in rows]

    # ---------------- mentions ----------------

    def save_mention(self, mention: EntityMention) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_mentions
            (article_id, entity_id, entity_type, mention_text, relevance, confidence, method, position)
            VALUES (?,?,?,?,?,?,?,?)
        """, (mention.article_id, mention.entity_id, mention.entity_type.value, mention.mention_text,
               mention.relevance.value, str(mention.confidence), mention.method.value, mention.position))
        self._conn.commit()

    def get_mentions_for_article(self, article_id: str) -> List[EntityMention]:
        rows = self._conn.execute(
            "SELECT * FROM entity_mentions WHERE article_id = ?", (article_id,)
        ).fetchall()
        return [self._row_to_mention(r) for r in rows]

    def get_articles_for_entity(self, entity_id: str,
                                 min_relevance: Optional[MentionRelevance] = None) -> List[str]:
        """Article ids mentioning this entity, optionally restricted to primary/secondary prominence."""
        if min_relevance == MentionRelevance.PRIMARY:
            allowed = (MentionRelevance.PRIMARY.value,)
        elif min_relevance == MentionRelevance.SECONDARY:
            allowed = (MentionRelevance.PRIMARY.value, MentionRelevance.SECONDARY.value)
        else:
            allowed = None

        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            rows = self._conn.execute(
                f"SELECT DISTINCT article_id FROM entity_mentions WHERE entity_id = ? AND relevance IN ({placeholders})",
                (entity_id, *allowed),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT article_id FROM entity_mentions WHERE entity_id = ?", (entity_id,)
            ).fetchall()
        return [r["article_id"] for r in rows]

    @staticmethod
    def _row_to_mention(row: sqlite3.Row) -> EntityMention:
        return EntityMention(
            article_id=row["article_id"], entity_id=row["entity_id"],
            entity_type=EntityType(row["entity_type"]), mention_text=row["mention_text"],
            relevance=MentionRelevance(row["relevance"]), confidence=Decimal(row["confidence"]),
            method=ResolutionMethod(row["method"]), position=row["position"],
        )

    # ---------------- relationships ----------------

    def save_relationship(self, relationship: EntityRelationship) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_relationships
            (from_entity_id, to_entity_id, relationship_type, source, provenance_kind, confidence, observed_at, method, valid_until)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (relationship.from_entity_id, relationship.to_entity_id, relationship.relationship_type.value,
               relationship.source, relationship.provenance_kind.value, str(relationship.confidence),
               _iso(relationship.observed_at), relationship.method, _iso(relationship.valid_until)))
        self._conn.commit()

    def get_relationships(self, entity_id: str, facts_only: bool = False) -> List[EntityRelationship]:
        sql = "SELECT * FROM entity_relationships WHERE (from_entity_id = ? OR to_entity_id = ?)"
        params = [entity_id, entity_id]
        if facts_only:
            sql += " AND provenance_kind = ?"
            params.append(ProvenanceKind.SOURCED_FACT.value)
        rows = self._conn.execute(sql, params).fetchall()
        return [EntityRelationship(
            from_entity_id=r["from_entity_id"], to_entity_id=r["to_entity_id"],
            relationship_type=RelationshipType(r["relationship_type"]), source=r["source"],
            provenance_kind=ProvenanceKind(r["provenance_kind"]), confidence=Decimal(r["confidence"]),
            observed_at=_parse_iso(r["observed_at"]), method=r["method"],
            valid_until=_parse_iso(r["valid_until"]),
        ) for r in rows]

    # ---------------- identity changes ----------------

    def save_identity_change(self, change: IdentityChange) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_identity_changes
            (entity_id, change_type, effective_at, previous_value, new_value, related_entity_id, source, notes)
            VALUES (?,?,?,?,?,?,?,?)
        """, (change.entity_id, change.change_type.value, _iso(change.effective_at),
               change.previous_value, change.new_value, change.related_entity_id,
               change.source, change.notes))
        self._conn.commit()

    def get_identity_changes(self, entity_id: str) -> List[IdentityChange]:
        rows = self._conn.execute(
            "SELECT * FROM entity_identity_changes WHERE entity_id = ? OR related_entity_id = ? ORDER BY effective_at",
            (entity_id, entity_id),
        ).fetchall()
        return [IdentityChange(
            entity_id=r["entity_id"], change_type=IdentityChangeType(r["change_type"]),
            effective_at=_parse_iso(r["effective_at"]), previous_value=r["previous_value"],
            new_value=r["new_value"], related_entity_id=r["related_entity_id"],
            source=r["source"], notes=r["notes"],
        ) for r in rows]

    # ---------------- sector classification ----------------

    def save_sector_classification(self, classification: SectorClassification) -> None:
        """
        Store a classification FROM ONE SOURCE. Because the primary key
        is (entity_id, source), a second provider's differing opinion
        is stored alongside rather than overwriting the first.
        """
        self._conn.execute("""
            INSERT OR REPLACE INTO entity_sector_classifications
            (entity_id, sector_id, industry_id, source, is_canonical, effective_at)
            VALUES (?,?,?,?,?,?)
        """, (classification.entity_id, classification.sector_id, classification.industry_id,
               classification.source, int(classification.is_canonical), _iso(classification.effective_at)))
        self._conn.commit()

    def get_sector_classifications(self, entity_id: str) -> List[SectorClassification]:
        """EVERY classification on record for this entity, including conflicting ones."""
        rows = self._conn.execute(
            "SELECT * FROM entity_sector_classifications WHERE entity_id = ?", (entity_id,)
        ).fetchall()
        return [SectorClassification(
            entity_id=r["entity_id"], sector_id=r["sector_id"], industry_id=r["industry_id"],
            source=r["source"], is_canonical=bool(r["is_canonical"]),
            effective_at=_parse_iso(r["effective_at"]),
        ) for r in rows]

    def get_canonical_sector(self, entity_id: str) -> Optional[SectorClassification]:
        """The classification marked canonical, if one is."""
        for classification in self.get_sector_classifications(entity_id):
            if classification.is_canonical:
                return classification
        return None
