"""
src/domain/event_models.py
-------------------------------
Canonical Event model (Phase 4, spec §3, §10, §11, §12, §13, §14).

CORE DISTINCTION THIS FILE ENFORCES — ARTICLE != EVENT (spec §2):
one article may yield zero, one, or several events; several articles
may describe one event. StructuredEvent is therefore a first-class
entity with its own id, never a per-article record.

FACT VS INFERENCE (spec §10, mandatory): a StructuredEvent holds only
what was REPORTED. Interpretation ("this may raise capacity") lives in
a separate EventInference record, linked to the event but never merged
into it. There is deliberately no field on StructuredEvent that could
hold an interpretation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any

from src.events.taxonomy import EventCategory, EventType


def _require_utc(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    """Same UTC enforcement used across every canonical model since Phase 1 — naive/non-UTC timestamps are rejected, never guessed."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC (got offset {value.utcoffset()})")
    return value


class EventStatus(str, Enum):
    """
    Lifecycle of an event (spec §13). A first report is DETECTED, never
    CONFIRMED — confirmation requires corroboration, which is Phase 5's
    job, not something a single article can grant itself.
    """
    DETECTED = "detected"
    CONFIRMED = "confirmed"
    UPDATED = "updated"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    RETRACTED = "retracted"


class ConfidenceBand(str, Enum):
    """Human-readable band derived from the numeric score — see EventConfidence.band()."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionTier(str, Enum):
    """
    Which tier produced this event (spec §7, §26). Recorded so cost and
    quality can be measured per tier, and so an expensive tier's output
    is always distinguishable from a cheap one's.
    """
    DETERMINISTIC_RULE = "deterministic_rule"   # Tier 2 — keyword/pattern
    STRUCTURED_PROVIDER = "structured_provider"  # Tier 2 — provider-supplied event data
    STATISTICAL_NLP = "statistical_nlp"          # Tier 3 — reserved, not used in Phase 4
    SEMANTIC_MODEL = "semantic_model"            # Tier 4 — reserved, not used in Phase 4
    HUMAN_CORRECTION = "human_correction"        # spec §27


class ParticipationRole(str, Enum):
    """
    How an entity takes part in an event (spec §19). Deliberately
    distinct from "mentioned in the article" (Phase 3's
    MentionRelevance) and from "materially impacted" (a later phase) —
    conflating the three is exactly the error §19 warns against.
    """
    PRIMARY = "primary"          # the event is about this entity
    SECONDARY = "secondary"      # a named counterparty (e.g. the other side of a partnership)
    AFFECTED = "affected"        # explicitly named as affected by the event
    REFERENCED = "referenced"    # appears in the reporting, no established participation


class ArticleEventRelation(str, Enum):
    """How an article relates to an event (spec §15) — the foundation Phase 5's fusion will build on."""
    CREATES = "creates"
    UPDATES = "updates"
    CONFIRMS = "confirms"
    CONTRADICTS = "contradicts"
    REFERENCES = "references"


@dataclass
class EventEvidence:
    """
    Traceability for one supporting article (spec §9). An event with no
    evidence must never exist — StructuredEvent.validate() enforces it.

    `excerpt` holds a short supporting span only. Full article text is
    deliberately NOT stored here: licensing differs per provider (the
    same constraint already documented in Phase 2's RawArticle).
    """
    article_id: str
    source_id: Optional[str] = None
    source_name: Optional[str] = None
    published_at: Optional[datetime] = None
    excerpt: Optional[str] = None
    is_syndicated: bool = False   # spec §21: a syndicated copy is NOT independent confirmation

    def __post_init__(self):
        self.published_at = _require_utc(self.published_at, "published_at")


@dataclass
class EventConfidence:
    """
    Explainable confidence (spec §8). The score is never an arbitrary
    number: it is the weighted mean of named, individually-inspectable
    components, and `explain()` reproduces exactly how it was reached.
    """
    extraction_certainty: float = 0.0      # how unambiguous the extraction signal was
    entity_resolution_confidence: float = 0.0  # carried from Phase 3's resolver
    source_quality: float = 0.0            # from the source's credibility tier
    temporal_certainty: float = 0.0        # do we actually know when it happened?
    corroboration: float = 0.0             # independent sources — stays 0.0 until Phase 5 populates it

    #: Weights sum to 1.0. Corroboration is weighted but will read 0.0
    #: throughout Phase 4 — stated plainly rather than hidden, since
    #: that caps achievable confidence until Phase 5 exists.
    WEIGHTS = {
        "extraction_certainty": 0.35,
        "entity_resolution_confidence": 0.25,
        "source_quality": 0.20,
        "temporal_certainty": 0.10,
        "corroboration": 0.10,
    }

    def score(self) -> float:
        """Weighted mean of the components, in [0.0, 1.0]."""
        total = sum(getattr(self, name) * weight for name, weight in self.WEIGHTS.items())
        return round(min(1.0, max(0.0, total)), 4)

    def band(self) -> ConfidenceBand:
        s = self.score()
        if s >= 0.7:
            return ConfidenceBand.HIGH
        if s >= 0.4:
            return ConfidenceBand.MEDIUM
        return ConfidenceBand.LOW

    def explain(self) -> Dict[str, Any]:
        """Full breakdown — every component, its weight, and its contribution."""
        return {
            "score": self.score(),
            "band": self.band().value,
            "components": {
                name: {"value": getattr(self, name), "weight": weight,
                        "contribution": round(getattr(self, name) * weight, 4)}
                for name, weight in self.WEIGHTS.items()
            },
        }


