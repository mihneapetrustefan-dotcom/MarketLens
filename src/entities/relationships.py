"""
src/entities/relationships.py
----------------------------------
Company relationship graph and corporate identity history (Phase 3,
§17, §18, §19).

TWO NON-NEGOTIABLE RULES ENFORCED HERE:

1. NO FABRICATED RELATIONSHIPS. A relationship cannot be stored
   without a stated source (enforced in EntityRelationship's own
   __post_init__). This module adds NO relationships of its own — it
   stores and queries what a caller supplies from a real source. There
   is deliberately no "infer competitors from shared sector" helper:
   that would manufacture plausible-looking relationships that nobody
   actually asserted.

2. FACT AND INFERENCE STAY SEPARABLE. Every query can filter to
   sourced facts only, so a downstream consumer is never silently fed
   an inference as though it were established.

IDENTITY CONTINUITY: renames, ticker changes, mergers and delistings
are recorded as timestamped events against a STABLE internal entity
id. Because the id never changes, historical article links survive a
rename — which is exactly what future quant research requires.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Set

from src.domain.entity_models import (
    EntityRelationship, RelationshipType, ProvenanceKind,
    IdentityChange, IdentityChangeType,
)

logger = logging.getLogger("marketlens.entities.relationships")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Relationship pairs that are logically symmetric — if A partners
#: with B, B partners with A. Used ONLY for querying convenience; the
#: reverse edge is never silently persisted as if separately sourced.
_SYMMETRIC = {RelationshipType.COMPETITOR_OF, RelationshipType.PARTNER_OF}

#: Directed inverses. Same rule: used for querying, never auto-stored.
_INVERSE = {
    RelationshipType.SUBSIDIARY_OF: RelationshipType.PARENT_OF,
    RelationshipType.PARENT_OF: RelationshipType.SUBSIDIARY_OF,
    RelationshipType.SUPPLIER_OF: RelationshipType.CUSTOMER_OF,
    RelationshipType.CUSTOMER_OF: RelationshipType.SUPPLIER_OF,
}


class RelationshipGraph:
    """
    In-memory relationship store with provenance-aware querying.

    Deliberately in-memory: the relationship set is currently EMPTY
    (MarketLens has no relationship data source yet), and building
    database infrastructure for data that does not exist would be
    speculative. Persisting it is a small, mechanical step once a real
    source is connected — see entity_repository.py's schema, which
    already has the table.
    """

    def __init__(self, relationships: Optional[List[EntityRelationship]] = None):
        self._relationships: List[EntityRelationship] = []
        for relationship in relationships or []:
            self.add(relationship)

    def add(self, relationship: EntityRelationship) -> None:
        """
        Store a relationship. Provenance is mandatory and already
        enforced by the model itself — a source-less relationship
        cannot be constructed, so it can never reach this method.
        """
        self._relationships.append(relationship)

    def add_all(self, relationships: List[EntityRelationship]) -> None:
        for relationship in relationships:
            self.add(relationship)

    def get_relationships(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        facts_only: bool = False,
        as_of: Optional[datetime] = None,
        include_inverse: bool = True,
    ) -> List[EntityRelationship]:
        """
        Every relationship involving `entity_id`.

        Args:
            facts_only: exclude inferred relationships entirely. A
                consumer that must not act on inference sets this.
            as_of: only relationships valid at that moment — a lapsed
                supplier relationship is correctly excluded from a
                historical query about a later date.
            include_inverse: also return edges where this entity is the
                TARGET (e.g. "who supplies me" as well as "whom I
                supply"), using the symmetric/inverse maps above.
        """
        results = []
        for relationship in self._relationships:
            involves_directly = relationship.from_entity_id == entity_id
            involves_inversely = include_inverse and relationship.to_entity_id == entity_id
            if not (involves_directly or involves_inversely):
                continue
            if facts_only and not relationship.is_fact:
                continue
            if as_of and relationship.valid_until and relationship.valid_until < as_of:
                continue
            if relationship_type:
                effective_type = relationship.relationship_type
                if involves_inversely and not involves_directly:
                    if effective_type in _SYMMETRIC:
                        pass
                    else:
                        effective_type = _INVERSE.get(effective_type, effective_type)
                if effective_type != relationship_type:
                    continue
            results.append(relationship)
        return results

    def get_related_entity_ids(self, entity_id: str, relationship_type: Optional[RelationshipType] = None,
                                facts_only: bool = False) -> Set[str]:
        """The distinct entity ids connected to this one."""
        related = set()
        for relationship in self.get_relationships(entity_id, relationship_type, facts_only):
            other = relationship.to_entity_id if relationship.from_entity_id == entity_id else relationship.from_entity_id
            related.add(other)
        return related

    @property
    def count(self) -> int:
        return len(self._relationships)

    @property
    def fact_count(self) -> int:
        return sum(1 for r in self._relationships if r.is_fact)

    @property
    def inference_count(self) -> int:
        return sum(1 for r in self._relationships if not r.is_fact)


class IdentityHistory:
    """
    Tracks corporate identity changes without ever destroying history.

    The internal entity id is the anchor: it never changes, so every
    historical link (articles, recommendations, market observations)
    remains valid across a rename, ticker change or merger.
    """

    def __init__(self, changes: Optional[List[IdentityChange]] = None):
        self._changes: List[IdentityChange] = list(changes or [])

    def record(self, change: IdentityChange) -> None:
        self._changes.append(change)
        logger.info("Identity change recorded: %s %s at %s",
                     change.entity_id, change.change_type.value, change.effective_at.isoformat())

    def changes_for(self, entity_id: str) -> List[IdentityChange]:
        """Every recorded change for an entity, oldest first."""
        return sorted(
            [c for c in self._changes if c.entity_id == entity_id or c.related_entity_id == entity_id],
            key=lambda c: c.effective_at,
        )

    def name_as_of(self, entity_id: str, as_of: datetime, current_name: str) -> str:
        """
        What this entity was CALLED at a past moment.

        Walks renames backwards from the current name, so an article
        from 2019 about "Facebook" can still be presented under the
        name in use at the time, while remaining linked to the same
        entity that is now "Meta Platforms".
        """
        renames = [c for c in self.changes_for(entity_id)
                    if c.change_type == IdentityChangeType.RENAME and c.effective_at > as_of]
        if not renames:
            return current_name
        earliest_after = min(renames, key=lambda c: c.effective_at)
        return earliest_after.previous_value or current_name

    def ticker_as_of(self, entity_id: str, as_of: datetime, current_ticker: str) -> str:
        """What this entity's ticker was at a past moment — same logic as name_as_of."""
        changes = [c for c in self.changes_for(entity_id)
                    if c.change_type == IdentityChangeType.TICKER_CHANGE and c.effective_at > as_of]
        if not changes:
            return current_ticker
        earliest_after = min(changes, key=lambda c: c.effective_at)
        return earliest_after.previous_value or current_ticker

    def is_active_as_of(self, entity_id: str, as_of: datetime) -> bool:
        """
        Whether the entity was still listed at that moment. A delisting
        does NOT delete the entity — it just marks the point after
        which it stopped trading, so historical data before it stays
        valid and usable.
        """
        for change in self.changes_for(entity_id):
            if change.entity_id == entity_id and change.change_type == IdentityChangeType.DELISTING:
                if change.effective_at <= as_of:
                    return False
        return True

    def successor_of(self, entity_id: str) -> Optional[str]:
        """
        The entity that absorbed this one in a merger/acquisition, if
        any — so a query about an acquired company can follow the chain
        forward without the original record being deleted.
        """
        for change in sorted(self._changes, key=lambda c: c.effective_at, reverse=True):
            if change.entity_id == entity_id and change.change_type in (
                IdentityChangeType.MERGER, IdentityChangeType.ACQUISITION
            ):
                return change.related_entity_id
        return None

    @property
    def count(self) -> int:
        return len(self._changes)
