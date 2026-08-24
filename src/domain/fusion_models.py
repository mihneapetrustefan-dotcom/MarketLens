"""
src/domain/fusion_models.py
--------------------------------
Phase 5 domain models: Event Report vs Canonical Event, attribute-level
provenance, corroboration, contradiction, source lineage, clusters.

THE CENTRAL DISTINCTION (spec §2, §21) — four separate concepts that
must never collapse into each other:

    ARTICLE         source content            (Phase 2)
    EVENT REPORT    one source's CLAIM         (this phase)
    CANONICAL EVENT the real-world occurrence  (this phase)
    EVENT CLUSTER   a developing story         (this phase)

Phase 4's StructuredEvent becomes an EVENT REPORT here — one extracted
claim from one article. It is never destroyed by fusion: a
CanonicalEvent REFERENCES its contributing reports (spec §8).

THE OTHER LOAD-BEARING RULE (spec §9, §10): a later report never
silently overwrites an earlier one. Every consolidated attribute keeps
every reported value, each with its own provenance, and disagreement
is surfaced as contradiction rather than resolved by overwriting.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any, Set

from src.events.taxonomy import EventType, EventCategory
from src.domain.event_models import StructuredEvent, EventParticipant, EventGeography


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    """UTC enforcement, consistent with every canonical model since Phase 1."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


# ============================================================
# Fusion decisions
# ============================================================

class FusionDecisionState(str, Enum):
    """
    Spec §7. A false merge destroys information irrecoverably, while a
    missed merge merely leaves two rows — so the pipeline is biased
    toward NOT merging when uncertain, and POSSIBLE_SAME_EVENT never
    merges on its own.
    """
    SAME_EVENT = "same_event"
    POSSIBLE_SAME_EVENT = "possible_same_event"
    DIFFERENT_EVENT = "different_event"
    UNRESOLVED = "unresolved"
    NEEDS_REVIEW = "needs_review"


class ComparisonMethod(str, Enum):
    """How a fusion decision was reached — recorded so cost and reliability are measurable per method."""
    DETERMINISTIC_BLOCKING = "deterministic_blocking"
    STRUCTURED_COMPARISON = "structured_comparison"
    SEMANTIC_SIMILARITY = "semantic_similarity"
    LLM_ASSISTED = "llm_assisted"          # reserved; never used in Phase 5
    HUMAN_REVIEW = "human_review"


@dataclass
class FusionScore:
    """
    Explainable fusion score (spec §6). Never a bare number: every
    component is named, weighted, and reproducible via explain().

    UNKNOWN COMPONENTS ARE EXCLUDED, NOT SCORED NEUTRAL. A component is
    None when the comparison carries no information (neither side
    reports a geography, neither has instruments, no shared attributes
    to compare). Those components are dropped and the remaining
    weights are RENORMALIZED.

    Why this matters, concretely: scoring silence as a "neutral 0.5"
    caps the achievable total far below 1.0, so a textbook-perfect
    match (identical entities, identical type, minutes apart) tops out
    around 0.83 and never clears a 0.85 bar. That is not conservatism,
    it is a miscalibrated scale — the score must mean "how well do the
    things we actually know agree", not "how much did the source
    happen to tell us".

    SEMANTIC SIMILARITY is a token-overlap measure here, NOT an
    embedding or model call — stated plainly so the score is not
    mistaken for something more sophisticated than it is.
    """
    entity_match: Optional[float] = None
    event_type_match: Optional[float] = None
    temporal_proximity: Optional[float] = None
    geographic_similarity: Optional[float] = None
    attribute_similarity: Optional[float] = None
    instrument_similarity: Optional[float] = None
    semantic_similarity: Optional[float] = None

    WEIGHTS = {
        "entity_match": 0.30,
        "event_type_match": 0.25,
        "temporal_proximity": 0.15,
        "attribute_similarity": 0.10,
        "semantic_similarity": 0.10,
        "instrument_similarity": 0.05,
        "geographic_similarity": 0.05,
    }

    def informative_components(self) -> Dict[str, float]:
        """Only the components that actually carry a signal."""
        return {name: getattr(self, name) for name in self.WEIGHTS
                if getattr(self, name) is not None}

    def score(self) -> float:
        """Weighted mean over informative components only, with weights renormalized."""
        informative = self.informative_components()
        if not informative:
            return 0.0
        total_weight = sum(self.WEIGHTS[name] for name in informative)
        if total_weight == 0:
            return 0.0
        weighted = sum(value * self.WEIGHTS[name] for name, value in informative.items())
        return round(min(1.0, max(0.0, weighted / total_weight)), 4)

    def explain(self) -> Dict[str, Any]:
        informative = self.informative_components()
        total_weight = sum(self.WEIGHTS[name] for name in informative) or 1.0
        return {
            "score": self.score(),
            "components": {
                name: {
                    "value": round(value, 4),
                    "weight": self.WEIGHTS[name],
                    "effective_weight": round(self.WEIGHTS[name] / total_weight, 4),
                    "contribution": round(value * self.WEIGHTS[name] / total_weight, 4),
                }
                for name, value in informative.items()
            },
            "excluded_components": [name for name in self.WEIGHTS if getattr(self, name) is None],
        }


