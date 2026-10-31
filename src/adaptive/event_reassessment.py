"""
src/adaptive/event_reassessment.py
------------------------------------------------
Event evidence reaching a position thesis (Phase 25.9I-F).

THE WHOLE FLOW, AND WHERE IT STOPS
--------------------------------------
    canonical event -> relevance -> assessment -> ThesisEvidence
                    -> 25.9I-E reassessment -> recommendation
                    -> a market-data REQUEST -> recorded

It stops there. Every recommendation is `is_executable = False`, no
module here imports execution, trading or portfolio, and no broker
method is called. Portfolio and Risk remain the gatekeepers and
Execution remains the only route to IBKR.

THE SEAM THAT MATTERS MOST
------------------------------
`PositionThesisEngine.reassess` takes `proposed_target`,
`proposed_horizon` and `exit_fraction` as parameters SEPARATE from
`evidence`. This engine fills in `evidence` and nothing else. The three
proposals arrive, when they arrive at all, from an explicit external
estimator that the caller passes through untouched.

So the separation is structural rather than disciplinary: a positive
event produces `target_reestimation_required=True` and no number,
because this engine has no number to give and no field to put one in.

ARTICLE != EVENT, AND IT IS ALREADY HANDLED
-----------------------------------------------
Fusion collapsed 846 reports into 581 canonical events before this
layer sees anything. Five articles about one acquisition arrive as ONE
`canonical_event_id`, so one reaction is the default rather than
something this engine has to suppress. What it DOES suppress is the
same canonical event arriving twice on different days.

NOVELTY IS PER (EVENT, REVISION, THESIS)
--------------------------------------------
More syndicated copies of the same story is `UPDATED` -- not material,
no second reaction. A rumour becoming a confirmed filing changes the
lifecycle state, which is `MATERIAL_REVISION` -- a second assessment,
explicitly linked to the first.

COOLDOWN RESTRAINS GREED, NEVER SAFETY
------------------------------------------
25.9I-E checks invalidation BEFORE any policy gate, so an anti-
overreaction cooldown physically cannot suppress an invalidating event.
This engine relies on that ordering rather than re-implementing it, and
a test drives the whole path end to end.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.event_assessment import EventImpactAssessor
from src.adaptive.event_relevance import EventRelevanceClassifier
from src.domain.adaptive_models import (
    MarketDataState, RealtimeActivationReason,
)
from src.domain.data_attention_models import (
    AttentionPriority, DataAttentionRequest,
)
from src.domain.event_position_models import (
    DataIntegrityConcern, EventConsumptionStatus, EventEvidenceBundle,
    EventReassessmentRequest, EventUrgency, MultiPositionEventReport,
    PositionEventImpactAssessment, PreEventRiskAdvisory,
)
from src.domain.impact_models import ImpactDirection, ImpactLevel
from src.domain.thesis_models import (
    ExpectedHorizon, InvalidationZone, PositionThesis, TargetZone,
    ThesisEvidence, ThesisStatus,
)

#: Thesis statuses that can still be reassessed. A CLOSED thesis is
#: terminal: re-entering the same instrument is a NEW thesis with a new
#: id (25.9I-E), so offering evidence to a closed one is a bug.
REASSESSABLE_STATUSES = frozenset({
    ThesisStatus.ACTIVE, ThesisStatus.PARTIALLY_EXITED,
    ThesisStatus.EXIT_RECOMMENDED, ThesisStatus.DRAFT,
})


def evidence_id_for(canonical_event_id: str, revision_key: str,
                    thesis_id: str) -> str:
    """Content-derived, so the same event yields the same evidence id."""
    raw = "|".join((str(canonical_event_id), str(revision_key), str(thesis_id)))
    return "eve-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class ConsumptionRecord:
    """
    What a thesis has already seen of one event.

    Stores the MATERIAL fields rather than only the revision hash, so
    the engine can tell a material revision from another syndicated
    copy. A hash alone would say "something changed" without saying
    whether it mattered.
    """
    canonical_event_id: str
    thesis_id: str
    revision_key: str
    assessment_id: str
    assessed_at: datetime
    lifecycle_state: str = ""
    corroboration_state: str = ""
    total_report_count: int = 0
    has_contradictions: bool = False


@dataclass(frozen=True)
class EventReassessmentOutcome:
    """
    Everything one (event, thesis) pair produced. Non-executable throughout.

    `thesis_assessment` is 25.9I-E's own output, unmodified. This engine
    does not post-process a recommendation: if it did, the rules
    25.9I-E enforces could be undone one layer up.
    """
    assessment: PositionEventImpactAssessment
    evidence: Optional[ThesisEvidence] = None
    thesis_assessment: Optional[object] = None
    reassessment_request: Optional[EventReassessmentRequest] = None
    data_request: Optional[DataAttentionRequest] = None
    consumption_status: EventConsumptionStatus = EventConsumptionStatus.NEW
    skipped_reason: str = ""
    #: The thesis as it stood BEFORE 25.9I-E saw the evidence. Captured
    #: so `target_changed` and `horizon_changed` are observed facts
    #: rather than claims -- the report has to be able to prove the
    #: target did not move, not merely assert it.
    original_target: Optional[TargetZone] = None
    original_horizon: Optional[ExpectedHorizon] = None

    @property
    def is_executable(self) -> bool:
        """Permanently False, at every level of this phase."""
        return False

    @property
    def recommendation(self):
        """25.9I-E's `PositionActionRecommendation`, or None."""
        return getattr(self.thesis_assessment, "recommendation", None)

    @property
    def action(self):
        """The `PositionAction` inside that recommendation, or None."""
        return getattr(self.recommendation, "action", None)

    @property
    def revised_thesis(self):
        return getattr(self.thesis_assessment, "revised_thesis", None)

    def describe(self) -> Dict[str, object]:
        action = self.action
        recommendation = self.recommendation
        return {
            "recommendation_id": getattr(recommendation, "recommendation_id",
                                         None),
            "assessment": self.assessment.describe(),
            "consumption_status": self.consumption_status.value,
            "skipped_reason": self.skipped_reason,
            "evidence_id": self.evidence.evidence_id if self.evidence else None,
            "evidence_direction": (self.evidence.direction.value
                                   if self.evidence else None),
            "evidence_materiality": (self.evidence.materiality
                                     if self.evidence else None),
            "evidence_references": (list(self.evidence.references)
                                    if self.evidence else []),
            "recommendation": (action.value if action else None),
            "target_changed": self._target_changed(),
            "horizon_changed": self._horizon_changed(),
            "partial_exit_fraction": self._fraction(),
            "market_data_request": (self.data_request.desired_state.value
                                    if self.data_request else None),
            "urgency": self.assessment.urgency.value,
            "is_executable": self.is_executable,
        }

    # ---- the three prohibitions, reported as observable facts ----

    def _target_changed(self) -> bool:
        revised = self.revised_thesis
        if revised is None or self.original_target is None:
            return False
        return revised.target_zone != self.original_target

    def _horizon_changed(self) -> bool:
        revised = self.revised_thesis
        if revised is None or self.original_horizon is None:
            return False
        return revised.horizon != self.original_horizon

    def _fraction(self) -> Optional[float]:
        """
        The exit fraction 25.9I-E settled on, if any.

        Read from the recommendation rather than computed. Whenever this
        is not None it came from the CALLER via `exit_fraction`, because
        this engine has no field that could hold one -- which is the
        fact the report needs to be able to show rather than assert.
        """
        return getattr(self.recommendation, "exit_fraction", None)


