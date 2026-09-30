"""
src/adaptive/event_assessment.py
----------------------------------------------
Direction, materiality, confidence and urgency for one event and one
thesis (Phase 25.9I-F, spec sections 8-11, 20-22, 33).

WHERE DIRECTION IS ALLOWED TO COME FROM
-------------------------------------------
Exactly two places, both evidenced:

1.  The canonical SUBTYPE that the existing extractor already derives
    from article text by deterministic rule -- `earnings_beat`,
    `earnings_miss`, `guidance_raised`, `guidance_cut`. Reading those is
    not inventing a rule; the rule is in `src/events/extractor.py` and
    has been in production since Phase 4.
2.  An explicit direction supplied by the caller (a fixture, or a
    future classifier that can justify itself).

Everything else is UNKNOWN. There is no "acquisition = bullish" table
here, because an acquisition is good for the target and usually bad for
the acquirer's balance sheet, and which of those we hold is precisely
the thing a headline does not say.

Measured 2026-09-30: 27 of 581 canonical events carry a directional
subtype. So direction is genuinely unknown for about 95% of this
corpus, and the engine says UNKNOWN 95% of the time rather than
guessing 581 times.

WHERE DIRECTION MAY *NOT* COME FROM
---------------------------------------
`ImpactProfile.dimensions.direction` and `ImpactDimensions` generally.
Audited: `src/impact/engine.py` derives that direction from ABNORMAL
RETURNS after the event. Using it to classify the event would be
reading the price reaction back into the cause -- the exact leakage
spec section 21 forbids, and it would also be future information
relative to the assessment moment. This module imports no impact
engine, and a test asserts it.

MATERIALITY, AND WHY IT IS A BAND
-------------------------------------
25.9I-E's `ThesisEvidence.materiality` is a 0..1 float and its
docstring says 25.9I-F owns it. It does -- but a float implies a
calibration nobody has. So materiality is decided as a BAND
(`ImpactLevel`: low/medium/high/unknown) from evidenced components, and
the float handed to 25.9I-E is a plain, documented band midpoint, not a
probability. `materiality_score` is None whenever the band is UNKNOWN,
so unmeasured evidence cannot clear a materiality threshold.

Components, each used only where the evidence actually exists:

    issuer directness     DIRECT outranks SECTOR outranks MARKET_WIDE
    canonical subtype     a stated beat/miss/raise/cut is concrete
    corroboration         multi-report raises it; syndication does not
    lifecycle             a confirmed filing outranks a rumour
    contradictions        lower it
    thesis relevance      an event touching a stated assumption matters
                          more to THIS position

CONFIDENCE STAYS IN PIECES
------------------------------
Four axes, never averaged into one number. `overall()` is the weakest
known axis, because the weakest axis is the one that will be wrong.

OUR OWN DATA PROBLEMS ARE NOT BAD NEWS
------------------------------------------
A stale quote, an unresolved entity or a timestamp conflict raises
`data_integrity_review_required` and can suppress a directional claim.
It never becomes NEGATIVE direction. A system that sells on its own
outages is not reading the market, it is reading itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.adaptive_models import MarketDataState
from src.domain.event_position_models import (
    DataIntegrityConcern, EvidenceConflictState, EventAssessmentConfidence,
    EventConsumptionStatus, EventEvidenceBundle, EventPositionRelation,
    EventUrgency, PositionEventImpactAssessment, PositionEventRelevance,
    ThesisRelevance,
)
from src.domain.fusion_models import CorroborationState
from src.domain.impact_models import ImpactDirection, ImpactLevel
from src.domain.thesis_models import PositionThesis

#: Canonical subtypes that STATE a direction, produced by the existing
#: extractor's deterministic rules. Nothing is added to this map without
#: a corresponding rule in `src/events/extractor.py`.
SUBTYPE_DIRECTION: Dict[str, ImpactDirection] = {
    "earnings_beat": ImpactDirection.POSITIVE,
    "earnings_miss": ImpactDirection.NEGATIVE,
    "guidance_raised": ImpactDirection.POSITIVE,
    "guidance_cut": ImpactDirection.NEGATIVE,
}

#: Lifecycle states that mean the event is established rather than
#: rumoured. Used to raise materiality, never to set direction.
ESTABLISHED_LIFECYCLE = frozenset({"confirmed", "corroborated", "completed"})

#: Lifecycle states that mean the event went away.
WITHDRAWN_LIFECYCLE = frozenset({"denied", "cancelled", "retracted"})

#: Band midpoints handed to 25.9I-E. Plain, documented numbers with no
#: calibration claimed behind them; see the module docstring.
MATERIALITY_SCORE: Dict[ImpactLevel, Optional[float]] = {
    ImpactLevel.HIGH: 0.80,
    ImpactLevel.MEDIUM: 0.50,
    ImpactLevel.LOW: 0.20,
    ImpactLevel.UNKNOWN: None,
}


@dataclass(frozen=True)
class AssessmentPolicy:
    """
    Every threshold, stated and configurable. No calibration claimed.

    `stale_after_seconds` defaults to None -- staleness is a judgement
    about how fast a thesis's world moves, and no evidence in this
    repository sets it. A caller that wants staleness says how long.
    """
    stale_after_seconds: Optional[float] = None
    #: Materiality a NEGATIVE direct event needs before it may be
    #: treated as invalidating. HIGH by contract; restated here so a
    #: caller can see it without reading the dataclass validator.
    invalidation_materiality: ImpactLevel = ImpactLevel.HIGH
    #: Whether an event with no established direction may still raise
    #: urgency. It may: "something material happened and we cannot tell
    #: which way" is a reason to look, not a reason to relax.
    unknown_direction_may_elevate: bool = True
    #: Minimum reports before multi-source raises materiality.
    multi_report_threshold: int = 2
    #: Whether a caller-supplied direction may override a canonical
    #: subtype. False: the subtype is evidence and the caller's opinion
    #: is not, so the subtype wins and the conflict is recorded.
    caller_direction_overrides_subtype: bool = False


def assessment_id_for(canonical_event_id: str, revision_key: str,
                      thesis_id: str, as_of: datetime) -> str:
    """
    Content-derived, so the same inputs produce the same id.

    Includes the revision key, so a materially revised event produces a
    DIFFERENT assessment id and can therefore be linked to its
    predecessor rather than overwriting it.
    """
    raw = "|".join((str(canonical_event_id), str(revision_key), str(thesis_id),
                    as_of.isoformat(timespec="seconds")))
    return "pea-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


class EventImpactAssessor:
    """
    Turns an event bundle plus a thesis into an assessment. Deterministic.

    No clock, no database, no transport. `as_of` is an argument, so the
    same inputs always give the same assessment and the same id.
    """

    def __init__(self, policy: Optional[AssessmentPolicy] = None):
        self.policy = policy or AssessmentPolicy()

    # ---------------- direction ----------------

    def direction_for(self, bundle: EventEvidenceBundle, *,
                      subtype: Optional[str] = None,
                      supplied: Optional[ImpactDirection] = None
                      ) -> Tuple[ImpactDirection, List[str]]:
        """
        The event's direction, or UNKNOWN. Never derived from price.

        A withdrawn event (denied, cancelled, retracted) is NEUTRAL
        regardless of what it originally said: a retracted rumour of a
        takeover is not bullish, and it is not bearish either.
        """
        reasons: List[str] = []
        if bundle.lifecycle_state.lower() in WITHDRAWN_LIFECYCLE:
            reasons.append(
                f"the event is {bundle.lifecycle_state}; a withdrawn event "
                "carries no direction")
            return ImpactDirection.NEUTRAL, reasons

        from_subtype = SUBTYPE_DIRECTION.get(str(subtype or "").lower())
        if from_subtype is not None:
            reasons.append(
                f"canonical subtype {subtype!r} states a "
                f"{from_subtype.value} direction")
            if (supplied is not None and supplied is not from_subtype
                    and supplied is not ImpactDirection.UNKNOWN):
                if self.policy.caller_direction_overrides_subtype:
                    reasons.append(
                        f"caller direction {supplied.value} overrides the subtype")
                    return supplied, reasons
                reasons.append(
                    f"a caller supplied {supplied.value}, which disagrees with "
                    "the canonical subtype; the subtype is evidence and wins")
                return ImpactDirection.MIXED, reasons
            return from_subtype, reasons

        if supplied is not None and supplied is not ImpactDirection.UNKNOWN:
            reasons.append(f"direction {supplied.value} supplied by the caller")
            return supplied, reasons

        reasons.append(
            f"no canonical subtype states a direction for this "
            f"{bundle.event_type} event, and none was supplied")
        return ImpactDirection.UNKNOWN, reasons

    # ---------------- materiality ----------------

    def materiality_for(self, bundle: EventEvidenceBundle, *,
                        relation: EventPositionRelation,
                        thesis_relevance: ThesisRelevance,
                        direction: ImpactDirection,
                        subtype: Optional[str] = None,
                        supplied: Optional[ImpactLevel] = None
                        ) -> Tuple[ImpactLevel, List[str]]:
        """
        A band, from evidenced components. UNKNOWN when nothing supports one.

        Deliberately conservative at both ends: a sector event cannot
        reach HIGH on its own, and an event with no direction and no
        assumption match cannot rise above LOW.
        """
        reasons: List[str] = []
        if supplied is not None:
            reasons.append(f"materiality {supplied.value} supplied by the caller")
            return supplied, reasons

        if relation in (EventPositionRelation.UNKNOWN,):
            reasons.append(
                "the event has no evidenced relation to this instrument, so "
                "its materiality to this position cannot be assessed")
            return ImpactLevel.UNKNOWN, reasons

        if bundle.lifecycle_state.lower() in WITHDRAWN_LIFECYCLE:
            reasons.append(f"the event is {bundle.lifecycle_state}")
            return ImpactLevel.LOW, reasons

        score = 0
        if relation is EventPositionRelation.DIRECT:
            score += 2
            reasons.append("the event is about this issuer")
        elif relation in (EventPositionRelation.INDIRECT,
                          EventPositionRelation.SECTOR):
            score += 1
            reasons.append(f"the event reaches this position via {relation.value}")
        else:
            reasons.append("market-wide events affect this position weakly")

        if thesis_relevance is ThesisRelevance.HIGH:
            score += 2
            reasons.append("it bears on a stated thesis assumption")
        elif thesis_relevance is ThesisRelevance.MEDIUM:
            score += 1
        elif thesis_relevance is ThesisRelevance.NOT_RELEVANT:
            score -= 1
            reasons.append("it bears on none of the stated assumptions")

        if str(subtype or "").lower() in SUBTYPE_DIRECTION:
            score += 1
            reasons.append(f"the canonical subtype {subtype!r} is concrete")

        if bundle.lifecycle_state.lower() in ESTABLISHED_LIFECYCLE:
            score += 1
            reasons.append(f"the event is {bundle.lifecycle_state}")
        elif bundle.lifecycle_state.lower() == "rumored":
            # A rumour about something that would matter is still a
            # rumour. It can warrant a look; it should not carry the
            # same weight as a filing, or the first unsourced report
            # moves the position and the confirmation adds nothing.
            score -= 1
            reasons.append("the event is only rumoured")

        if bundle.is_independently_corroborated:
            score += 1
            reasons.append("independently corroborated")
        elif bundle.total_report_count >= self.policy.multi_report_threshold:
            reasons.append(
                f"{bundle.total_report_count} reports, but source independence "
                "is not established, so this does not raise materiality")

        if bundle.has_contradictions:
            score -= 1
            reasons.append("the reporting contains contradictions")

        if direction is ImpactDirection.UNKNOWN and score < 3:
            reasons.append(
                "no direction is established and the supporting evidence is "
                "thin")
            return ImpactLevel.LOW, reasons

        if score >= 4 and relation is EventPositionRelation.DIRECT:
            band = ImpactLevel.HIGH
        elif score >= 2:
            band = ImpactLevel.MEDIUM
        else:
            band = ImpactLevel.LOW
        return band, reasons

    # ---------------- corroboration and conflict ----------------

    def conflict_for(self, bundle: EventEvidenceBundle,
                     direction: ImpactDirection, *,
                     opposing_directions: Sequence[ImpactDirection] = ()
                     ) -> Tuple[EvidenceConflictState, List[str]]:
        """
        Whether the evidence agrees with itself.

        Never resolved by counting: a 3-to-2 split is CONFLICTED, not
        "positive". The caller may pass directions derived from other
        evidence about the same event.
        """
        reasons: List[str] = []
        directions = {d for d in opposing_directions
                      if d not in (ImpactDirection.UNKNOWN,
                                   ImpactDirection.NEUTRAL)}
        if direction not in (ImpactDirection.UNKNOWN, ImpactDirection.NEUTRAL):
            directions.add(direction)

        if bundle.has_contradictions:
            reasons.append("the canonical event records contradictions")
            return EvidenceConflictState.CONFLICTED, reasons
        if direction is ImpactDirection.MIXED:
            reasons.append("the evidence points both ways")
            return EvidenceConflictState.CONFLICTED, reasons
        if (ImpactDirection.POSITIVE in directions
                and ImpactDirection.NEGATIVE in directions):
            reasons.append(
                "credible evidence points in both directions; this is not "
                "resolved by counting headlines")
            return EvidenceConflictState.CONFLICTED, reasons
        if not directions:
            reasons.append("no directional evidence")
            return EvidenceConflictState.INSUFFICIENT, reasons
        if directions == {ImpactDirection.POSITIVE}:
            return EvidenceConflictState.CONSISTENT_POSITIVE, reasons
        if directions == {ImpactDirection.NEGATIVE}:
            return EvidenceConflictState.CONSISTENT_NEGATIVE, reasons
        return EvidenceConflictState.UNKNOWN, reasons

    # ---------------- urgency ----------------

    def urgency_for(self, *, materiality: ImpactLevel,
                    direction: ImpactDirection,
                    relation: EventPositionRelation,
                    conflict: EvidenceConflictState,
                    invalidates: bool, is_stale: bool
                    ) -> Tuple[EventUrgency, List[str]]:
        """
        How fast someone must LOOK. Never how fast they must sell.

        A stale event is capped at NORMAL: reprocessing yesterday's news
        today does not make it urgent, which is the specific failure
        spec section 14 names.
        """
        reasons: List[str] = []
        if is_stale:
            reasons.append(
                "the event is stale; reprocessing does not make it urgent")
            return EventUrgency.NORMAL, reasons
        if invalidates:
            reasons.append("the thesis may be invalidated: reassess now")
            return EventUrgency.CRITICAL, reasons
        if (materiality is ImpactLevel.HIGH
                and relation is EventPositionRelation.DIRECT
                and direction is ImpactDirection.NEGATIVE):
            reasons.append("material negative issuer event")
            return EventUrgency.CRITICAL, reasons
        if conflict is EvidenceConflictState.CONFLICTED and materiality in (
                ImpactLevel.HIGH, ImpactLevel.MEDIUM):
            reasons.append(
                "material but conflicted: uncertainty is raised and a fresh "
                "look is warranted")
            return EventUrgency.ELEVATED, reasons
        if materiality is ImpactLevel.HIGH:
            reasons.append("material issuer event")
            return EventUrgency.ELEVATED, reasons
        if (materiality is ImpactLevel.MEDIUM
                and direction is ImpactDirection.UNKNOWN
                and self.policy.unknown_direction_may_elevate
                and relation is EventPositionRelation.DIRECT):
            reasons.append(
                "a medium-materiality issuer event with no established "
                "direction still warrants a look")
            return EventUrgency.ELEVATED, reasons
        return EventUrgency.NORMAL, reasons

    # ---------------- the whole thing ----------------

    def assess(self, bundle: EventEvidenceBundle, thesis: PositionThesis,
               relevance: PositionEventRelevance, *, as_of: datetime,
               subtype: Optional[str] = None,
               supplied_direction: Optional[ImpactDirection] = None,
               supplied_materiality: Optional[ImpactLevel] = None,
               opposing_directions: Sequence[ImpactDirection] = (),
               consumption: EventConsumptionStatus = EventConsumptionStatus.NEW,
               previous_assessment_id: Optional[str] = None,
               invalidates_assumption: bool = False,
               extra_integrity_concerns: Sequence[DataIntegrityConcern] = (),
               lineage_id: Optional[str] = None
               ) -> PositionEventImpactAssessment:
        """
        One assessment. Every conclusion carries the reason that produced it.

        `invalidates_assumption` is the caller's statement that a CORE
        assumption is contradicted -- the heaviest input this method
        takes. It is still not sufficient: the contracts require HIGH
        materiality, NEGATIVE direction and a DIRECT relation before
        invalidation is recorded, so a caller cannot end a thesis by
        setting a boolean.
        """
        reasons: List[str] = []

        direction, direction_reasons = self.direction_for(
            bundle, subtype=subtype, supplied=supplied_direction)
        reasons.extend(direction_reasons)

        materiality, materiality_reasons = self.materiality_for(
            bundle, relation=relevance.relation,
            thesis_relevance=relevance.thesis_relevance, direction=direction,
            subtype=subtype, supplied=supplied_materiality)
        reasons.extend(materiality_reasons)

        conflict, conflict_reasons = self.conflict_for(
            bundle, direction, opposing_directions=opposing_directions)
        reasons.extend(conflict_reasons)

        is_stale = self._is_stale(bundle, as_of)
        if is_stale:
            reasons.append("the event is older than the configured freshness "
                           "window")

        concerns = tuple(dict.fromkeys(
            tuple(bundle.integrity_concerns) + tuple(extra_integrity_concerns)))
        integrity_review = bool(
            set(concerns) - {DataIntegrityConcern.MISSING_EVENT_TIME})
        if DataIntegrityConcern.UNRESOLVED_ENTITY in concerns:
            reasons.append(
                "an entity could not be resolved; this is a data problem, not "
                "bearish news")

        invalidates = bool(
            invalidates_assumption
            and direction is ImpactDirection.NEGATIVE
            and materiality is self.policy.invalidation_materiality
            and relevance.relation.is_issuer
            and conflict is not EvidenceConflictState.CONFLICTED)
        if invalidates_assumption and not invalidates:
            reasons.append(
                "a core assumption was reported as contradicted, but the "
                "evidence does not meet the bar for invalidation "
                f"(direction={direction.value}, materiality={materiality.value}, "
                f"relation={relevance.relation.value}, conflict={conflict.value})")

        strengthens = bool(
            direction is ImpactDirection.POSITIVE
            and materiality in (ImpactLevel.HIGH, ImpactLevel.MEDIUM)
            and relevance.is_relevant_to_thesis
            and conflict is not EvidenceConflictState.CONFLICTED)
        weakens = bool(
            direction is ImpactDirection.NEGATIVE
            and materiality in (ImpactLevel.HIGH, ImpactLevel.MEDIUM)
            and relevance.warrants_reassessment
            and not invalidates)

        urgency, urgency_reasons = self.urgency_for(
            materiality=materiality, direction=direction,
            relation=relevance.relation, conflict=conflict,
            invalidates=invalidates, is_stale=is_stale)
        reasons.extend(urgency_reasons)

        # A positive material event may ask for the target and horizon to
        # be RE-ESTIMATED. It may not estimate them -- there is no field
        # here that could carry a number.
        target_reestimation = bool(
            strengthens and materiality in (ImpactLevel.HIGH, ImpactLevel.MEDIUM))
        horizon_reestimation = target_reestimation
        if target_reestimation:
            reasons.append(
                "material positive evidence: the target and horizon should be "
                "re-estimated by a qualified estimator, which this layer is not")
        partial_exit_review = bool(weakens and materiality is ImpactLevel.HIGH)
        if partial_exit_review:
            reasons.append(
                "material negative evidence: a partial exit should be reviewed; "
                "no fraction is proposed here")

        confidence = EventAssessmentConfidence(
            entity_match=relevance.entity_resolution_confidence,
            event_extraction=bundle.quality_confidence,
            fact=bundle.quality_confidence,
            materiality=None,
            thesis_impact=None)

        return PositionEventImpactAssessment(
            assessment_id=assessment_id_for(
                bundle.canonical_event_id, bundle.revision_key,
                thesis.thesis_id, as_of),
            canonical_event_id=bundle.canonical_event_id,
            thesis_id=thesis.thesis_id,
            instrument_id=thesis.instrument_id,
            as_of=as_of, relevance=relevance, direction=direction,
            materiality=materiality,
            materiality_score=MATERIALITY_SCORE[materiality],
            confidence=confidence,
            corroboration_state=bundle.corroboration_state,
            source_independence_known=bundle.source_independence_known,
            conflict_state=conflict, urgency=urgency,
            consumption_status=consumption,
            strengthens_thesis=strengthens, weakens_thesis=weakens,
            invalidates_thesis=invalidates,
            target_reestimation_required=target_reestimation,
            horizon_reestimation_required=horizon_reestimation,
            partial_exit_review_required=partial_exit_review,
            data_integrity_review_required=integrity_review,
            integrity_concerns=concerns,
            requested_market_data_state=urgency.requested_market_data_state,
            event_revision_key=bundle.revision_key,
            evidence_completeness=confidence.completeness,
            is_stale=is_stale, reasons=tuple(reasons),
            lineage_id=lineage_id,
            session_snapshot_id=thesis.session_snapshot_id,
            allocation_plan_id=thesis.allocation_plan_id,
            previous_assessment_id=previous_assessment_id)

    def _is_stale(self, bundle: EventEvidenceBundle, as_of: datetime) -> bool:
        if self.policy.stale_after_seconds is None:
            return False
        age = bundle.age_seconds(as_of)
        if age is None:
            return False
        return age > self.policy.stale_after_seconds