@dataclass
class EventParticipant:
    """One entity's participation in an event, using Phase 3 canonical ids (spec §18: multi-entity events, stored once)."""
    entity_id: str
    role: ParticipationRole
    entity_type: str = "company"
    resolution_confidence: Optional[float] = None


@dataclass
class EventGeography:
    """Structured geography, all optional (spec §20: never force geographic fields when irrelevant)."""
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    facility: Optional[str] = None
    jurisdiction: Optional[str] = None

    def is_empty(self) -> bool:
        return not any([self.country, self.region, self.city, self.facility, self.jurisdiction])


@dataclass
class StructuredEvent:
    """
    A structured financial event: WHAT HAPPENED, as reported.

    Contains facts only. Any interpretation belongs in EventInference.
    """
    event_id: str
    event_type: EventType
    category: EventCategory
    subtype: Optional[str] = None

    title: str = ""
    description: str = ""      # factual restatement only — never an implication

    participants: List[EventParticipant] = field(default_factory=list)
    instrument_ids: List[str] = field(default_factory=list)
    sector_ids: List[str] = field(default_factory=list)
    geography: Optional[EventGeography] = None

    # --- the four distinct clocks (spec §12) ---
    event_time: Optional[datetime] = None        # when the occurrence itself happened, if known
    publication_time: Optional[datetime] = None  # when the earliest source published it
    ingestion_time: Optional[datetime] = None    # when we first held the article
    detection_time: Optional[datetime] = None    # when THIS engine extracted the event

    evidence: List[EventEvidence] = field(default_factory=list)
    confidence: EventConfidence = field(default_factory=EventConfidence)
    status: EventStatus = EventStatus.DETECTED
    extraction_tier: ExtractionTier = ExtractionTier.DETERMINISTIC_RULE

    attributes: Dict[str, Any] = field(default_factory=dict)  # event-type-specific (spec §11)
    fingerprint: Optional[str] = None
    supersedes_event_id: Optional[str] = None
    version: int = 1

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        for name in ("event_time", "publication_time", "ingestion_time",
                      "detection_time", "created_at", "updated_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    # --- convenience accessors ---

    def primary_entity_id(self) -> Optional[str]:
        for p in self.participants:
            if p.role == ParticipationRole.PRIMARY:
                return p.entity_id
        return None

    def secondary_entity_ids(self) -> List[str]:
        return [p.entity_id for p in self.participants if p.role == ParticipationRole.SECONDARY]

    def all_entity_ids(self) -> List[str]:
        return [p.entity_id for p in self.participants]

    def independent_source_count(self) -> int:
        """
        Count of DISTINCT non-syndicated sources (spec §21). Syndicated
        copies are excluded: five outlets running one wire story is one
        source, not five confirmations.
        """
        return len({e.source_id or e.source_name for e in self.evidence
                     if not e.is_syndicated and (e.source_id or e.source_name)})

    def validate(self) -> Optional[str]:
        """Return a stated reason if this event is not storable, else None. Enforces spec §9 (evidence is mandatory)."""
        if not self.evidence:
            return "event has no evidence — an untraceable event must never be created"
        if not self.participants:
            return "event has no participants"
        if self.publication_time is None:
            return "event has no publication time"
        return None


@dataclass
class EventInference:
    """
    An INTERPRETATION derived from an event — deliberately a separate
    record (spec §10). Nothing here is a fact; every field is a claim
    someone or something made, with its own provenance.

    Phase 4 does not create these. The type exists so that when a later
    phase does, there is no temptation to widen StructuredEvent instead.
    """
    inference_id: str
    event_id: str
    statement: str
    method: str                     # what produced it (rule name, model id, analyst, ...)
    confidence: Optional[Decimal] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")


@dataclass
class ArticleEventLink:
    """Explicit article -> event relationship (spec §15)."""
    article_id: str
    event_id: str
    relation: ArticleEventRelation = ArticleEventRelation.REFERENCES
    created_at: Optional[datetime] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")


@dataclass
class EventCorrection:
    """
    An auditable human correction (spec §27). Stored as its own record
    rather than an in-place edit, so the original extraction and the
    correction both remain inspectable.
    """
    correction_id: str
    event_id: str
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    corrected_by: str
    reason: Optional[str] = None
    corrected_at: Optional[datetime] = None

    def __post_init__(self):
        self.corrected_at = _require_utc(self.corrected_at, "corrected_at")


@dataclass
class ExtractionStats:
    """Per-run observability (spec §28). Contains no credentials by construction."""
    articles_processed: int = 0
    articles_filtered_out: int = 0     # Tier 1 cheap filter rejections
    events_detected: int = 0
    events_rejected: int = 0
    duplicates_detected: int = 0
    extraction_failures: int = 0
    llm_calls: int = 0                 # stays 0 in Phase 4 — no tier here calls a model
    by_event_type: Dict[str, int] = field(default_factory=dict)
    by_confidence_band: Dict[str, int] = field(default_factory=dict)
    duration_seconds: Optional[float] = None

    def as_log_dict(self) -> Dict[str, Any]:
        return {
            "articles_processed": self.articles_processed,
            "articles_filtered_out": self.articles_filtered_out,
            "events_detected": self.events_detected,
            "events_rejected": self.events_rejected,
            "duplicates_detected": self.duplicates_detected,
            "extraction_failures": self.extraction_failures,
            "llm_calls": self.llm_calls,
            "by_event_type": dict(self.by_event_type),
            "by_confidence_band": dict(self.by_confidence_band),
            "duration_seconds": self.duration_seconds,
        }
