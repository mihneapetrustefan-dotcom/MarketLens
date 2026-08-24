"""
src/fusion/corroboration.py
--------------------------------
Corroboration, source independence, and contradiction detection
(Phase 5, spec §11-§15).

THE DISTINCTION THAT DRIVES THIS FILE (spec §14):

    CORROBORATION  several INDEPENDENT sources report substantially
                   the same thing
    CONFIRMATION   one AUTHORITATIVE source (filing, regulator,
                   company statement) confirms it

They are different axes. Three outlets repeating a rumour is neither.
Ten sites carrying one wire story is not even multi-source in any
meaningful sense — it is one source, ten times (spec §12).

WHAT COUNTS AS INDEPENDENT: only a report with positive lineage
evidence that it is an ORIGINAL_REPORT. Absence of lineage information
means UNKNOWN, and unknown never counts as independent. That is the
conservative direction on purpose: overstating independence
manufactures false confidence, which is the more dangerous error.
"""

import re
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from src.domain.fusion_models import (
    EventReport, CanonicalEvent, CorroborationState, ContradictionRecord,
    ContradictionType, SourceCategory, ConsolidatedAttribute, AttributeValue,
)

logger = logging.getLogger("marketlens.fusion.corroboration")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Source categories whose statement constitutes CONFIRMATION rather
#: than mere reporting. Deliberately narrow: a company or regulator
#: speaking about its own action is qualitatively different evidence
#: from a publication reporting on it.
AUTHORITATIVE_CATEGORIES = {
    SourceCategory.REGULATORY_FILING,
    SourceCategory.OFFICIAL_COMPANY,
    SourceCategory.GOVERNMENT,
}

#: Phrases indicating a report DENIES rather than describes an event.
_DENIAL_MARKERS = [
    "denies", "denied", "rejects the report", "no such discussions",
    "declined to confirm", "dismissed reports", "refutes",
]
_CANCELLATION_MARKERS = ["call off", "calls off", "called off", "cancelled", "canceled",
                          "abandons", "abandoned", "terminates the deal", "walks away", "scrapped"]
_RETRACTION_MARKERS = ["retracts", "retracted", "withdraws the report", "correction:", "we regret the error"]


def count_independent_sources(reports: List[EventReport]) -> int:
    """
    Count DISTINCT independent sources.

    Independence requires positive evidence (an ORIGINAL_REPORT
    lineage). Reports with unknown or derived lineage are excluded —
    never counted optimistically.
    """
    independent_names = {
        r.source_name for r in reports
        if r.is_independent() and r.source_name
    }
    return len(independent_names)


def count_distinct_sources(reports: List[EventReport]) -> int:
    """Distinct source NAMES, regardless of independence — the 'how many outlets' number, not the 'how many confirmations' number."""
    return len({r.source_name for r in reports if r.source_name})


def has_authoritative_confirmation(reports: List[EventReport]) -> bool:
    """Whether any report comes from an authoritative source (spec §14's CONFIRMATION axis)."""
    return any(r.source_category in AUTHORITATIVE_CATEGORIES for r in reports)


def _text_of(report: EventReport) -> str:
    ev = report.structured_event
    excerpt = ev.evidence[0].excerpt if ev.evidence else ""
    return f"{ev.title} {ev.description} {excerpt or ''}".lower()


def detect_denial(report: EventReport) -> Optional[ContradictionType]:
    """
    Detect whether a report denies, cancels, or retracts rather than
    describes. Deterministic phrase matching — no model call.
    """
    text = _text_of(report)
    if any(marker in text for marker in _RETRACTION_MARKERS):
        return ContradictionType.RETRACTION
    if any(marker in text for marker in _CANCELLATION_MARKERS):
        return ContradictionType.CANCELLATION
    if any(marker in text for marker in _DENIAL_MARKERS):
        return ContradictionType.DIRECT_DENIAL
    return None


def detect_attribute_contradictions(
    canonical_event: CanonicalEvent,
    report: EventReport,
    now: Optional[datetime] = None,
) -> List[ContradictionRecord]:
    """
    Find attributes where this report disagrees with what is already
    recorded (spec §15, §39 CASE 6).

    Both values are preserved with provenance — this NEVER picks a
    winner, it records the disagreement.
    """
    now = now or datetime.now(timezone.utc)
    contradictions: List[ContradictionRecord] = []
    report_attrs = report.structured_event.attributes or {}

    for name, new_value in report_attrs.items():
        if name == "matched_phrase":
            continue
        existing = canonical_event.attributes.get(name)
        if not existing:
            continue
        best = existing.current_best()
        if best is None or str(best.value) == str(new_value):
            continue

        contradictions.append(ContradictionRecord(
            contradiction_id=f"con-{uuid.uuid4().hex[:16]}",
            canonical_event_id=canonical_event.canonical_event_id,
            contradiction_type=ContradictionType.MATERIAL_ATTRIBUTE_CONFLICT,
            report_id_a=best.report_id,
            report_id_b=report.report_id,
            field_name=name,
            value_a=str(best.value),
            value_b=str(new_value),
            description=(f"Conflicting values for '{name}': "
                          f"{best.source_name or 'unknown source'} reported {best.value}, "
                          f"{report.source_name or 'unknown source'} reported {new_value}. "
                          f"Both preserved; neither selected."),
            detected_at=now,
        ))
    return contradictions


