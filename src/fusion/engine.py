"""
src/fusion/engine.py
-------------------------
The Event Fusion pipeline (Phase 5, spec §3, §8, §9, §16, §17, §26).

    EVENT REPORT -> CANDIDATE GENERATION (blocking) -> SCORING
                 -> DECISION -> MERGE INTO CANONICAL EVENT / CREATE NEW
                 -> CORROBORATION -> CONTRADICTION -> TIMELINE

WHAT IS PRESERVED, ALWAYS (spec §8): fusing never deletes a report.
The canonical event references its contributing reports; each report
keeps its own extracted values, timestamps, confidence and evidence.
The fusion DECISION is itself recorded, so why a report was attached
is reconstructable later.

IDEMPOTENCY (spec §26): re-processing the same report attaches it to
the same canonical event and creates nothing new — enforced by
report_id, not by wording or provider id.

NO LLM ANYWHERE (spec §29, §30): blocking is dictionary lookup,
scoring is arithmetic, corroboration is counting. FusionStats reports
llm_assisted_cases=0 by construction.
"""

import time
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from src.domain.fusion_models import (
    EventReport, CanonicalEvent, FusionDecision, FusionDecisionState, FusionScore,
    ComparisonMethod, CorroborationState, ContradictionRecord, EventLifecycleState,
    TimelineEntry, TimelineEntryType, ReviewCase, ReviewReason, FusionStats,
    is_valid_transition, ContradictionType,
)
from src.fusion.blocking import BlockingIndex
from src.fusion import scoring
from src.fusion import corroboration as corr

