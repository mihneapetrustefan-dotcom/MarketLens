"""
src/domain/event_position_models.py
-------------------------------------------------
What an event means for a position we already hold (Phase 25.9I-F).

THE QUESTION THIS LAYER ASKS
--------------------------------
Not "is this news good or bad", which is what a headline reader asks.
It asks: **does this change the reason we own this?**

Those are different questions and they have different answers. A
company can announce a genuinely excellent quarter that has nothing to
do with the deleveraging thesis under which the position was opened.
The event is positive; the thesis is untouched. Conflating the two is
how a system ends up trading headlines.

WHAT IS REUSED RATHER THAN REDEFINED
----------------------------------------
Audited 2026-09-30. Almost every vocabulary this phase needs already
exists, so almost nothing here is new:

    ImpactDirection        positive/negative/mixed/neutral/unknown
                           -- exactly the five the spec asks for
    ImpactLevel            low/medium/high/unknown -- exactly the four
    ParticipationRole      primary/secondary/affected/referenced
                           -- canonical issuer directness
    CorroborationState     unconfirmed .. independently_corroborated
    SourceLineage          only ORIGINAL_REPORT counts as independent
    EventLifecycleState    rumored -> reported -> confirmed -> ...
    RelationshipType       competitor/supplier/customer/... WITH
                           mandatory provenance
    EventType/EventCategory   the canonical taxonomy

Defining a second `EventDirection` beside `ImpactDirection` would have
produced two enums with identical members and a mapping function
between them whose only purpose was to lose information at the seam.

WHAT IS GENUINELY NEW, AND WHY
----------------------------------
    PositionEventRelevance        instrument relevance and THESIS
                                  relevance are different facts
    PositionEventImpactAssessment the per-(event, thesis) verdict
    EventUrgency                  how fast a human or engine must look
    EvidenceConflictState         credible evidence pointing both ways
    EventConsumptionStatus        has this thesis already reacted?
    ThesisAssumptionImpact        which assumption moved, and how

THREE PROHIBITIONS, ENFORCED IN THE CONTRACTS
-------------------------------------------------
`PositionEventImpactAssessment` has **no field** for a revised target
zone, a revised horizon or an exit fraction. Not "defaults to None" --
absent. Event intelligence cannot supply what it has no field to carry,
so the prohibition survives a careless caller. 25.9I-E already accepts
those three from an explicit external estimator, and that seam is
unchanged.

UNKNOWN IS A RESULT
-----------------------
Measured on this repository's 581 canonical events: `event_time` is
NULL for every one, and `independent_source_count` is 0 for every one.
So occurrence time and source independence are genuinely unknown here,
and every contract below can say so without pretending otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Tuple

from src.domain.adaptive_models import (
    MarketDataState, _require_fraction, require_utc,
)
from src.domain.event_models import ParticipationRole
from src.domain.fusion_models import CorroborationState
from src.domain.impact_models import ImpactDirection, ImpactLevel
from src.domain.thesis_models import EvidenceDirection

# ======================================================================
# How an event reaches a position
# ======================================================================


class EventPositionRelation(str, Enum):
    """
    The evidenced path from an event to an instrument we hold.

    Named `EventPositionRelation` rather than `EventRelation` because
    `fusion_models.EventRelation` already means something else -- an
    edge between two events. Two different things with one name in one
    codebase is a bug waiting for a busy afternoon.

    UNKNOWN is the default and the safe answer. A ticker appearing in
    an article body is not a relation; only a resolved entity that maps
    to an instrument, or a sourced relationship, establishes one.
    """
    DIRECT = "direct"
    INDIRECT = "indirect"
    SECTOR = "sector"
    MARKET_WIDE = "market_wide"
    UNKNOWN = "unknown"

    @property
    def is_issuer(self) -> bool:
        """Only DIRECT means "this event is about the company we own"."""
        return self is EventPositionRelation.DIRECT

    @property
    def rank(self) -> int:
        return _RELATION_RANK[self]


_RELATION_RANK: Dict[EventPositionRelation, int] = {
    EventPositionRelation.DIRECT: 0,
    EventPositionRelation.INDIRECT: 1,
    EventPositionRelation.SECTOR: 2,
    EventPositionRelation.MARKET_WIDE: 3,
    EventPositionRelation.UNKNOWN: 4,
}


class ThesisRelevance(str, Enum):
    """
    Whether the event touches the REASON the position is held.

    Deliberately separate from instrument relevance. The canonical
    example, and the one the tests use:

        thesis: margin expansion
        event:  guidance shows margin compression   -> HIGH

        thesis: deleveraging
        event:  minor app redesign                  -> NOT_RELEVANT

    UNKNOWN is returned whenever the thesis assumptions are not stated
    in a form the event taxonomy can be matched against -- which, in
    this repository today, is most of the time. Saying so is the point.
    """
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOT_RELEVANT = "not_relevant"
    UNKNOWN = "unknown"


class ThesisAssumptionImpact(str, Enum):
    """What an event did to one stated assumption. Minimal and extensible."""
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    INVALIDATED = "invalidated"
    NOT_RELEVANT = "not_relevant"
    UNKNOWN = "unknown"


class EventUrgency(str, Enum):
    """
    How soon someone must look. NEVER how soon someone must sell.

    CRITICAL means REASSESS NOW. It does not mean SELL NOW, and the
    engine that produces it cannot sell anything -- every recommendation
    downstream is non-executable.
    """
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """0 = least urgent. Explicit, so comparisons are not string order."""
        return {"normal": 0, "elevated": 1, "critical": 2}[self.value]

    @property
    def requested_market_data_state(self) -> MarketDataState:
        """
        What data freshness this urgency would ASK for (25.9I-C owns
        whether it is granted, and the account has never served realtime).
        """
        return {
            EventUrgency.NORMAL: MarketDataState.COLD,
            EventUrgency.ELEVATED: MarketDataState.WARM,
            EventUrgency.CRITICAL: MarketDataState.HOT,
        }[self]


class EvidenceConflictState(str, Enum):
    """
    Whether the evidence agrees with itself.

    CONFLICTED is never resolved by counting headlines, by source order
    or by last-write-wins. It is reported, uncertainty is raised, and a
    reassessment is requested. A confident direction manufactured from
    a 3-to-2 headline split is worse than admitting the split.
    """
    CONSISTENT_POSITIVE = "consistent_positive"
    CONSISTENT_NEGATIVE = "consistent_negative"
    CONFLICTED = "conflicted"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class EventConsumptionStatus(str, Enum):
    """
    Has this thesis already reacted to this event?

    The difference between UPDATED and MATERIAL_REVISION is the whole
    point: a rumour that becomes a confirmed filing deserves a second
    look; the same rumour re-crawled tomorrow does not.
    """
    NEW = "new"
    ALREADY_ASSESSED = "already_assessed"
    UPDATED = "updated"
    MATERIAL_REVISION = "material_revision"
    STALE = "stale"
    IGNORED = "ignored"

    @property
    def permits_reassessment(self) -> bool:
        return self in (EventConsumptionStatus.NEW,
                        EventConsumptionStatus.MATERIAL_REVISION)


class DataIntegrityConcern(str, Enum):
    """
    A problem with our own data, which is NOT a bearish market event.

    A stale quote is not bad news about the company. Translating data
    uncertainty into NEGATIVE direction would make the system sell on
    its own outages, so these are carried on a separate axis entirely.
    """
    STALE_QUOTE = "stale_quote"
    UNRESOLVED_ENTITY = "unresolved_entity"
    TIMESTAMP_CONFLICT = "timestamp_conflict"
    MISSING_EVENT_TIME = "missing_event_time"
    RECONCILIATION_UNCERTAINTY = "reconciliation_uncertainty"
    MAPPING_AMBIGUOUS = "mapping_ambiguous"


# ======================================================================
# Evidence bundle -- what is known about the event itself
# ======================================================================


@dataclass(frozen=True)
class EventEvidenceBundle:
    """
    Everything known about one canonical event at one `as_of`, with its
    provenance. Read-only, as-of bounded, and never widened later.

    `source_independence_known` is a separate boolean from
    `independent_source_count` for a measured reason: in this corpus the
    count is 0 for all 581 events, and 0 could mean "checked, none" or
    "never established". It is the second, and a consumer must be able
    to tell the difference before treating five reports as five
    confirmations.
    """
    canonical_event_id: str
    event_type: str
    event_category: str
    as_of: datetime
    title: str = ""
    #: The canonical subtype the existing extractor derived, e.g.
    #: `guidance_cut`. The ONLY evidenced source of direction in this
    #: repository, and present on just 41 of 581 events -- of which 27
    #: are directional. Carried here so direction can be read from
    #: evidence rather than guessed from the event type.
    subtype: str = ""
    lifecycle_state: str = ""
    corroboration_state: CorroborationState = CorroborationState.UNCONFIRMED
    total_report_count: int = 0
    independent_source_count: int = 0
    source_independence_known: bool = False
    has_contradictions: bool = False
    quality_confidence: Optional[float] = None
    event_time: Optional[datetime] = None
    first_reported_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    is_scheduled: bool = False
    revision_key: str = ""
    entity_ids: Tuple[str, ...] = ()
    primary_entity_id: Optional[str] = None
    instrument_ids: Tuple[str, ...] = ()
    sector_ids: Tuple[str, ...] = ()
    report_ids: Tuple[str, ...] = ()
    integrity_concerns: Tuple[DataIntegrityConcern, ...] = ()

    def __post_init__(self):
        if not str(self.canonical_event_id).strip():
            raise ValueError("canonical_event_id is required")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        for name in ("event_time", "first_reported_at", "last_updated_at",
                     "first_seen_at"):
            object.__setattr__(self, name, require_utc(getattr(self, name), name))
        _require_fraction(self.quality_confidence, "quality_confidence")
        for name in ("total_report_count", "independent_source_count"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.independent_source_count and not self.source_independence_known:
            raise ValueError(
                "an independent source count cannot be reported while source "
                "independence is unknown; that is how syndication becomes "
                "confirmation")
        # No-lookahead, enforced structurally rather than by convention.
        for name in ("event_time", "first_reported_at", "first_seen_at"):
            moment = getattr(self, name)
            if moment is not None and moment > self.as_of:
                raise ValueError(
                    f"{name} is after as_of; an assessment cannot see the future")

    @property
    def known_occurrence_at(self) -> Optional[datetime]:
        """
        The best evidenced moment the event happened.

        Falls back to first report, because `event_time` is NULL for
        every canonical event in this repository. The fallback is
        reported through `occurrence_is_inferred` rather than hidden.
        """
        return self.event_time or self.first_reported_at

    @property
    def occurrence_is_inferred(self) -> bool:
        return self.event_time is None and self.first_reported_at is not None

    @property
    def is_multi_report(self) -> bool:
        return self.total_report_count > 1

    @property
    def is_independently_corroborated(self) -> bool:
        """
        Five syndicated copies are not five confirmations.

        True only when independence was actually established AND the
        canonical corroboration state agrees.
        """
        return (self.source_independence_known
                and self.independent_source_count > 1
                and self.corroboration_state in (
                    CorroborationState.INDEPENDENTLY_CORROBORATED,
                    CorroborationState.OFFICIALLY_CONFIRMED))

    def age_seconds(self, now: datetime) -> Optional[float]:
        moment = self.known_occurrence_at
        if moment is None:
            return None
        return (require_utc(now, "now") - moment).total_seconds()


# ======================================================================
# Relevance
# ======================================================================


@dataclass(frozen=True)
class PositionEventRelevance:
    """
    Two relevance questions, answered separately and kept separate.

    `instrument_relevance` -- is this event about something we hold?
    `thesis_relevance`     -- does it touch why we hold it?

    Collapsing them would make "big news about a company we own, which
    has nothing to do with our reason for owning it" inexpressible, and
    that is the single most common case.
    """
    canonical_event_id: str
    thesis_id: str
    instrument_id: str
    relation: EventPositionRelation = EventPositionRelation.UNKNOWN
    participation_role: Optional[ParticipationRole] = None
    entity_resolution_confidence: Optional[float] = None
    thesis_relevance: ThesisRelevance = ThesisRelevance.UNKNOWN
    assumption_impacts: Tuple[Tuple[str, ThesisAssumptionImpact], ...] = ()
    relation_evidence: str = ""
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        _require_fraction(self.entity_resolution_confidence,
                          "entity_resolution_confidence")
        if (self.relation is EventPositionRelation.INDIRECT
                and not str(self.relation_evidence).strip()):
            # An indirect relation is a CLAIM about corporate structure.
            # Without a sourced relationship it is a guess, and guesses
            # about supply chains move real positions.
            raise ValueError(
                "an INDIRECT relation requires relation_evidence naming the "
                "sourced relationship it rests on")

    @property
    def is_relevant_to_thesis(self) -> bool:
        """Relevance that was actually established. UNKNOWN is not relevance."""
        return self.thesis_relevance in (ThesisRelevance.HIGH,
                                         ThesisRelevance.MEDIUM,
                                         ThesisRelevance.LOW)

    @property
    def warrants_reassessment(self) -> bool:
        """
        Whether this deserves a second look, which is a weaker test.

        A DIRECT issuer event on something we hold warrants a look even
        when nobody can prove which assumption it touches -- and in this
        repository nobody usually can, because thesis assumptions are
        free text and the event taxonomy has no mapping to them.

        The distinction matters downstream: `is_relevant_to_thesis`
        gates a directional REVISION, this gates a REASSESSMENT. An
        unmapped event therefore prompts a fresh look without pushing
        the thesis in either direction, which is the honest behaviour
        when relevance is genuinely unknown.

        NOT_RELEVANT is excluded: once relevance has been established as
        absent, re-examining on every headline is the overreaction this
        phase exists to prevent.
        """
        if self.thesis_relevance is ThesisRelevance.NOT_RELEVANT:
            return False
        return self.is_relevant_to_thesis or self.relation.is_issuer

    @property
    def affected_assumptions(self) -> Tuple[str, ...]:
        """
        Assumptions this event bears on, whichever way.

        UNKNOWN impact is INCLUDED: an event that matches an assumption
        but has no established direction has still touched it, and we
        simply cannot say how. Excluding those would report "nothing
        affected" for the ~95% of this corpus that carries no
        directional subtype -- which is the opposite of what happened.
        """
        return tuple(name for name, impact in self.assumption_impacts
                     if impact is not ThesisAssumptionImpact.NOT_RELEVANT)

    def describe(self) -> Dict[str, object]:
        return {
            "canonical_event_id": self.canonical_event_id,
            "thesis_id": self.thesis_id,
            "instrument_id": self.instrument_id,
            "relation": self.relation.value,
            "participation_role": (self.participation_role.value
                                   if self.participation_role else None),
            "entity_resolution_confidence": self.entity_resolution_confidence,
            "thesis_relevance": self.thesis_relevance.value,
            "assumption_impacts": [[n, i.value] for n, i in self.assumption_impacts],
            "reasons": list(self.reasons),
        }


# ======================================================================
# Confidence, kept decomposed
# ======================================================================


@dataclass(frozen=True)
class EventAssessmentConfidence:
    """
    Four confidences that are four different questions.

    Never collapsed into one number. A perfectly-resolved entity
    attached to a shakily-extracted fact is not "medium confidence"; it
    is high on one axis and low on another, and the consumer needs to
    know which.

    `overall()` returns the MINIMUM of what is known, not an average.
    Averaging lets a strong axis carry a weak one, and the weak axis is
    usually the one that matters.
    """
    entity_match: Optional[float] = None
    event_extraction: Optional[float] = None
    fact: Optional[float] = None
    materiality: Optional[float] = None
    thesis_impact: Optional[float] = None

    def __post_init__(self):
        for name in ("entity_match", "event_extraction", "fact", "materiality",
                     "thesis_impact"):
            _require_fraction(getattr(self, name), name)

    @property
    def known_axes(self) -> Tuple[str, ...]:
        return tuple(name for name in ("entity_match", "event_extraction", "fact",
                                       "materiality", "thesis_impact")
                     if getattr(self, name) is not None)

    @property
    def completeness(self) -> float:
        return round(len(self.known_axes) / 5.0, 6)

    def overall(self) -> Optional[float]:
        """The weakest known axis, or None when nothing was measured."""
        values = [getattr(self, name) for name in self.known_axes]
        return min(values) if values else None

    def describe(self) -> Dict[str, object]:
        return {
            "entity_match": self.entity_match,
            "event_extraction": self.event_extraction,
            "fact": self.fact,
            "materiality": self.materiality,
            "thesis_impact": self.thesis_impact,
            "overall_is_weakest_axis": self.overall(),
            "completeness": self.completeness,
            "known_axes": list(self.known_axes),
        }


# ======================================================================
# The assessment
# ======================================================================


@dataclass(frozen=True)
class PositionEventImpactAssessment:
    """
    What one canonical event means for one thesis. The phase's output.

    NOTICE WHAT IS NOT HERE: no revised target zone, no revised horizon,
    no exit fraction. Those fields are absent rather than optional, so
    event intelligence physically cannot supply them. 25.9I-E accepts
    all three from an explicit external estimator, and that is the only
    route they may take.

    `direction` reuses `ImpactDirection`, which carries MIXED and
    UNKNOWN. 25.9I-E's `EvidenceDirection` carries only three values, so
    `evidence_direction()` maps MIXED and UNKNOWN to NEUTRAL -- material
    but directionless. That is deliberately lossy in the safe direction:
    a thesis reassesses without being pushed either way.
    """
    assessment_id: str
    canonical_event_id: str
    thesis_id: str
    instrument_id: str
    as_of: datetime
    relevance: PositionEventRelevance
    direction: ImpactDirection = ImpactDirection.UNKNOWN
    materiality: ImpactLevel = ImpactLevel.UNKNOWN
    materiality_score: Optional[float] = None
    confidence: EventAssessmentConfidence = field(
        default_factory=EventAssessmentConfidence)
    corroboration_state: CorroborationState = CorroborationState.UNCONFIRMED
    source_independence_known: bool = False
    conflict_state: EvidenceConflictState = EvidenceConflictState.UNKNOWN
    urgency: EventUrgency = EventUrgency.NORMAL
    consumption_status: EventConsumptionStatus = EventConsumptionStatus.NEW
    strengthens_thesis: bool = False
    weakens_thesis: bool = False
    invalidates_thesis: bool = False
    target_reestimation_required: bool = False
    horizon_reestimation_required: bool = False
    partial_exit_review_required: bool = False
    data_integrity_review_required: bool = False
    integrity_concerns: Tuple[DataIntegrityConcern, ...] = ()
    requested_market_data_state: MarketDataState = MarketDataState.COLD
    event_revision_key: str = ""
    evidence_completeness: float = 0.0
    is_stale: bool = False
    reasons: Tuple[str, ...] = ()
    # lineage, carried never derived
    lineage_id: Optional[str] = None
    session_snapshot_id: Optional[str] = None
    allocation_plan_id: Optional[str] = None
    previous_assessment_id: Optional[str] = None

    def __post_init__(self):
        if not str(self.assessment_id).strip():
            raise ValueError("assessment_id is required")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        _require_fraction(self.materiality_score, "materiality_score")
        _require_fraction(self.evidence_completeness, "evidence_completeness")

        if self.strengthens_thesis and self.weakens_thesis:
            raise ValueError(
                "an assessment cannot both strengthen and weaken a thesis; "
                "evidence pointing both ways is CONFLICTED")
        if self.invalidates_thesis and self.strengthens_thesis:
            raise ValueError(
                "an assessment cannot invalidate and strengthen a thesis")
        # Invalidation is the heaviest claim this layer can make, so it
        # is the one that must be evidenced rather than asserted.
        if self.invalidates_thesis:
            if self.materiality is not ImpactLevel.HIGH:
                raise ValueError(
                    "invalidation requires HIGH materiality; a low-materiality "
                    "event cannot end a thesis")
            if self.direction is not ImpactDirection.NEGATIVE:
                raise ValueError(
                    "invalidation requires a NEGATIVE direction; a mixed or "
                    "unknown reading is a reason to reassess, not to conclude")
            if not self.relevance.relation.is_issuer:
                raise ValueError(
                    "invalidation requires a DIRECT issuer relation; a sector "
                    "or indirect event does not invalidate a company thesis")
        if (self.data_integrity_review_required
                and self.direction is ImpactDirection.NEGATIVE
                and not self.weakens_thesis):
            # Guard against the specific confusion in section 33: our own
            # data problems are not bearish news about the issuer.
            object.__setattr__(self, "direction", ImpactDirection.UNKNOWN)

    # ---------------- derived ----------------

    @property
    def is_actionable_evidence(self) -> bool:
        """Whether this warrants offering evidence to the thesis at all."""
        return (self.consumption_status.permits_reassessment
                and self.relevance.warrants_reassessment
                and self.materiality is not ImpactLevel.UNKNOWN)

    @property
    def is_executable(self) -> bool:
        """Permanently False. Event intelligence never reaches a broker."""
        return False

    def evidence_direction(self) -> EvidenceDirection:
        """
        Map to 25.9I-E's three-valued direction, losing detail safely.

        MIXED and UNKNOWN become NEUTRAL: the thesis learns that
        something material happened and that nobody can say which way,
        which is exactly the state that should trigger a reassessment
        and exactly the state that should not trigger a revision.
        """
        return {
            ImpactDirection.POSITIVE: EvidenceDirection.POSITIVE,
            ImpactDirection.NEGATIVE: EvidenceDirection.NEGATIVE,
        }.get(self.direction, EvidenceDirection.NEUTRAL)

    def describe(self) -> Dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "canonical_event_id": self.canonical_event_id,
            "event_revision_key": self.event_revision_key,
            "thesis_id": self.thesis_id,
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "relation": self.relevance.relation.value,
            "thesis_relevance": self.relevance.thesis_relevance.value,
            "direction": self.direction.value,
            "evidence_direction_for_thesis": self.evidence_direction().value,
            "materiality": self.materiality.value,
            "materiality_score": self.materiality_score,
            "confidence": self.confidence.describe(),
            "corroboration_state": self.corroboration_state.value,
            "source_independence_known": self.source_independence_known,
            "conflict_state": self.conflict_state.value,
            "urgency": self.urgency.value,
            "consumption_status": self.consumption_status.value,
            "strengthens_thesis": self.strengthens_thesis,
            "weakens_thesis": self.weakens_thesis,
            "invalidates_thesis": self.invalidates_thesis,
            "target_reestimation_required": self.target_reestimation_required,
            "horizon_reestimation_required": self.horizon_reestimation_required,
            "partial_exit_review_required": self.partial_exit_review_required,
            "data_integrity_review_required": self.data_integrity_review_required,
            "integrity_concerns": [c.value for c in self.integrity_concerns],
            "requested_market_data_state": self.requested_market_data_state.value,
            "evidence_completeness": self.evidence_completeness,
            "is_stale": self.is_stale,
            "is_executable": self.is_executable,
            "reasons": list(self.reasons),
        }


# ======================================================================
# Requests and advisories
# ======================================================================


@dataclass(frozen=True)
class EventReassessmentRequest:
    """
    A request that a thesis be looked at again. Not a decision.

    Carries the market-data state it would LIKE. 25.9I-C owns whether
    that is granted, and this account has never served a realtime
    marker, so a HOT request routinely results in delayed data and a
    non-executable outcome. That is the system working.
    """
    request_id: str
    canonical_event_id: str
    thesis_id: str
    instrument_id: str
    as_of: datetime
    urgency: EventUrgency = EventUrgency.NORMAL
    requested_market_data_state: MarketDataState = MarketDataState.COLD
    assessment_id: Optional[str] = None
    reasons: Tuple[str, ...] = ()
    lineage_id: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))

    @property
    def is_executable(self) -> bool:
        return False


@dataclass(frozen=True)
class PreEventRiskAdvisory:
    """
    A scheduled event is approaching a position we hold. Advisory only.

    May recommend a review and fresher data. May NOT reduce, resize,
    sell or move a stop -- there is no field here that could express
    any of those, which is the point.
    """
    advisory_id: str
    thesis_id: str
    instrument_id: str
    as_of: datetime
    event_type: str = ""
    scheduled_for: Optional[datetime] = None
    canonical_event_id: Optional[str] = None
    horizon_days_remaining: Optional[int] = None
    exposure_notional: Optional[float] = None
    gap_risk_relevant: bool = False
    sector_id: Optional[str] = None
    recommend_thesis_review: bool = True
    recommend_risk_review: bool = False
    requested_market_data_state: MarketDataState = MarketDataState.COLD
    reasons: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "scheduled_for",
                           require_utc(self.scheduled_for, "scheduled_for"))
        if self.exposure_notional is not None and self.exposure_notional < 0:
            raise ValueError("exposure_notional must be >= 0")

    @property
    def is_executable(self) -> bool:
        return False

    @property
    def hours_until(self) -> Optional[float]:
        if self.scheduled_for is None:
            return None
        return round((self.scheduled_for - self.as_of).total_seconds() / 3600.0, 3)

    def describe(self) -> Dict[str, object]:
        return {
            "advisory_id": self.advisory_id,
            "thesis_id": self.thesis_id,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "scheduled_for": (self.scheduled_for.isoformat()
                              if self.scheduled_for else None),
            "hours_until": self.hours_until,
            "horizon_days_remaining": self.horizon_days_remaining,
            "exposure_notional": self.exposure_notional,
            "gap_risk_relevant": self.gap_risk_relevant,
            "recommend_thesis_review": self.recommend_thesis_review,
            "recommend_risk_review": self.recommend_risk_review,
            "requested_market_data_state":
                self.requested_market_data_state.value,
            "is_executable": self.is_executable,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class MultiPositionEventReport:
    """
    One event, several theses, each assessed on its own.

    Holds a LIST of independent assessments rather than a portfolio
    verdict. There is deliberately no aggregate recommendation field:
    "sell all tech" must be unspellable, not merely discouraged.
    """
    report_id: str
    canonical_event_id: str
    as_of: datetime
    assessments: Tuple[PositionEventImpactAssessment, ...] = ()
    affected_thesis_count: int = 0
    aggregate_notional: Optional[float] = None
    sector_ids: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if self.aggregate_notional is not None and self.aggregate_notional < 0:
            raise ValueError("aggregate_notional must be >= 0")

    @property
    def is_executable(self) -> bool:
        return False

    def describe(self) -> Dict[str, object]:
        return {
            "report_id": self.report_id,
            "canonical_event_id": self.canonical_event_id,
            "affected_thesis_count": self.affected_thesis_count,
            "aggregate_notional": self.aggregate_notional,
            "sector_ids": list(self.sector_ids),
            "assessments": [a.describe() for a in self.assessments],
            "is_executable": self.is_executable,
            "note": ("each thesis is assessed independently; there is no "
                     "portfolio-wide action"),
            "notes": list(self.notes),
        }