@dataclass
class FusionDecision:
    """
    One recorded comparison between a report and a canonical event
    (spec §3: every fusion decision must be explainable; §27:
    corrections never silently mutate history).
    """
    decision_id: str
    report_id: str
    canonical_event_id: Optional[str]
    state: FusionDecisionState
    score: FusionScore = field(default_factory=FusionScore)
    method: ComparisonMethod = ComparisonMethod.STRUCTURED_COMPARISON
    reason: str = ""
    decided_at: Optional[datetime] = None
    corrected_by_decision_id: Optional[str] = None   # set when a later correction supersedes this
    candidate_count: int = 0                          # how many candidates were considered

    def __post_init__(self):
        self.decided_at = _require_utc(self.decided_at, "decided_at")


# ============================================================
# Source quality, independence, lineage
# ============================================================

class SourceCategory(str, Enum):
    """
    Spec §11. Deliberately descriptive, NOT a ranking: nothing here
    asserts that one category is always correct — an official
    announcement can be self-serving, a specialist outlet can break a
    story first. Category feeds quality scoring, it does not decide truth.
    """
    OFFICIAL_COMPANY = "official_company"
    REGULATORY_FILING = "regulatory_filing"
    GOVERNMENT = "government"
    MAJOR_FINANCIAL_PRESS = "major_financial_press"
    SPECIALIZED_PRESS = "specialized_press"
    ANALYST_COMMENTARY = "analyst_commentary"
    SOCIAL_MEDIA = "social_media"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


class LineageRelation(str, Enum):
    """
    How one report derives from another (spec §12). UNKNOWN is a
    first-class value: claiming independence without evidence is the
    specific failure this enum exists to prevent.
    """
    ORIGINAL_REPORT = "original_report"
    SYNDICATES = "syndicates"
    DERIVED_FROM = "derived_from"
    REFERENCES = "references"
    QUOTES = "quotes"
    ATTRIBUTES_TO = "attributes_to"
    UNKNOWN = "unknown"


@dataclass
class SourceLineage:
    """One edge in the report-derivation graph. Absence of an edge means UNKNOWN, never 'independent'."""
    report_id: str
    relation: LineageRelation
    parent_report_id: Optional[str] = None
    parent_source_name: Optional[str] = None
    observed_at: Optional[datetime] = None
    evidence: Optional[str] = None      # what indicated this lineage (e.g. "attributed to Reuters in body")

    def __post_init__(self):
        self.observed_at = _require_utc(self.observed_at, "observed_at")

    def is_independent(self) -> bool:
        """Only an ORIGINAL_REPORT counts as independent. Everything else — including UNKNOWN — does not."""
        return self.relation == LineageRelation.ORIGINAL_REPORT


# ============================================================
# Corroboration & contradiction
# ============================================================

class CorroborationState(str, Enum):
    """
    Spec §13, §14. MULTI_SOURCE and INDEPENDENTLY_CORROBORATED are
    deliberately distinct states: ten outlets running one wire story is
    MULTI_SOURCE, not corroboration. OFFICIALLY_CONFIRMED is a
    different axis again — confirmation by an authoritative source,
    which does not require multiplicity.
    """
    UNCONFIRMED = "unconfirmed"
    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE = "multi_source"
    INDEPENDENTLY_CORROBORATED = "independently_corroborated"
    OFFICIALLY_CONFIRMED = "officially_confirmed"
    CONTRADICTED = "contradicted"
    RETRACTED = "retracted"


class ContradictionType(str, Enum):
    """Spec §15."""
    VALUE_CONFLICT = "value_conflict"
    STATUS_CONFLICT = "status_conflict"
    DATE_CONFLICT = "date_conflict"
    ENTITY_CONFLICT = "entity_conflict"
    EVENT_TYPE_CONFLICT = "event_type_conflict"
    DIRECT_DENIAL = "direct_denial"
    CANCELLATION = "cancellation"
    RETRACTION = "retraction"
    MATERIAL_ATTRIBUTE_CONFLICT = "material_attribute_conflict"