logger = logging.getLogger("marketlens.fusion.engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class FusionEngine:
    """Fuses event reports into canonical events, preserving every report and every decision."""

    def __init__(
        self,
        same_threshold: float = scoring.SAME_EVENT_THRESHOLD,
        possible_threshold: float = scoring.POSSIBLE_SAME_THRESHOLD,
        max_candidates: int = 50,
    ):
        self.same_threshold = same_threshold
        self.possible_threshold = possible_threshold
        self.max_candidates = max_candidates

        self.index = BlockingIndex()
        self.canonical_events: Dict[str, CanonicalEvent] = {}
        self.reports: Dict[str, EventReport] = {}
        self.decisions: List[FusionDecision] = []
        self.contradictions: List[ContradictionRecord] = []
        self.timeline: List[TimelineEntry] = []
        self.review_cases: List[ReviewCase] = []

    # ---------------- helpers ----------------

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _add_timeline(self, canonical_event_id: str, entry_type: TimelineEntryType,
                      description: str, report: Optional[EventReport] = None,
                      old_value: Optional[str] = None, new_value: Optional[str] = None) -> None:
        self.timeline.append(TimelineEntry(
            entry_id=f"tl-{uuid.uuid4().hex[:16]}",
            canonical_event_id=canonical_event_id,
            entry_type=entry_type,
            occurred_at=self._now(),
            description=description,
            report_id=report.report_id if report else None,
            source_name=report.source_name if report else None,
            old_value=old_value, new_value=new_value,
        ))

    def _reports_for(self, event: CanonicalEvent) -> List[EventReport]:
        return [self.reports[rid] for rid in event.report_ids if rid in self.reports]

    def _contradictions_for(self, event_id: str) -> List[ContradictionRecord]:
        return [c for c in self.contradictions if c.canonical_event_id == event_id]

    # ---------------- canonical event creation / merging ----------------

    def _create_canonical_event(self, report: EventReport) -> CanonicalEvent:
        """Promote a report into a new canonical event, seeding its attributes with full provenance."""
        se = report.structured_event
        event = CanonicalEvent(
            canonical_event_id=f"cev-{uuid.uuid4().hex[:16]}",
            event_type=se.event_type,
            category=se.category,
            subtype=se.subtype,
            title=se.title,
            participants=list(se.participants),
            instrument_ids=list(se.instrument_ids),
            sector_ids=list(se.sector_ids),
            geography=se.geography,
            report_ids=[report.report_id],
            first_reported_at=report.publication_time,
            last_updated_at=report.publication_time,
            event_time=se.event_time,
            fingerprint=se.fingerprint,
            # A first report is REPORTED, never CONFIRMED — confirmation
            # requires evidence a single article cannot supply.
            lifecycle_state=EventLifecycleState.REPORTED,
            total_report_count=1,
        )

        for name, value in (se.attributes or {}).items():
            if name == "matched_phrase":
                continue
            event.add_attribute_value(name, corr.build_attribute_value(report, value))

        reports = [report]
        event.corroboration_state = corr.determine_corroboration_state(reports)
        event.independent_source_count = corr.count_independent_sources(reports)
        event.quality_confidence = corr.compute_quality_confidence(event, reports)

        self.canonical_events[event.canonical_event_id] = event
        self.index.add(event)
        report.canonical_event_id = event.canonical_event_id

        self._add_timeline(event.canonical_event_id, TimelineEntryType.REPORT_ADDED,
                            f"Canonical event created from first report by {report.source_name or 'unknown source'}.",
                            report=report)
        return event

    def _merge_report_into_event(self, report: EventReport, event: CanonicalEvent) -> CanonicalEvent:
        """
        Attach a report to an existing canonical event.

        The report is NOT consumed: it keeps its own identity and
        values, and the event gains a reference plus any new attribute
        values (each with its own provenance). Conflicting values are
        recorded as contradictions, never resolved by overwriting.
        """
        if report.report_id in event.report_ids:
            return event   # idempotent: already attached

        now = self._now()
        se = report.structured_event

        # Contradictions must be detected BEFORE the new values are
        # added, while the prior state is still intact.
        attribute_conflicts = corr.detect_attribute_contradictions(event, report, now)
        denial = corr.detect_denial_contradiction(event, report, now)
        new_contradictions = attribute_conflicts + ([denial] if denial else [])
        self.contradictions.extend(new_contradictions)

        for contradiction in new_contradictions:
            event.has_contradictions = True
            self._add_timeline(event.canonical_event_id, TimelineEntryType.CONTRADICTION,
                                contradiction.description, report=report,
                                old_value=contradiction.value_a, new_value=contradiction.value_b)
            self.review_cases.append(ReviewCase(
                review_id=f"rev-{uuid.uuid4().hex[:16]}",
                reason=ReviewReason.CONTRADICTION,
                report_id=report.report_id,
                canonical_event_id=event.canonical_event_id,
                description=contradiction.description,
                created_at=now,
            ))

        # Accumulate attribute values — append, never overwrite (spec §9, §10).
        for name, value in (se.attributes or {}).items():
            if name == "matched_phrase":
                continue
            existing = event.attributes.get(name)
            previous_best = existing.current_best() if existing else None
            event.add_attribute_value(name, corr.build_attribute_value(report, value))
            if previous_best is None:
                self._add_timeline(event.canonical_event_id, TimelineEntryType.ATTRIBUTE_CHANGE,
                                    f"Attribute '{name}' first reported as {value}.",
                                    report=report, new_value=str(value))

        event.report_ids.append(report.report_id)
        event.total_report_count = len(event.report_ids)
        report.canonical_event_id = event.canonical_event_id

        # Widen participants with entities this report adds, without
        # duplicating anyone already present.
        known = {p.entity_id for p in event.participants}
        for participant in se.participants:
            if participant.entity_id not in known:
                event.participants.append(participant)

        if report.publication_time:
            if event.first_reported_at is None or report.publication_time < event.first_reported_at:
                event.first_reported_at = report.publication_time
            event.last_updated_at = max(event.last_updated_at or report.publication_time, report.publication_time)

        reports = self._reports_for(event)
        all_contradictions = self._contradictions_for(event.canonical_event_id)
        previous_state = event.corroboration_state
        event.corroboration_state = corr.determine_corroboration_state(reports, all_contradictions)
        event.independent_source_count = corr.count_independent_sources(reports)
        event.quality_confidence = corr.compute_quality_confidence(event, reports, all_contradictions)
        event.version += 1

        self._add_timeline(event.canonical_event_id, TimelineEntryType.REPORT_ADDED,
                            f"Report from {report.source_name or 'unknown source'} attached.", report=report)

        if previous_state != event.corroboration_state:
            self._add_timeline(event.canonical_event_id, TimelineEntryType.STATE_CHANGE,
                                f"Corroboration state changed from {previous_state.value} to {event.corroboration_state.value}.",
                                report=report, old_value=previous_state.value, new_value=event.corroboration_state.value)

        self._apply_lifecycle_from_corroboration(event, report)

        self.index.remove(event.canonical_event_id)
        self.index.add(event)
        return event

    def _apply_lifecycle_from_corroboration(self, event: CanonicalEvent, report: EventReport) -> None:
        """
        Move the lifecycle state when the evidence justifies it — and
        only through a permitted transition (spec §16).
        """
        target: Optional[EventLifecycleState] = None
        state = event.corroboration_state
        if state == CorroborationState.RETRACTED:
            target = EventLifecycleState.RETRACTED
        elif state == CorroborationState.CONTRADICTED:
            target = EventLifecycleState.DENIED
        elif state == CorroborationState.OFFICIALLY_CONFIRMED:
            target = EventLifecycleState.CONFIRMED
        elif state == CorroborationState.INDEPENDENTLY_CORROBORATED:
            target = EventLifecycleState.CORROBORATED

        if target and target != event.lifecycle_state and is_valid_transition(event.lifecycle_state, target):
            previous = event.lifecycle_state
            event.lifecycle_state = target
            self._add_timeline(event.canonical_event_id, TimelineEntryType.STATE_CHANGE,
                                f"Lifecycle moved from {previous.value} to {target.value}.",
                                report=report, old_value=previous.value, new_value=target.value)

    # ---------------- main entry point ----------------

    def process_report(self, report: EventReport) -> Tuple[CanonicalEvent, FusionDecision]:
        """
        Run one report through the full pipeline.

        Returns:
            (canonical_event, decision). Idempotent: re-processing an
            already-seen report_id returns its existing event and
            records no new decision.
        """
        now = self._now()

        if report.report_id in self.reports:
            existing_id = self.reports[report.report_id].canonical_event_id
            if existing_id and existing_id in self.canonical_events:
                prior = next((d for d in self.decisions if d.report_id == report.report_id), None)
                return self.canonical_events[existing_id], prior or FusionDecision(
                    decision_id=f"fd-{uuid.uuid4().hex[:16]}", report_id=report.report_id,
                    canonical_event_id=existing_id, state=FusionDecisionState.SAME_EVENT,
                    reason="report already processed — idempotent no-op", decided_at=now,
                )

        self.reports[report.report_id] = report
        candidates = self.index.find_candidates(report, self.max_candidates)
        match, state, score, reason = scoring.best_match(report, candidates)

        decision = FusionDecision(
            decision_id=f"fd-{uuid.uuid4().hex[:16]}",
            report_id=report.report_id,
            canonical_event_id=match.canonical_event_id if match else None,
            state=state, score=score, method=ComparisonMethod.STRUCTURED_COMPARISON,
            reason=reason, decided_at=now, candidate_count=len(candidates),
        )
        self.decisions.append(decision)

        if state == FusionDecisionState.SAME_EVENT and match:
            event = self._merge_report_into_event(report, match)
            self._add_timeline(event.canonical_event_id, TimelineEntryType.FUSION_DECISION,
                                f"Fused: {reason}", report=report)
            return event, decision

        if state in (FusionDecisionState.POSSIBLE_SAME_EVENT, FusionDecisionState.NEEDS_REVIEW) and match:
            # A DENIAL, CANCELLATION or RETRACTION is BY NATURE about an
            # existing event, so it attaches to a plausible candidate even
            # on a marginal score. Letting it become an orphan event would
            # leave the original reporting standing unchallenged while the
            # contradiction sat elsewhere — precisely the failure spec §39
            # CASE 4 forbids ("preserve BOTH the reported event and the
            # denial"). The attachment is recorded as a contradiction, never
            # as agreement, and the decision is still logged for review.
            if corr.detect_denial(report) is not None:
                event = self._merge_report_into_event(report, match)
                decision.canonical_event_id = event.canonical_event_id
                self._add_timeline(event.canonical_event_id, TimelineEntryType.FUSION_DECISION,
                                    f"Contradicting report attached despite a marginal fusion score: {reason}",
                                    report=report)
                return event, decision

            # Deliberately NOT merged — a new event is created and the
            # ambiguity is escalated instead (spec §7: never force an
            # uncertain match).
            event = self._create_canonical_event(report)
            self.review_cases.append(ReviewCase(
                review_id=f"rev-{uuid.uuid4().hex[:16]}",
                reason=(ReviewReason.CONFLICTING_ATTRIBUTES if state == FusionDecisionState.NEEDS_REVIEW
                         else ReviewReason.AMBIGUOUS_MATCH),
                report_id=report.report_id, canonical_event_id=event.canonical_event_id,
                candidate_event_ids=[match.canonical_event_id],
                description=f"{state.value}: {reason}. A separate event was created rather than merging.",
                created_at=now,
            ))
            decision.canonical_event_id = event.canonical_event_id
            return event, decision

        event = self._create_canonical_event(report)
        decision.canonical_event_id = event.canonical_event_id
        return event, decision

    def process_batch(self, reports: List[EventReport]) -> Tuple[List[CanonicalEvent], FusionStats]:
        """Process many reports, returning the touched events and full observability stats (spec §32)."""
        started = time.monotonic()
        stats = FusionStats()
        touched: Dict[str, CanonicalEvent] = {}

        for report in reports:
            stats.reports_processed += 1
            try:
                event, decision = self.process_report(report)
            except Exception as exc:  # noqa: BLE001 — one bad report must not abort the batch
                stats.errors += 1
                logger.error("Fusion failed for report %s: %s", report.report_id, exc)
                continue

            touched[event.canonical_event_id] = event
            stats.candidates_generated += decision.candidate_count
            if decision.state == FusionDecisionState.SAME_EVENT:
                stats.same_event += 1
            elif decision.state == FusionDecisionState.POSSIBLE_SAME_EVENT:
                stats.possible_same_event += 1
            elif decision.state == FusionDecisionState.NEEDS_REVIEW:
                stats.needs_review += 1
            elif decision.state == FusionDecisionState.UNRESOLVED:
                stats.unresolved += 1
            else:
                stats.different_event += 1

        stats.canonical_events_created = len(self.canonical_events)
        stats.contradictions_detected = len(self.contradictions)
        stats.duration_seconds = round(time.monotonic() - started, 3)
        logger.info("Fusion run: %s", stats.as_log_dict())
        return list(touched.values()), stats

    # ---------------- corrections & reconstruction ----------------

    def correct_decision(self, original_decision_id: str, new_state: FusionDecisionState,
                          reason: str, corrected_by: str = "human") -> FusionDecision:
        """
        Record a CORRECTION to an earlier fusion decision (spec §27).

        The original decision is never mutated or deleted — it is
        marked as superseded by the new one, so both remain auditable.
        """
        original = next((d for d in self.decisions if d.decision_id == original_decision_id), None)
        if original is None:
            raise ValueError(f"no such fusion decision: {original_decision_id}")

        correction = FusionDecision(
            decision_id=f"fd-{uuid.uuid4().hex[:16]}",
            report_id=original.report_id,
            canonical_event_id=original.canonical_event_id,
            state=new_state, score=original.score, method=ComparisonMethod.HUMAN_REVIEW,
            reason=f"correction by {corrected_by}: {reason}", decided_at=self._now(),
            candidate_count=original.candidate_count,
        )
        original.corrected_by_decision_id = correction.decision_id
        self.decisions.append(correction)

        if original.canonical_event_id:
            self._add_timeline(original.canonical_event_id, TimelineEntryType.CORRECTION,
                                f"Fusion decision corrected from {original.state.value} to {new_state.value}: {reason}",
                                old_value=original.state.value, new_value=new_state.value)
        return correction

    def timeline_for(self, canonical_event_id: str) -> List[TimelineEntry]:
        """Chronological history of one canonical event (spec §17)."""
        entries = [e for e in self.timeline if e.canonical_event_id == canonical_event_id]
        return sorted(entries, key=lambda e: e.occurred_at)

    def state_as_of(self, canonical_event_id: str, as_of: datetime) -> Dict[str, Any]:
        """
        Reconstruct what the system believed about an event at a past
        moment (spec §35) — built purely from the retained timeline, so
        it stays accurate no matter how the event later evolved.
        """
        entries = [e for e in self.timeline_for(canonical_event_id) if e.occurred_at <= as_of]
        state: Dict[str, Any] = {
            "canonical_event_id": canonical_event_id,
            "as_of": as_of.isoformat(),
            "known_reports": [e.report_id for e in entries
                               if e.entry_type == TimelineEntryType.REPORT_ADDED and e.report_id],
            "entry_count": len(entries),
            "corroboration_state": None,
            "lifecycle_state": None,
            "contradictions_known": sum(1 for e in entries if e.entry_type == TimelineEntryType.CONTRADICTION),
        }
        for entry in entries:
            if entry.entry_type == TimelineEntryType.STATE_CHANGE and entry.new_value:
                if entry.new_value in {s.value for s in CorroborationState}:
                    state["corroboration_state"] = entry.new_value
                if entry.new_value in {s.value for s in EventLifecycleState}:
                    state["lifecycle_state"] = entry.new_value
        return state
