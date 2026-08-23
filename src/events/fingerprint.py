"""
src/events/fingerprint.py
------------------------------
Event identity / fingerprinting (Phase 4, spec §16, §17).

PURPOSE: decide whether a newly-extracted event candidate is a NEW
event or another report of an EXISTING one — without creating a
duplicate event per article.

THE HARD REQUIREMENT (spec §17): the fingerprint must tolerate wording
differences. "Company X acquires Company Y" and "Company X agrees to
purchase Company Y" describe one event and must fingerprint alike.

HOW THAT IS ACHIEVED: the fingerprint deliberately IGNORES the
headline wording entirely. It is built from the structured facts that
identify an occurrence — event type, the participating canonical
entity ids (Phase 3), and a coarse time bucket — none of which change
when an outlet rephrases the story. Wording similarity is used only as
a secondary signal, never as the identity itself.

NO LLM (spec §16): identity is hashing plus set comparison.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from src.domain.event_models import StructuredEvent, ParticipationRole


#: Occurrences of the same type involving the same entities within one
#: bucket are treated as one event. 3 days balances two real failure
#: modes: too narrow splits a story reported across a weekend; too wide
#: merges this quarter's earnings with next quarter's.
DEFAULT_BUCKET_DAYS = 3


def _time_bucket(moment: Optional[datetime], bucket_days: int = DEFAULT_BUCKET_DAYS) -> str:
    """
    Coarse time key. Uses a fixed-width bucket anchored to the epoch,
    so two events days apart still land together when they should.
    Returns "unknown" for a missing timestamp rather than inventing one.
    """
    if moment is None:
        return "unknown"
    days_since_epoch = (moment - datetime(1970, 1, 1, tzinfo=moment.tzinfo)).days
    return f"b{days_since_epoch // bucket_days}"


def compute_event_fingerprint(event: StructuredEvent, bucket_days: int = DEFAULT_BUCKET_DAYS) -> str:
    """
    Build the identity fingerprint for an event.

    Uses: event type + sorted primary/secondary entity ids + time
    bucket. Entity ids are sorted so "X partners with Y" and "Y
    partners with X" produce the same fingerprint — the same real
    partnership, reported from either side.

    AFFECTED/REFERENCED participants are excluded on purpose: which
    peripheral companies an outlet chose to name varies per article and
    would wrongly split one event into several.
    """
    identifying_roles = (ParticipationRole.PRIMARY, ParticipationRole.SECONDARY)
    entity_ids = sorted({p.entity_id for p in event.participants if p.role in identifying_roles})
    moment = event.event_time or event.publication_time
    basis = f"{event.event_type.value}|{','.join(entity_ids)}|{_time_bucket(moment, bucket_days)}"
    return "evt-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _entity_overlap(a: StructuredEvent, b: StructuredEvent) -> float:
    """Jaccard overlap of the two events' participating entity id sets, in [0.0, 1.0]."""
    ids_a = {p.entity_id for p in a.participants}
    ids_b = {p.entity_id for p in b.participants}
    if not ids_a or not ids_b:
        return 0.0
    return len(ids_a & ids_b) / len(ids_a | ids_b)


def is_same_event(
    candidate: StructuredEvent,
    existing: StructuredEvent,
    bucket_days: int = DEFAULT_BUCKET_DAYS,
    min_entity_overlap: float = 0.5,
    max_time_gap_days: int = 7,
) -> Tuple[bool, str]:
    """
    Decide whether `candidate` describes the same occurrence as
    `existing`, in two tiers.

    Tier 1 — exact fingerprint match: same type, same identifying
    entities, same time bucket. Certain, free.

    Tier 2 — near match: same type, substantial entity overlap, and
    within `max_time_gap_days`. This catches the case where two reports
    straddle a bucket boundary, which Tier 1 alone would miss.

    Returns:
        (is_same, reason) — the reason is always recorded so a merge
        decision is auditable rather than a black box.
    """
    if candidate.event_type != existing.event_type:
        return False, "different event type"

    if compute_event_fingerprint(candidate, bucket_days) == compute_event_fingerprint(existing, bucket_days):
        return True, "exact fingerprint match"

    overlap = _entity_overlap(candidate, existing)
    if overlap < min_entity_overlap:
        return False, f"insufficient entity overlap ({overlap:.2f})"

    a_time = candidate.event_time or candidate.publication_time
    b_time = existing.event_time or existing.publication_time
    if a_time is None or b_time is None:
        return False, "missing timestamp on one side — refusing to merge on incomplete evidence"

    gap_days = abs((a_time - b_time).total_seconds()) / 86400
    if gap_days > max_time_gap_days:
        return False, f"time gap too large ({gap_days:.1f} days)"

    return True, f"near match (entity overlap {overlap:.2f}, {gap_days:.1f} days apart)"


def find_matching_event(
    candidate: StructuredEvent,
    existing_events: List[StructuredEvent],
    bucket_days: int = DEFAULT_BUCKET_DAYS,
) -> Tuple[Optional[StructuredEvent], Optional[str]]:
    """
    Find the first existing event that `candidate` duplicates.

    The caller is responsible for keeping `existing_events` bounded
    (see EventRepository.find_candidate_events, which restricts by type
    and time window) — this never scans everything.
    """
    for existing in existing_events:
        if existing.event_id == candidate.event_id:
            continue
        same, reason = is_same_event(candidate, existing, bucket_days)
        if same:
            return existing, reason
    return None, None
