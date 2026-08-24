"""
src/fusion/scoring.py
--------------------------
Fusion scoring and the same/different decision (Phase 5, spec §5, §6, §7).

DESIGN BIAS, STATED UP FRONT: a false merge destroys information
irrecoverably — two real events become one, and the second can never
be recovered. A missed merge merely leaves two rows that a later
correction can join. Every threshold and every hard gate below is
therefore tuned to fail toward NOT merging.

HARD GATES (checked before scoring; any failure ends the comparison):
    1. different event type          -> DIFFERENT_EVENT
    2. no shared identifying entity  -> DIFFERENT_EVENT
    3. beyond the maximum time gap   -> DIFFERENT_EVENT

These gates are what make spec §39's critical failure cases
structurally impossible rather than merely unlikely — "X acquires Y in
January" and "X acquires Z in February" fail gate 2 AND gate 3.

SEMANTIC SIMILARITY here is token-set overlap on titles, NOT an
embedding and NOT a model call (spec §29, §30). It is a weak signal by
design, weighted accordingly, and never sufficient on its own.
"""

import re
import logging
from datetime import datetime
from typing import Optional, Tuple, Set, List, Any

from src.domain.fusion_models import (
    EventReport, CanonicalEvent, FusionScore, FusionDecisionState,
)

logger = logging.getLogger("marketlens.fusion.scoring")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: A report further than this from an event is a different occurrence,
#: regardless of how similar it looks. 14 days accommodates a story
#: that develops over weeks while keeping quarterly repeats apart.
MAX_TIME_GAP_DAYS = 14

#: Score at or above this fuses automatically.
SAME_EVENT_THRESHOLD = 0.85
#: Between this and SAME_EVENT_THRESHOLD: plausible, but NEVER merged
#: automatically — flagged for review instead (spec §7).
POSSIBLE_SAME_THRESHOLD = 0.65


def _normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _token_set(text: Optional[str]) -> Set[str]:
    """
    Tokens for similarity. Short tokens containing digits are KEPT —
    the same lesson learned in Phase 2: "Q2" vs "Q3" and "5%" vs "9%"
    are exactly the tokens that distinguish otherwise-identical
    financial headlines.
    """
    return {t for t in _normalize(text).split() if len(t) > 2 or any(c.isdigit() for c in t)}