@dataclass
class ContradictionRecord:
    """
    A detected conflict between reports (spec §15). Stored, never
    resolved away — the canonical event surfaces the conflict rather
    than picking a winner.
    """
    contradiction_id: str
    canonical_event_id: str
    contradiction_type: ContradictionType
    report_id_a: str
    report_id_b: Optional[str] = None
    field_name: Optional[str] = None
    value_a: Optional[str] = None
    value_b: Optional[str] = None
    description: str = ""
    detected_at: Optional[datetime] = None
    resolved: bool = False
    resolution_note: Optional[str] = None

    def __post_init__(self):
        self.detected_at = _require_utc(self.detected_at, "detected_at")


# ============================================================
# Attribute-level provenance
# ============================================================

@dataclass
class AttributeValue:
    """
    ONE reported value for ONE attribute, with full provenance (spec
    §10). A canonical event holds a LIST of these per attribute — never
    a single overwritten value — so two sources disagreeing on a deal
    value both survive, each attributable.
    """
    value: Any
    report_id: str
    source_name: Optional[str] = None
    source_category: SourceCategory = SourceCategory.UNKNOWN
    confidence: Optional[float] = None
    extraction_method: Optional[str] = None
    reported_at: Optional[datetime] = None
    superseded: bool = False       # a later value from the SAME source supersedes this one

    def __post_init__(self):
        self.reported_at = _require_utc(self.reported_at, "reported_at")


@dataclass
class ConsolidatedAttribute:
    """
    Every value ever reported for one attribute of a canonical event.

    There is deliberately NO `final_value` field: choosing one would be
    exactly the silent overwrite spec §9/§10 forbids. `current_best()`
    offers a view, always alongside `has_conflict()`.
    """
    name: str
    values: List[AttributeValue] = field(default_factory=list)

    def add(self, value: AttributeValue) -> None:
        self.values.append(value)

    def has_conflict(self) -> bool:
        """True when two non-superseded values disagree — the caller must surface this, not hide it."""
        active = [v.value for v in self.values if not v.superseded]
        return len({str(v) for v in active}) > 1

    def distinct_values(self) -> List[Any]:
        seen, result = set(), []
        for v in self.values:
            if v.superseded:
                continue
            key = str(v.value)
            if key not in seen:
                seen.add(key)
                result.append(v.value)
        return result

    def current_best(self) -> Optional[AttributeValue]:
        """
        A VIEW, not a verdict: the most recent non-superseded value
        from the highest-authority source category available. Callers
        must still check has_conflict() — this never resolves a dispute.
        """
        active = [v for v in self.values if not v.superseded]
        if not active:
            return None
        authority = {
            SourceCategory.REGULATORY_FILING: 5, SourceCategory.OFFICIAL_COMPANY: 4,
            SourceCategory.GOVERNMENT: 4, SourceCategory.MAJOR_FINANCIAL_PRESS: 3,
            SourceCategory.SPECIALIZED_PRESS: 2, SourceCategory.ANALYST_COMMENTARY: 1,
            SourceCategory.AGGREGATOR: 1, SourceCategory.SOCIAL_MEDIA: 0, SourceCategory.UNKNOWN: 0,
        }
        return max(active, key=lambda v: (authority.get(v.source_category, 0),
                                           v.reported_at or datetime.min.replace(tzinfo=timezone.utc)))


# ============================================================
# Event lifecycle & timeline
# ============================================================

class EventLifecycleState(str, Enum):
    """
    Spec §16. Deliberately NOT a rigid single path — different event
    types evolve differently, so transitions are validated against a
    permitted-set map rather than a fixed sequence.
    """
    RUMORED = "rumored"
    REPORTED = "reported"
    CORROBORATED = "corroborated"
    CONFIRMED = "confirmed"
    UPDATED = "updated"
    COMPLETED = "completed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"