@dataclass(frozen=True)
class ReassessmentPolicy:
    """Configurable, inert by default. No calibrated production values."""
    #: Whether an event with UNKNOWN thesis relevance may still offer
    #: evidence. It may, for a DIRECT issuer event -- see
    #: `PositionEventRelevance.warrants_reassessment`.
    allow_unknown_relevance: bool = True
    #: Urgency at or above which a market-data request is emitted at all.
    #: NORMAL events do not ask for anything: COLD is the absence of a
    #: request, not a request for cheapness.
    request_data_from: EventUrgency = EventUrgency.ELEVATED
    #: Hours before a scheduled event at which an advisory is produced.
    #: None means "whenever asked" -- no calibrated window exists.
    advisory_window_hours: Optional[float] = None


class EventDrivenReassessmentEngine:
    """
    Event intelligence reaching a thesis, and stopping there.

    Deterministic: no clock (every method takes `as_of`), no database,
    no transport. The same inputs produce the same assessment ids, the
    same evidence ids and the same recommendation.
    """

    def __init__(self, thesis_engine, classifier: EventRelevanceClassifier,
                 assessor: Optional[EventImpactAssessor] = None,
                 policy: Optional[ReassessmentPolicy] = None):
        self.thesis_engine = thesis_engine
        self.classifier = classifier
        self.assessor = assessor or EventImpactAssessor()
        self.policy = policy or ReassessmentPolicy()

    # ---------------- novelty ----------------

    def consumption_status(self, bundle: EventEvidenceBundle,
                           thesis: PositionThesis,
                           history: Sequence[ConsumptionRecord] = ()
                           ) -> Tuple[EventConsumptionStatus,
                                      Optional[ConsumptionRecord]]:
        """
        Has this thesis already reacted to this event, and did it change?

        A changed report COUNT alone is `UPDATED`: more outlets carrying
        the same wire story is not new information, and reacting to it
        would be reacting to syndication. A changed lifecycle,
        corroboration state or contradiction flag is
        `MATERIAL_REVISION`.
        """
        prior = [r for r in history
                 if r.canonical_event_id == bundle.canonical_event_id
                 and r.thesis_id == thesis.thesis_id]
        if not prior:
            return EventConsumptionStatus.NEW, None
        latest = max(prior, key=lambda r: (r.assessed_at, r.assessment_id))

        if latest.revision_key == bundle.revision_key:
            return EventConsumptionStatus.ALREADY_ASSESSED, latest

        material = (
            latest.lifecycle_state != bundle.lifecycle_state
            or latest.corroboration_state != bundle.corroboration_state.value
            or bool(latest.has_contradictions) != bool(bundle.has_contradictions))
        if material:
            return EventConsumptionStatus.MATERIAL_REVISION, latest
        return EventConsumptionStatus.UPDATED, latest

    # ---------------- adapter ----------------

    def evidence_from(self, assessment: PositionEventImpactAssessment,
                      bundle: EventEvidenceBundle) -> ThesisEvidence:
        """
        The canonical Event Intelligence -> ThesisEvidence adapter.

        No anonymous evidence: `references` carries the canonical event
        id, the revision key, the assessment id and every report id, so
        any later reader can walk back from a thesis revision to the
        articles that caused it.

        `materiality` is the band midpoint documented in
        `event_assessment.MATERIALITY_SCORE`, and is None whenever the
        band is UNKNOWN -- 25.9I-E treats unmeasured materiality as
        unable to clear a threshold, which is the behaviour we want.
        """
        references = (
            f"event:{bundle.canonical_event_id}",
            f"revision:{bundle.revision_key}",
            f"assessment:{assessment.assessment_id}",
        ) + tuple(f"report:{report_id}" for report_id in bundle.report_ids)

        summary = (f"{bundle.event_type} ({assessment.direction.value}, "
                   f"materiality {assessment.materiality.value}, "
                   f"{assessment.relevance.relation.value}): "
                   f"{bundle.title}")[:500]

        return ThesisEvidence(
            evidence_id=evidence_id_for(bundle.canonical_event_id,
                                        bundle.revision_key,
                                        assessment.thesis_id),
            direction=assessment.evidence_direction(),
            as_of=assessment.as_of,
            summary=summary,
            materiality=assessment.materiality_score,
            confidence=assessment.confidence.overall(),
            source=f"event_intelligence:{bundle.canonical_event_id}",
            invalidates_thesis=assessment.invalidates_thesis,
            references=references)

    # ---------------- requests ----------------

    def _data_request(self, assessment: PositionEventImpactAssessment,
                      thesis: PositionThesis) -> Optional[DataAttentionRequest]:
        """
        A REQUEST for fresher data. 25.9I-C decides whether it is granted.

        This account has never served a realtime marker, so a HOT
        request routinely yields delayed data and a non-executable
        outcome. That is the separation working, not a failure.
        """
        if assessment.urgency.rank < self.policy.request_data_from.rank:
            return None
        state = assessment.requested_market_data_state
        activation = None
        priority = AttentionPriority.POSITION_MONITOR
        if state is MarketDataState.HOT:
            activation = (
                RealtimeActivationReason.MATERIAL_NEGATIVE_EVENT
                if assessment.direction is ImpactDirection.NEGATIVE
                else RealtimeActivationReason.MATERIAL_POSITIVE_EVENT)
            if assessment.invalidates_thesis:
                activation = RealtimeActivationReason.INVALIDATION_PROXIMITY
                priority = AttentionPriority.POSITION_EXIT
        return DataAttentionRequest(
            instrument_id=thesis.instrument_id, desired_state=state,
            requester="event_intelligence",
            reason=(f"{assessment.urgency.value} {assessment.direction.value} "
                    f"{assessment.materiality.value}-materiality event "
                    f"{assessment.canonical_event_id}"),
            priority=priority, activation_reason=activation,
            as_of=assessment.as_of, lineage_id=assessment.lineage_id,
            thesis_id=thesis.thesis_id,
            session_snapshot_id=thesis.session_snapshot_id)

    def _reassessment_request(self, assessment: PositionEventImpactAssessment,
                              thesis: PositionThesis) -> EventReassessmentRequest:
        return EventReassessmentRequest(
            request_id="err-" + assessment.assessment_id.split("-", 1)[-1],
            canonical_event_id=assessment.canonical_event_id,
            thesis_id=thesis.thesis_id, instrument_id=thesis.instrument_id,
            as_of=assessment.as_of, urgency=assessment.urgency,
            requested_market_data_state=assessment.requested_market_data_state,
            assessment_id=assessment.assessment_id,
            reasons=assessment.reasons[:6], lineage_id=assessment.lineage_id)

    # ---------------- the main path ----------------

    def reassess(self, bundle: EventEvidenceBundle, thesis: PositionThesis, *,
                 as_of: datetime, price: Optional[float] = None,
                 history: Sequence[ConsumptionRecord] = (),
                 subtype: Optional[str] = None,
                 supplied_direction: Optional[ImpactDirection] = None,
                 supplied_materiality: Optional[ImpactLevel] = None,
                 opposing_directions: Sequence[ImpactDirection] = (),
                 invalidates_assumption: bool = False,
                 entity_id: Optional[str] = None,
                 sector_id: Optional[str] = None,
                 extra_integrity_concerns: Sequence[DataIntegrityConcern] = (),
                 proposed_target: Optional[TargetZone] = None,
                 proposed_invalidation: Optional[InvalidationZone] = None,
                 proposed_horizon: Optional[ExpectedHorizon] = None,
                 exit_fraction: Optional[float] = None,
                 since_last_revision: Optional[timedelta] = None,
                 revisions_today: int = 0,
                 lineage_id: Optional[str] = None
                 ) -> EventReassessmentOutcome:
        """
        One event, one thesis, one outcome.

        `proposed_target`, `proposed_horizon` and `exit_fraction` are
        passed THROUGH to 25.9I-E untouched. This engine never sets
        them, never defaults them and never reads them to make a
        decision. They are the external estimator's, and carrying them
        without touching them is the whole proof of separation.
        """
        # --- a closed thesis is terminal ---
        if thesis.status not in REASSESSABLE_STATUSES:
            assessment = self._inert_assessment(
                bundle, thesis, as_of, lineage_id,
                f"the thesis is {thesis.status.value} and cannot be reassessed")
            return EventReassessmentOutcome(
                assessment=assessment,
                consumption_status=EventConsumptionStatus.IGNORED,
                skipped_reason=f"thesis status {thesis.status.value}")

        consumption, prior = self.consumption_status(bundle, thesis, history)

        # --- direction first: relevance needs it to grade assumptions ---
        direction, _ = self.assessor.direction_for(
            bundle, subtype=subtype, supplied=supplied_direction)
        relevance = self.classifier.classify(
            bundle, thesis, direction=direction, entity_id=entity_id,
            sector_id=sector_id)

        assessment = self.assessor.assess(
            bundle, thesis, relevance, as_of=as_of, subtype=subtype,
            supplied_direction=supplied_direction,
            supplied_materiality=supplied_materiality,
            opposing_directions=opposing_directions,
            consumption=consumption,
            previous_assessment_id=prior.assessment_id if prior else None,
            invalidates_assumption=invalidates_assumption,
            extra_integrity_concerns=extra_integrity_concerns,
            lineage_id=lineage_id)

        # --- already seen, or nothing worth offering ---
        if not consumption.permits_reassessment:
            return EventReassessmentOutcome(
                assessment=assessment, consumption_status=consumption,
                skipped_reason=(
                    f"this thesis already assessed {bundle.canonical_event_id} "
                    f"({consumption.value}); no second reaction"))
        if not assessment.is_actionable_evidence:
            return EventReassessmentOutcome(
                assessment=assessment, consumption_status=consumption,
                skipped_reason=(
                    "the event does not warrant offering evidence to this "
                    f"thesis (relevance={relevance.thesis_relevance.value}, "
                    f"materiality={assessment.materiality.value})"))

        # --- evidence, then 25.9I-E's own judgement ---
        evidence = self.evidence_from(assessment, bundle)
        thesis_assessment = self.thesis_engine.reassess(
            thesis, as_of, price=price, evidence=(evidence,),
            proposed_target=proposed_target,
            proposed_invalidation=proposed_invalidation,
            proposed_horizon=proposed_horizon, exit_fraction=exit_fraction,
            since_last_revision=since_last_revision,
            revisions_today=revisions_today)

        return EventReassessmentOutcome(
            assessment=assessment, evidence=evidence,
            thesis_assessment=thesis_assessment,
            reassessment_request=self._reassessment_request(assessment, thesis),
            data_request=self._data_request(assessment, thesis),
            consumption_status=consumption,
            original_target=thesis.target_zone,
            original_horizon=thesis.horizon)

    def _inert_assessment(self, bundle, thesis, as_of, lineage_id, reason):
        from src.adaptive.event_assessment import assessment_id_for
        from src.domain.event_position_models import (
            PositionEventRelevance, ThesisRelevance,
        )
        return PositionEventImpactAssessment(
            assessment_id=assessment_id_for(
                bundle.canonical_event_id, bundle.revision_key,
                thesis.thesis_id, as_of),
            canonical_event_id=bundle.canonical_event_id,
            thesis_id=thesis.thesis_id, instrument_id=thesis.instrument_id,
            as_of=as_of,
            relevance=PositionEventRelevance(
                canonical_event_id=bundle.canonical_event_id,
                thesis_id=thesis.thesis_id, instrument_id=thesis.instrument_id,
                thesis_relevance=ThesisRelevance.UNKNOWN),
            event_revision_key=bundle.revision_key,
            reasons=(reason,), lineage_id=lineage_id)

    # ---------------- multi-position ----------------

    def reassess_many(self, bundle: EventEvidenceBundle,
                      theses: Sequence[PositionThesis], *, as_of: datetime,
                      history: Sequence[ConsumptionRecord] = (),
                      sector_ids: Optional[Mapping[str, str]] = None,
                      exposures: Optional[Mapping[str, float]] = None,
                      **kwargs) -> Tuple[MultiPositionEventReport,
                                         Tuple[EventReassessmentOutcome, ...]]:
        """
        One event, several theses, each judged on its own merits.

        There is no portfolio verdict and no aggregate recommendation --
        `MultiPositionEventReport` has no field that could hold one.
        The aggregate notional it does report is a DIAGNOSTIC for a
        human, not an instruction to resize anything.

        Theses are processed in a deterministic order (thesis id), so a
        sector event produces the same set of assessments every run.
        """
        outcomes: List[EventReassessmentOutcome] = []
        sectors = dict(sector_ids or {})
        for thesis in sorted(theses, key=lambda t: t.thesis_id):
            outcomes.append(self.reassess(
                bundle, thesis, as_of=as_of, history=history,
                sector_id=sectors.get(thesis.instrument_id), **kwargs))

        notional = None
        if exposures:
            values = [exposures.get(t.instrument_id) for t in theses]
            known = [v for v in values if v is not None]
            notional = round(sum(known), 6) if known else None

        report = MultiPositionEventReport(
            report_id="mpr-" + hashlib.sha1(
                f"{bundle.canonical_event_id}|{bundle.revision_key}|"
                f"{as_of.isoformat()}".encode("utf-8")).hexdigest()[:16],
            canonical_event_id=bundle.canonical_event_id, as_of=as_of,
            assessments=tuple(o.assessment for o in outcomes),
            affected_thesis_count=sum(
                1 for o in outcomes if o.assessment.is_actionable_evidence),
            aggregate_notional=notional,
            sector_ids=tuple(sorted(set(bundle.sector_ids))),
            notes=("each thesis was assessed independently; no portfolio-wide "
                   "action exists in this phase",))
        return report, tuple(outcomes)

    # ---------------- scheduled events ----------------

    @staticmethod
    def _horizon_end(thesis: PositionThesis) -> Optional[datetime]:
        """
        When the thesis's horizon runs out, or None.

        `ExpectedHorizon` carries `trading_days`, `calendar_days` and
        `expires_at`, and a thesis may set any combination -- 25.9I-E's
        own fixtures use `ExpectedHorizon(7)`, which is SEVEN TRADING
        DAYS with no calendar figure at all. Reading `calendar_days`
        alone would silently produce None for those and quietly drop the
        horizon from every advisory.

        Trading days are converted at 7/5 to approximate weekends. That
        is an approximation and is labelled as one: it ignores holidays,
        and it is used only to decide whether to SUGGEST a review.
        """
        if thesis.horizon is None:
            return None
        if thesis.horizon.expires_at is not None:
            return thesis.horizon.expires_at
        anchor = thesis.horizon.set_at or thesis.created_at
        if anchor is None:
            return None
        if thesis.horizon.calendar_days is not None:
            return anchor + timedelta(days=thesis.horizon.calendar_days)
        if thesis.horizon.trading_days is not None:
            return anchor + timedelta(
                days=round(thesis.horizon.trading_days * 7 / 5))
        return None

    def pre_event_advisory(self, thesis: PositionThesis, *, as_of: datetime,
                           event_type: str,
                           scheduled_for: Optional[datetime] = None,
                           canonical_event_id: Optional[str] = None,
                           exposure_notional: Optional[float] = None,
                           sector_id: Optional[str] = None,
                           gap_risk_relevant: bool = False
                           ) -> Optional[PreEventRiskAdvisory]:
        """
        A scheduled event is approaching a position. ADVISORY ONLY.

        Recommends a review and, when the event lands inside the
        thesis's horizon, fresher data. It cannot reduce, resize, sell
        or move a stop, because `PreEventRiskAdvisory` has no field that
        expresses any of those.
        """
        if scheduled_for is not None and scheduled_for <= as_of:
            return None
        hours = (None if scheduled_for is None
                 else (scheduled_for - as_of).total_seconds() / 3600.0)
        if (self.policy.advisory_window_hours is not None
                and hours is not None
                and hours > self.policy.advisory_window_hours):
            return None

        remaining: Optional[int] = None
        reasons: List[str] = [
            f"a scheduled {event_type} event is ahead of an open position"]
        inside_horizon = False
        end = self._horizon_end(thesis)
        if end is not None:
            remaining = max(0, (end - as_of).days)
            if scheduled_for is not None and scheduled_for <= end:
                inside_horizon = True
                reasons.append(
                    "the event falls inside the thesis horizon, so it will "
                    "resolve before the thesis does")
        else:
            reasons.append(
                "the thesis horizon has no end date, so whether the event "
                "resolves inside it is unknown")

        if gap_risk_relevant:
            reasons.append(
                "gap risk: the event resolves outside trading hours and no "
                "protective order exists at the venue")

        return PreEventRiskAdvisory(
            advisory_id="adv-" + hashlib.sha1(
                f"{thesis.thesis_id}|{event_type}|"
                f"{scheduled_for.isoformat() if scheduled_for else ''}"
                .encode("utf-8")).hexdigest()[:16],
            thesis_id=thesis.thesis_id, instrument_id=thesis.instrument_id,
            as_of=as_of, event_type=event_type, scheduled_for=scheduled_for,
            canonical_event_id=canonical_event_id,
            horizon_days_remaining=remaining,
            exposure_notional=exposure_notional,
            gap_risk_relevant=gap_risk_relevant, sector_id=sector_id,
            recommend_thesis_review=True,
            recommend_risk_review=bool(gap_risk_relevant or inside_horizon),
            requested_market_data_state=(
                MarketDataState.WARM if inside_horizon else MarketDataState.COLD),
            reasons=tuple(reasons))


def suppression_reason(flags) -> str:
    """
    Why event-driven reassessment would be inert in production (section 51).

    Returns "" when it would run. Event reassessment depends on
    `position_thesis_enabled`: reassessment revises a thesis, and
    without one there is nothing to revise, so the dependency is
    declared in `FLAG_DEPENDENCIES` and suppression is reported here
    rather than discovered at runtime.
    """
    if not getattr(flags, "event_driven_position_reassessment_enabled", False):
        return "event_driven_position_reassessment_enabled is off"
    if not getattr(flags, "position_thesis_enabled", False):
        return ("event_driven_position_reassessment_enabled requires "
                "position_thesis_enabled; reassessment with no thesis has "
                "nothing to revise")
    return ""