def jaccard(a: Set[Any], b: Set[Any]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def entity_similarity(report: EventReport, event: CanonicalEvent) -> float:
    """Jaccard overlap of participating entity id sets."""
    return jaccard(set(report.entity_ids), set(event.entity_ids()))


def temporal_similarity(report_time: Optional[datetime], event_time: Optional[datetime]) -> Optional[float]:
    """
    1.0 for simultaneous, decaying linearly to 0.0 at MAX_TIME_GAP_DAYS.

    Returns None when either timestamp is missing. Note this never
    lets an undated report slip through: decide()'s hard gate rejects
    a missing timestamp outright before the score is ever consulted.
    """
    if report_time is None or event_time is None:
        return None
    gap_days = abs((report_time - event_time).total_seconds()) / 86400
    if gap_days >= MAX_TIME_GAP_DAYS:
        return 0.0
    return round(1.0 - (gap_days / MAX_TIME_GAP_DAYS), 4)


def geographic_similarity(report: EventReport, event: CanonicalEvent) -> Optional[float]:
    """
    1.0 when both name the same country, 0.0 on explicit disagreement.

    Returns None when either side is silent: unknown is neither
    agreement nor conflict, and scoring it as a middling 0.5 would
    penalise a match purely for the source not mentioning geography.
    """
    report_geo = report.structured_event.geography
    event_geo = event.geography
    if not report_geo or not event_geo:
        return None
    if report_geo.country and event_geo.country:
        return 1.0 if report_geo.country == event_geo.country else 0.0
    return None


def attribute_similarity(report: EventReport, event: CanonicalEvent) -> Optional[float]:
    """
    Agreement across attributes BOTH sides report.

    Returns None when there is no overlap to compare — but a genuine
    DISAGREEMENT scores 0.0 and pulls the fusion score down hard,
    which is how conflicting deal values keep two reports apart
    instead of being quietly merged.
    """
    report_attrs = report.structured_event.attributes or {}
    comparable = [name for name in report_attrs
                   if name in event.attributes and name != "matched_phrase"]
    if not comparable:
        return None

    agreements = 0
    for name in comparable:
        best = event.attributes[name].current_best()
        if best is not None and str(best.value) == str(report_attrs[name]):
            agreements += 1
    return round(agreements / len(comparable), 4)


def instrument_similarity(report: EventReport, event: CanonicalEvent) -> Optional[float]:
    """Jaccard over instrument ids, or None when either side lists none."""
    report_instruments = set(report.structured_event.instrument_ids)
    event_instruments = set(event.instrument_ids)
    if not report_instruments or not event_instruments:
        return None
    return jaccard(report_instruments, event_instruments)


def semantic_similarity(report: EventReport, event: CanonicalEvent) -> float:
    """
    Token-overlap of titles. A WEAK signal, weighted low on purpose:
    different outlets describe one event in different words (so low
    overlap must not block a merge), and unrelated events share
    boilerplate (so high overlap must not force one).
    """
    return jaccard(_token_set(report.structured_event.title), _token_set(event.title))


def compute_fusion_score(report: EventReport, event: CanonicalEvent) -> FusionScore:
    """Compute every scoring component. Pure function — no I/O, no model calls."""
    report_time = report.structured_event.event_time or report.publication_time
    event_moment = event.event_time or event.first_reported_at

    return FusionScore(
        entity_match=entity_similarity(report, event),
        event_type_match=1.0 if report.event_type == event.event_type else 0.0,
        temporal_proximity=temporal_similarity(report_time, event_moment),
        geographic_similarity=geographic_similarity(report, event),
        attribute_similarity=attribute_similarity(report, event),
        instrument_similarity=instrument_similarity(report, event),
        semantic_similarity=semantic_similarity(report, event),
    )


def decide(
    report: EventReport,
    event: CanonicalEvent,
    same_threshold: float = SAME_EVENT_THRESHOLD,
    possible_threshold: float = POSSIBLE_SAME_THRESHOLD,
) -> Tuple[FusionDecisionState, FusionScore, str]:
    """
    Decide whether `report` describes `event`.

    Returns:
        (state, score, reason) — the reason is always a plain-language
        explanation of what drove the decision, so no merge is ever a
        black box (spec §3, §6).
    """
    score = compute_fusion_score(report, event)

    # --- Hard gate 1: event type ---
    if report.event_type != event.event_type:
        return (FusionDecisionState.DIFFERENT_EVENT, score,
                f"different event type ({report.event_type.value} vs {event.event_type.value})")

    # --- Hard gate 2: at least one shared entity ---
    shared = set(report.entity_ids) & set(event.entity_ids())
    if not shared:
        return (FusionDecisionState.DIFFERENT_EVENT, score,
                "no shared entity — different participants means a different occurrence")

    # --- Hard gate 3: time gap ---
    report_time = report.structured_event.event_time or report.publication_time
    event_moment = event.event_time or event.first_reported_at
    if report_time is None or event_moment is None:
        return (FusionDecisionState.UNRESOLVED, score,
                "missing timestamp on one side — refusing to merge on incomplete evidence")
    gap_days = abs((report_time - event_moment).total_seconds()) / 86400
    if gap_days > MAX_TIME_GAP_DAYS:
        return (FusionDecisionState.DIFFERENT_EVENT, score,
                f"time gap of {gap_days:.1f} days exceeds the {MAX_TIME_GAP_DAYS}-day maximum")

    # --- Attribute conflict is decisive, not merely a low score ---
    # Two reports of the same type and entities that disagree on a
    # material attribute are more likely two events (or a genuine
    # dispute needing review) than one clean event.
    if score.attribute_similarity is not None and score.attribute_similarity == 0.0:
        comparable = [n for n in report.structured_event.attributes
                       if n in event.attributes and n != "matched_phrase"]
        if comparable:
            return (FusionDecisionState.NEEDS_REVIEW, score,
                    f"attributes conflict on {', '.join(comparable)} — flagged rather than merged or discarded")

    total = score.score()
    if total >= same_threshold:
        return (FusionDecisionState.SAME_EVENT, score,
                f"fusion score {total} >= {same_threshold} (shared entities: {', '.join(sorted(shared))})")
    if total >= possible_threshold:
        return (FusionDecisionState.POSSIBLE_SAME_EVENT, score,
                f"fusion score {total} is plausible but below {same_threshold} — not merged automatically")
    return (FusionDecisionState.DIFFERENT_EVENT, score,
            f"fusion score {total} below the {possible_threshold} plausibility threshold")


def best_match(
    report: EventReport,
    candidates: List[CanonicalEvent],
) -> Tuple[Optional[CanonicalEvent], FusionDecisionState, FusionScore, str]:
    """
    Score a report against every candidate and return the strongest
    outcome.

    A SAME_EVENT result wins immediately. Otherwise the highest-scoring
    non-different outcome is returned, so a near miss surfaces as
    POSSIBLE_SAME_EVENT/NEEDS_REVIEW rather than being lost.
    """
    if not candidates:
        return None, FusionDecisionState.DIFFERENT_EVENT, FusionScore(), "no candidates in block"

    best: Optional[Tuple[CanonicalEvent, FusionDecisionState, FusionScore, str]] = None
    for candidate in candidates:
        state, score, reason = decide(report, candidate)
        if state == FusionDecisionState.SAME_EVENT:
            return candidate, state, score, reason
        if state in (FusionDecisionState.POSSIBLE_SAME_EVENT, FusionDecisionState.NEEDS_REVIEW):
            if best is None or score.score() > best[2].score():
                best = (candidate, state, score, reason)

    if best:
        return best
    return None, FusionDecisionState.DIFFERENT_EVENT, FusionScore(), "no candidate cleared the plausibility threshold"