#: Permitted transitions. Terminal states have no outgoing edges except
#: RETRACTED, which any state can reach (a retraction can come at any time).
VALID_TRANSITIONS: Dict[EventLifecycleState, Set[EventLifecycleState]] = {
    EventLifecycleState.RUMORED: {EventLifecycleState.REPORTED, EventLifecycleState.CORROBORATED,
                                   EventLifecycleState.DENIED, EventLifecycleState.CANCELLED},
    EventLifecycleState.REPORTED: {EventLifecycleState.CORROBORATED, EventLifecycleState.CONFIRMED,
                                    EventLifecycleState.UPDATED, EventLifecycleState.DENIED,
                                    EventLifecycleState.CANCELLED, EventLifecycleState.SUPERSEDED},
    EventLifecycleState.CORROBORATED: {EventLifecycleState.CONFIRMED, EventLifecycleState.UPDATED,
                                        EventLifecycleState.DENIED, EventLifecycleState.CANCELLED,
                                        EventLifecycleState.SUPERSEDED},
    EventLifecycleState.CONFIRMED: {EventLifecycleState.UPDATED, EventLifecycleState.COMPLETED,
                                     EventLifecycleState.CANCELLED, EventLifecycleState.SUPERSEDED},
    EventLifecycleState.UPDATED: {EventLifecycleState.CONFIRMED, EventLifecycleState.COMPLETED,
                                   EventLifecycleState.CANCELLED, EventLifecycleState.SUPERSEDED,
                                   EventLifecycleState.DENIED},
    EventLifecycleState.COMPLETED: set(),
    EventLifecycleState.DENIED: {EventLifecycleState.REPORTED, EventLifecycleState.CONFIRMED},
    EventLifecycleState.CANCELLED: set(),
    EventLifecycleState.RETRACTED: set(),
    EventLifecycleState.SUPERSEDED: set(),
}


def is_valid_transition(current: EventLifecycleState, target: EventLifecycleState) -> bool:
    """Whether a lifecycle transition is permitted. RETRACTED is reachable from anywhere."""
    if target == EventLifecycleState.RETRACTED:
        return current != EventLifecycleState.RETRACTED
    return target in VALID_TRANSITIONS.get(current, set())


class TimelineEntryType(str, Enum):
    REPORT_ADDED = "report_added"
    STATE_CHANGE = "state_change"
    ATTRIBUTE_CHANGE = "attribute_change"
    CONTRADICTION = "contradiction"
    CONFIRMATION = "confirmation"
    FUSION_DECISION = "fusion_decision"
    CORRECTION = "correction"


@dataclass
class TimelineEntry:
    """One point in a canonical event's history (spec §17, §35) — what makes 'what did we believe at time T' reconstructable."""
    entry_id: str
    canonical_event_id: str
    entry_type: TimelineEntryType
    occurred_at: datetime
    description: str = ""
    report_id: Optional[str] = None
    source_name: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None

    def __post_init__(self):
        self.occurred_at = _require_utc(self.occurred_at, "occurred_at")


# ============================================================
# Event report & canonical event
# ============================================================

@dataclass
class EventReport:
    """
    ONE source's claim about an occurrence — Phase 4's StructuredEvent,
    reframed as what it actually is (spec §2). Never deleted by fusion.
    """
    report_id: str
    structured_event: StructuredEvent
    source_category: SourceCategory = SourceCategory.UNKNOWN
    language: Optional[str] = None            # spec §23: kept so identity isn't English-only
    lineage: Optional[SourceLineage] = None
    canonical_event_id: Optional[str] = None  # set once fused
    created_at: Optional[datetime] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")

    @property
    def event_type(self) -> EventType:
        return self.structured_event.event_type

    @property
    def entity_ids(self) -> List[str]:
        return self.structured_event.all_entity_ids()

    @property
    def publication_time(self) -> Optional[datetime]:
        return self.structured_event.publication_time

    @property
    def source_name(self) -> Optional[str]:
        ev = self.structured_event.evidence
        return ev[0].source_name if ev else None

    def is_independent(self) -> bool:
        """Independent only with positive evidence of being an original report."""
        return bool(self.lineage and self.lineage.is_independent())


class EventRelationType(str, Enum):
    """Spec §19. CAUSES is present but never asserted automatically — see EventRelation.is_inference."""
    RELATED_TO = "related_to"
    PRECEDES = "precedes"
    FOLLOWS = "follows"
    UPDATES = "updates"
    CONFIRMS = "confirms"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    CAUSES = "causes"
    RESULTS_FROM = "results_from"


@dataclass
class EventRelation:
    """
    An edge between two canonical events, with mandatory provenance
    (spec §19, §36). `is_inference` separates a sourced fact from a
    derived claim — CAUSES and RESULTS_FROM are always inferences
    unless a source states them outright.
    """
    relation_id: str
    from_event_id: str
    to_event_id: str
    relation_type: EventRelationType
    is_inference: bool = True
    source: Optional[str] = None
    method: Optional[str] = None
    confidence: Optional[float] = None
    observed_at: Optional[datetime] = None

    def __post_init__(self):
        self.observed_at = _require_utc(self.observed_at, "observed_at")
        # A causal claim is an inference unless explicitly sourced —
        # enforced structurally, not left to the caller's discipline.
        if self.relation_type in (EventRelationType.CAUSES, EventRelationType.RESULTS_FROM) and not self.source:
            self.is_inference = True


