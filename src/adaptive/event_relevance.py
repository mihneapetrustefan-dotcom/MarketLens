"""
src/adaptive/event_relevance.py
---------------------------------------------
Does this event reach this position, and does it touch the thesis?
(Phase 25.9I-F, spec sections 5, 6, 27.)

TWO QUESTIONS, ANSWERED SEPARATELY
--------------------------------------
    instrument relevance -- is the event about something we hold?
    thesis relevance     -- does it touch the REASON we hold it?

The second is the one that matters and the harder one. A company we own
can report an excellent quarter that has nothing to do with the
deleveraging thesis the position was opened under. Conflating the two
produces a system that revises a thesis because a headline was good.

HOW A RELATION IS ESTABLISHED, AND HOW IT IS NOT
----------------------------------------------------
    DIRECT       the event NAMES an entity that resolves to this
                 instrument -- a PRIMARY, SECONDARY or AFFECTED
                 participant. REFERENCED does not qualify: appearing in
                 the reporting is not participation.
    INDIRECT     only through a SOURCED `EntityRelationship`
                 (competitor, supplier, customer...). An inferred
                 relationship is not enough, and no relationship at all
                 is UNKNOWN, never INDIRECT.
    SECTOR       the instrument's sector is among the event's sectors.
    MARKET_WIDE  macro, market or geopolitical category.
    UNKNOWN      everything else, including a ticker that merely
                 appears in an article body.

Ticker substring matching is never used. `EventInstrumentResolver`
walks canonical tables only.

MEASURED LIMITATION
-----------------------
Audited 2026-09-30: this repository persists NO entity relationships --
there is no `entity_relationships` table, and `RelationshipGraph` is
built in memory from relationships nobody stores. So INDIRECT cannot
currently be established in production at all, and the classifier
returns UNKNOWN rather than reaching for a plausible-looking guess
about a supply chain.

THESIS RELEVANCE NEEDS A STATED MAPPING
-------------------------------------------
`PositionThesis` carries `risk_assumptions`, `event_assumptions` and
`regime_assumptions` as free text. The event taxonomy has no mapping to
free text and building one would be inventing semantics, so the
classifier accepts an EXPLICIT assumption -> event-type mapping from
the caller and returns UNKNOWN without one.

That distinction is load-bearing:

    no mapping supplied   -> UNKNOWN  (we cannot tell; look anyway)
    mapping supplied,
      nothing matches     -> NOT_RELEVANT  (we can tell, and it does not)

UNKNOWN still warrants a reassessment for a direct issuer event;
NOT_RELEVANT does not. That is the difference between "we don't know"
and "we know it doesn't matter", and collapsing them would either
silence the engine or make it chase every headline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.event_sources import EventInstrumentResolver
from src.domain.entity_models import ProvenanceKind, RelationshipType
from src.domain.event_models import ParticipationRole
from src.domain.event_position_models import (
    EventEvidenceBundle, EventPositionRelation, PositionEventRelevance,
    ThesisAssumptionImpact, ThesisRelevance,
)
from src.domain.impact_models import ImpactDirection
from src.domain.thesis_models import PositionThesis

#: Categories whose events are market-wide by construction.
MARKET_WIDE_CATEGORIES = frozenset({"macro", "geopolitical", "market"})

#: Participation roles that make an event ABOUT an entity. REFERENCED is
#: deliberately absent: being mentioned is not being involved.
PARTICIPATING_ROLES = frozenset({
    ParticipationRole.PRIMARY, ParticipationRole.SECONDARY,
    ParticipationRole.AFFECTED,
})

#: Relationship kinds that can carry an INDIRECT relation, and only when
#: recorded as a SOURCED_FACT.
INDIRECT_RELATIONSHIP_TYPES = frozenset({
    RelationshipType.COMPETITOR_OF, RelationshipType.SUPPLIER_OF,
    RelationshipType.CUSTOMER_OF, RelationshipType.PARTNER_OF,
    RelationshipType.SUBSIDIARY_OF, RelationshipType.PARENT_OF,
    RelationshipType.OWNS, RelationshipType.INVESTOR_IN,
})


@dataclass(frozen=True)
class RelevancePolicy:
    """
    Every threshold, all of them inert or explicit by default.

    `assumption_event_types` maps an assumption string (exactly as it
    appears on the thesis) to the event types that would bear on it.
    Supplied by the caller -- a fixture, a config file, or a future
    Signal & Horizon layer. Empty by default, because nothing in this
    repository can currently derive it.
    """
    assumption_event_types: Mapping[str, FrozenSet[str]] = field(
        default_factory=dict)
    #: When True, a sector event may reach MEDIUM thesis relevance if it
    #: matches a stated assumption. When False it is capped at LOW.
    sector_events_may_be_medium: bool = True
    #: Whether an event naming the issuer as SECONDARY (the other side of
    #: a partnership, say) counts as DIRECT. It does: the event names
    #: them. Configurable because a caller may want it stricter.
    secondary_participation_is_direct: bool = True

    def event_types_for(self, assumption: str) -> FrozenSet[str]:
        return frozenset(self.assumption_event_types.get(assumption, frozenset()))

    @property
    def has_assumption_mapping(self) -> bool:
        return bool(self.assumption_event_types)


class EventRelevanceClassifier:
    """
    Classifies one (event, thesis) pair. Deterministic; no clock, no I/O.

    Holds a resolver and, optionally, a relationship graph. Never
    queries a database during classification: every input arrives as an
    argument or was loaded when the resolver was built.
    """

    def __init__(self, resolver: EventInstrumentResolver,
                 relationship_graph: Optional[object] = None,
                 policy: Optional[RelevancePolicy] = None):
        self.resolver = resolver
        self.relationships = relationship_graph
        self.policy = policy or RelevancePolicy()

    # ---------------- relation ----------------

    def _participant_roles(self, bundle: EventEvidenceBundle,
                           instrument_id: str,
                           participant_roles: Optional[Mapping[str, str]] = None
                           ) -> Optional[ParticipationRole]:
        """
        The role under which this instrument's entity took part, if any.

        `participant_roles` (entity_id -> role) may be supplied by a
        caller that already has it; otherwise PRIMARY is inferred from
        the bundle's own `primary_entity_id` and any other participating
        entity is treated as SECONDARY.
        """
        target = str(instrument_id)
        if bundle.primary_entity_id:
            primary = self.resolver.resolve(bundle.primary_entity_id)
            if target in primary.instrument_ids:
                return ParticipationRole.PRIMARY
        for entity_id in bundle.entity_ids:
            if entity_id == bundle.primary_entity_id:
                continue
            resolution = self.resolver.resolve(entity_id)
            if target in resolution.instrument_ids:
                if participant_roles:
                    raw = str(participant_roles.get(entity_id, "")).lower()
                    try:
                        return ParticipationRole(raw)
                    except ValueError:
                        pass
                return ParticipationRole.SECONDARY
        return None

    def _indirect_evidence(self, bundle: EventEvidenceBundle,
                           entity_id: Optional[str]) -> Tuple[bool, str]:
        """
        Whether a SOURCED relationship links the event's entities to ours.

        Returns (found, evidence). An INFERRED relationship is rejected:
        a derived supply-chain claim is not a basis for touching a
        position.
        """
        if self.relationships is None or not entity_id:
            return False, ""
        getter = getattr(self.relationships, "get_relationships", None)
        if getter is None:
            return False, ""
        try:
            edges = getter(entity_id)
        except Exception:                                   # noqa: BLE001
            return False, ""
        for edge in edges or ():
            if getattr(edge, "provenance_kind", None) is not ProvenanceKind.SOURCED_FACT:
                continue
            if getattr(edge, "relationship_type", None) not in INDIRECT_RELATIONSHIP_TYPES:
                continue
            other = getattr(edge, "to_entity_id", None)
            if other in bundle.entity_ids or getattr(
                    edge, "from_entity_id", None) in bundle.entity_ids:
                return True, (f"{edge.relationship_type.value} "
                              f"(source: {getattr(edge, 'source', 'unknown')})")
        return False, ""

    def classify_relation(self, bundle: EventEvidenceBundle, *,
                          instrument_id: str,
                          entity_id: Optional[str] = None,
                          sector_id: Optional[str] = None,
                          participant_roles: Optional[Mapping[str, str]] = None
                          ) -> Tuple[EventPositionRelation,
                                     Optional[ParticipationRole], str]:
        """(relation, role, evidence). UNKNOWN whenever nothing establishes one."""
        role = self._participant_roles(bundle, instrument_id, participant_roles)
        if role in PARTICIPATING_ROLES:
            if (role is ParticipationRole.SECONDARY
                    and not self.policy.secondary_participation_is_direct):
                return (EventPositionRelation.UNKNOWN, role,
                        "named as a secondary participant; policy does not "
                        "treat that as direct")
            return (EventPositionRelation.DIRECT, role,
                    f"named as a {role.value} participant")
        if role is ParticipationRole.REFERENCED:
            return (EventPositionRelation.UNKNOWN, role,
                    "merely referenced in the reporting; that is not "
                    "participation")

        found, evidence = self._indirect_evidence(bundle, entity_id)
        if found:
            return EventPositionRelation.INDIRECT, role, evidence

        if sector_id and sector_id in bundle.sector_ids:
            return (EventPositionRelation.SECTOR, role,
                    f"the event affects sector {sector_id}")

        if bundle.event_category.lower() in MARKET_WIDE_CATEGORIES:
            return (EventPositionRelation.MARKET_WIDE, role,
                    f"{bundle.event_category} events are market-wide")

        return (EventPositionRelation.UNKNOWN, role,
                "no evidenced path from this event to this instrument")

    # ---------------- thesis relevance ----------------

    def _assumptions(self, thesis: PositionThesis) -> Tuple[str, ...]:
        return tuple(thesis.event_assumptions) + tuple(
            thesis.risk_assumptions) + tuple(thesis.regime_assumptions)

    def classify_thesis_relevance(
            self, bundle: EventEvidenceBundle, thesis: PositionThesis, *,
            relation: EventPositionRelation,
            direction: ImpactDirection = ImpactDirection.UNKNOWN
    ) -> Tuple[ThesisRelevance,
               Tuple[Tuple[str, ThesisAssumptionImpact], ...], Tuple[str, ...]]:
        """
        Which stated assumptions this event touches, and how hard.

        Matches on the canonical subtype first and the event type
        second, so `guidance_cut` can be mapped more narrowly than
        `guidance` when a caller wants that.
        """
        assumptions = self._assumptions(thesis)
        reasons: List[str] = []

        if not self.policy.has_assumption_mapping:
            reasons.append(
                "no assumption-to-event-type mapping is configured, so the "
                "effect on this thesis cannot be established")
            return ThesisRelevance.UNKNOWN, (), tuple(reasons)

        if not assumptions:
            reasons.append("the thesis states no assumptions to match against")
            return ThesisRelevance.UNKNOWN, (), tuple(reasons)

        impact = {
            ImpactDirection.POSITIVE: ThesisAssumptionImpact.STRENGTHENED,
            ImpactDirection.NEGATIVE: ThesisAssumptionImpact.WEAKENED,
        }.get(direction, ThesisAssumptionImpact.UNKNOWN)

        matched: List[Tuple[str, ThesisAssumptionImpact]] = []
        untouched: List[Tuple[str, ThesisAssumptionImpact]] = []
        for assumption in assumptions:
            wanted = self.policy.event_types_for(assumption)
            if wanted and (bundle.event_type in wanted):
                matched.append((assumption, impact))
            else:
                untouched.append((assumption, ThesisAssumptionImpact.NOT_RELEVANT))

        if not matched:
            reasons.append(
                f"a {bundle.event_type} event bears on none of the "
                f"{len(assumptions)} stated assumption(s)")
            return ThesisRelevance.NOT_RELEVANT, tuple(untouched), tuple(reasons)

        reasons.append(
            "affects stated assumption(s): "
            + ", ".join(name for name, _ in matched))

        if relation is EventPositionRelation.DIRECT:
            relevance = ThesisRelevance.HIGH
        elif relation is EventPositionRelation.SECTOR:
            relevance = (ThesisRelevance.MEDIUM
                         if self.policy.sector_events_may_be_medium
                         else ThesisRelevance.LOW)
        elif relation is EventPositionRelation.INDIRECT:
            relevance = ThesisRelevance.MEDIUM
        elif relation is EventPositionRelation.MARKET_WIDE:
            relevance = ThesisRelevance.LOW
        else:
            # An unknown relation cannot carry a relevance claim, even
            # when an assumption matches: the event may not reach this
            # position at all.
            reasons.append("the relation to this instrument is unknown")
            relevance = ThesisRelevance.UNKNOWN

        return relevance, tuple(matched + untouched), tuple(reasons)

    # ---------------- combined ----------------

    def classify(self, bundle: EventEvidenceBundle, thesis: PositionThesis, *,
                 direction: ImpactDirection = ImpactDirection.UNKNOWN,
                 entity_id: Optional[str] = None,
                 sector_id: Optional[str] = None,
                 participant_roles: Optional[Mapping[str, str]] = None
                 ) -> PositionEventRelevance:
        """One (event, thesis) relevance verdict, with its reasons."""
        relation, role, evidence = self.classify_relation(
            bundle, instrument_id=thesis.instrument_id, entity_id=entity_id,
            sector_id=sector_id, participant_roles=participant_roles)
        relevance, impacts, reasons = self.classify_thesis_relevance(
            bundle, thesis, relation=relation, direction=direction)

        confidence: Optional[float] = None
        if relation is EventPositionRelation.DIRECT:
            # The entity resolved through canonical tables; the
            # remaining uncertainty is the pipeline's own resolution
            # quality, which this corpus does not record per-participant.
            confidence = None

        return PositionEventRelevance(
            canonical_event_id=bundle.canonical_event_id,
            thesis_id=thesis.thesis_id,
            instrument_id=thesis.instrument_id,
            relation=relation, participation_role=role,
            entity_resolution_confidence=confidence,
            thesis_relevance=relevance, assumption_impacts=impacts,
            relation_evidence=evidence,
            reasons=(evidence,) + reasons if evidence else reasons)