def detect_denial_contradiction(
    canonical_event: CanonicalEvent,
    report: EventReport,
    now: Optional[datetime] = None,
) -> Optional[ContradictionRecord]:
    """Build a contradiction record when a report denies/cancels/retracts an event (spec §39 CASE 4)."""
    denial_type = detect_denial(report)
    if not denial_type:
        return None
    now = now or datetime.now(timezone.utc)
    return ContradictionRecord(
        contradiction_id=f"con-{uuid.uuid4().hex[:16]}",
        canonical_event_id=canonical_event.canonical_event_id,
        contradiction_type=denial_type,
        report_id_a=canonical_event.report_ids[0] if canonical_event.report_ids else "",
        report_id_b=report.report_id,
        description=(f"{report.source_name or 'A source'} {denial_type.value.replace('_', ' ')} "
                      f"the reported event. Both the original reporting and this contradiction are retained."),
        detected_at=now,
    )


def determine_corroboration_state(
    reports: List[EventReport],
    contradictions: Optional[List[ContradictionRecord]] = None,
) -> CorroborationState:
    """
    Derive the corroboration state from evidence (spec §13).

    Precedence, strongest signal first:
      RETRACTED / CONTRADICTED  a denial or retraction outranks any
                                 amount of prior reporting
      OFFICIALLY_CONFIRMED      an authoritative source spoke
      INDEPENDENTLY_CORROBORATED 2+ genuinely independent sources
      MULTI_SOURCE              several outlets, independence unproven
      SINGLE_SOURCE / UNCONFIRMED

    Note MULTI_SOURCE sits BELOW independent corroboration on purpose:
    it explicitly does not claim confirmation (spec §13's warning).
    """
    contradictions = contradictions or []

    if any(c.contradiction_type == ContradictionType.RETRACTION for c in contradictions):
        return CorroborationState.RETRACTED
    if any(c.contradiction_type in (ContradictionType.DIRECT_DENIAL, ContradictionType.CANCELLATION)
            for c in contradictions):
        return CorroborationState.CONTRADICTED

    if not reports:
        return CorroborationState.UNCONFIRMED
    if has_authoritative_confirmation(reports):
        return CorroborationState.OFFICIALLY_CONFIRMED

    independent = count_independent_sources(reports)
    if independent >= 2:
        return CorroborationState.INDEPENDENTLY_CORROBORATED

    distinct = count_distinct_sources(reports)
    if distinct >= 2:
        return CorroborationState.MULTI_SOURCE
    if distinct == 1:
        return CorroborationState.SINGLE_SOURCE
    return CorroborationState.UNCONFIRMED


def compute_quality_confidence(
    canonical_event: CanonicalEvent,
    reports: List[EventReport],
    contradictions: Optional[List[ContradictionRecord]] = None,
) -> float:
    """
    How confident we are the event is CORRECTLY REPRESENTED (spec §24).

    Explicitly NOT market importance (spec §25) — nothing here
    considers how much the market might care. A well-evidenced trivial
    event scores high; a poorly-evidenced enormous one scores low.

    Components, each in [0,1], averaged with stated weights:
      extraction    average extraction confidence of contributing reports
      corroboration state-derived
      consistency   penalised by unresolved contradictions
      provenance    share of reports with known (not UNKNOWN) lineage
    """
    contradictions = contradictions or []
    if not reports:
        return 0.0

    extraction_scores = [r.structured_event.confidence.score() for r in reports]
    extraction = sum(extraction_scores) / len(extraction_scores)

    state = determine_corroboration_state(reports, contradictions)
    corroboration_value = {
        CorroborationState.OFFICIALLY_CONFIRMED: 1.0,
        CorroborationState.INDEPENDENTLY_CORROBORATED: 0.85,
        CorroborationState.MULTI_SOURCE: 0.6,
        CorroborationState.SINGLE_SOURCE: 0.4,
        CorroborationState.UNCONFIRMED: 0.2,
        CorroborationState.CONTRADICTED: 0.3,
        CorroborationState.RETRACTED: 0.1,
    }.get(state, 0.3)

    unresolved = [c for c in contradictions if not c.resolved]
    consistency = max(0.0, 1.0 - 0.25 * len(unresolved))

    known_lineage = sum(1 for r in reports if r.lineage is not None)
    provenance = known_lineage / len(reports)

    weights = {"extraction": 0.30, "corroboration": 0.35, "consistency": 0.20, "provenance": 0.15}
    total = (extraction * weights["extraction"]
             + corroboration_value * weights["corroboration"]
             + consistency * weights["consistency"]
             + provenance * weights["provenance"])
    return round(min(1.0, max(0.0, total)), 4)


def build_attribute_value(report: EventReport, value: Any) -> AttributeValue:
    """Wrap a reported value with full provenance (spec §10)."""
    return AttributeValue(
        value=value,
        report_id=report.report_id,
        source_name=report.source_name,
        source_category=report.source_category,
        confidence=report.structured_event.confidence.score(),
        extraction_method=report.structured_event.extraction_tier.value,
        reported_at=report.publication_time,
    )