@dataclass
class CanonicalEvent:
    """
    The underlying real-world occurrence that one or more EventReports
    describe (spec §2).

    Holds NO market-impact notion of any kind: `quality_confidence`
    answers "how sure are we this event is correctly represented", and
    is deliberately separate from importance (spec §24, §25), which
    this phase does not compute at all.
    """
    canonical_event_id: str
    event_type: EventType
    category: EventCategory
    title: str = ""
    subtype: Optional[str] = None

    participants: List[EventParticipant] = field(default_factory=list)
    instrument_ids: List[str] = field(default_factory=list)
    sector_ids: List[str] = field(default_factory=list)
    geography: Optional[EventGeography] = None

    report_ids: List[str] = field(default_factory=list)
    attributes: Dict[str, ConsolidatedAttribute] = field(default_factory=dict)

    first_reported_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    event_time: Optional[datetime] = None

    lifecycle_state: EventLifecycleState = EventLifecycleState.REPORTED
    corroboration_state: CorroborationState = CorroborationState.SINGLE_SOURCE
    independent_source_count: int = 0
    total_report_count: int = 0
    has_contradictions: bool = False

    quality_confidence: float = 0.0
    fingerprint: Optional[str] = None
    cluster_id: Optional[str] = None
    version: int = 1

    def __post_init__(self):
        for name in ("first_reported_at", "last_updated_at", "event_time"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def primary_entity_id(self) -> Optional[str]:
        from src.domain.event_models import ParticipationRole
        for p in self.participants:
            if p.role == ParticipationRole.PRIMARY:
                return p.entity_id
        return None

    def entity_ids(self) -> List[str]:
        return [p.entity_id for p in self.participants]

    def add_attribute_value(self, name: str, value: AttributeValue) -> None:
        """Record a reported attribute value. NEVER overwrites — appends with provenance."""
        if name not in self.attributes:
            self.attributes[name] = ConsolidatedAttribute(name=name)
        self.attributes[name].add(value)

    def conflicting_attribute_names(self) -> List[str]:
        return [name for name, attr in self.attributes.items() if attr.has_conflict()]


@dataclass
class EventCluster:
    """
    A group of related events forming one developing story (spec §20,
    §37). Distinct from an event: clustering never merges its members.
    """
    cluster_id: str
    label: str = ""
    event_ids: List[str] = field(default_factory=list)
    primary_entity_ids: List[str] = field(default_factory=list)
    secondary_entity_ids: List[str] = field(default_factory=list)
    event_types: List[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    confidence: float = 0.0
    status: str = "active"
    created_at: Optional[datetime] = None

    def __post_init__(self):
        for name in ("started_at", "last_activity_at", "created_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def span_days(self) -> Optional[float]:
        if self.started_at and self.last_activity_at:
            return round((self.last_activity_at - self.started_at).total_seconds() / 86400, 2)
        return None


class ReviewReason(str, Enum):
    """Spec §28."""
    AMBIGUOUS_MATCH = "ambiguous_match"
    CONTRADICTION = "contradiction"
    LOW_CONFIDENCE_MERGE = "low_confidence_merge"
    CONFLICTING_ATTRIBUTES = "conflicting_attributes"
    POSSIBLE_FALSE_MERGE = "possible_false_merge"
    POSSIBLE_MISSED_MERGE = "possible_missed_merge"


@dataclass
class ReviewCase:
    """A case flagged for future human review (spec §28) — backend model only, no admin UI in this phase."""
    review_id: str
    reason: ReviewReason
    report_id: Optional[str] = None
    canonical_event_id: Optional[str] = None
    candidate_event_ids: List[str] = field(default_factory=list)
    description: str = ""
    status: str = "open"
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")
        self.resolved_at = _require_utc(self.resolved_at, "resolved_at")


@dataclass
class FusionStats:
    """Per-run observability (spec §32). No credentials by construction."""
    reports_processed: int = 0
    candidates_generated: int = 0
    same_event: int = 0
    possible_same_event: int = 0
    different_event: int = 0
    unresolved: int = 0
    needs_review: int = 0
    canonical_events_created: int = 0
    contradictions_detected: int = 0
    clusters_touched: int = 0
    llm_assisted_cases: int = 0        # stays 0 in Phase 5
    errors: int = 0
    duration_seconds: Optional[float] = None

    def as_log_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}
