"""
src/fusion/blocking.py
---------------------------
Candidate generation / blocking (Phase 5, spec §4, §30).

THE PROBLEM THIS SOLVES: comparing every incoming report against every
historical event is O(n²) and becomes impossible at millions of
reports. Blocking reduces each incoming report to a small set of
PLAUSIBLE candidates via indexed keys, so the expensive scoring step
only ever runs on a handful of pairs.

BLOCKING KEYS (all cheap, all indexable):
    type + primary entity + time bucket
    type + each other entity + time bucket

A report generates several keys — one per participating entity — so
"NVIDIA partners with TSMC" is findable from either side. Two reports
share a block if ANY key matches, which is what makes recall high
while keeping each block small.

WHY BLOCKING IS NOT THE DECISION: sharing a block only means "worth
comparing". The actual same/different judgement is scoring.py's job.
Blocking is deliberately permissive; scoring is deliberately strict.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Set, Optional, Dict

from src.domain.fusion_models import EventReport, CanonicalEvent

logger = logging.getLogger("marketlens.fusion.blocking")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Blocking time bucket. Wider than the fusion time tolerance on
#: purpose: a block that is too narrow silently loses true matches,
#: which scoring can never recover, whereas a slightly wide block only
#: costs a few extra comparisons.
BLOCKING_BUCKET_DAYS = 7


def time_bucket(moment: Optional[datetime], bucket_days: int = BLOCKING_BUCKET_DAYS) -> str:
    """Coarse, epoch-anchored time key. 'unknown' for a missing timestamp — never invented."""
    if moment is None:
        return "unknown"
    days = (moment - datetime(1970, 1, 1, tzinfo=moment.tzinfo)).days
    return f"tb{days // bucket_days}"


def blocking_keys_for_report(report: EventReport, bucket_days: int = BLOCKING_BUCKET_DAYS) -> Set[str]:
    """
    Every blocking key a report belongs to.

    Includes the ADJACENT time buckets as well as its own: without
    that, two reports minutes apart but astride a bucket boundary would
    never be compared — a silent, systematic miss.
    """
    moment = report.structured_event.event_time or report.publication_time
    event_type = report.event_type.value
    keys: Set[str] = set()

    if moment is None:
        for entity_id in report.entity_ids:
            keys.add(f"{event_type}|{entity_id}|unknown")
        return keys

    for offset_days in (-bucket_days, 0, bucket_days):
        bucket = time_bucket(moment + timedelta(days=offset_days), bucket_days)
        for entity_id in report.entity_ids:
            keys.add(f"{event_type}|{entity_id}|{bucket}")
    return keys


def blocking_keys_for_event(event: CanonicalEvent, bucket_days: int = BLOCKING_BUCKET_DAYS) -> Set[str]:
    """Blocking keys for a canonical event — the same scheme, so reports and events land in the same blocks."""
    moment = event.event_time or event.first_reported_at
    event_type = event.event_type.value
    keys: Set[str] = set()

    if moment is None:
        for entity_id in event.entity_ids():
            keys.add(f"{event_type}|{entity_id}|unknown")
        return keys

    bucket = time_bucket(moment, bucket_days)
    for entity_id in event.entity_ids():
        keys.add(f"{event_type}|{entity_id}|{bucket}")
    return keys


class BlockingIndex:
    """
    In-memory blocking index over canonical events.

    Kept in memory deliberately (spec §30: "do not introduce
    unnecessary distributed infrastructure"). The same key scheme is
    what fusion_repository.find_candidates() uses against SQL indexes,
    so moving to a database-backed lookup at scale requires no change
    to the keys themselves.
    """

    def __init__(self, bucket_days: int = BLOCKING_BUCKET_DAYS):
        self.bucket_days = bucket_days
        self._by_key: Dict[str, Set[str]] = {}
        self._events: Dict[str, CanonicalEvent] = {}

    def add(self, event: CanonicalEvent) -> None:
        self._events[event.canonical_event_id] = event
        for key in blocking_keys_for_event(event, self.bucket_days):
            self._by_key.setdefault(key, set()).add(event.canonical_event_id)

    def add_all(self, events: List[CanonicalEvent]) -> None:
        for event in events:
            self.add(event)

    def remove(self, canonical_event_id: str) -> None:
        event = self._events.pop(canonical_event_id, None)
        if not event:
            return
        for key in blocking_keys_for_event(event, self.bucket_days):
            bucket = self._by_key.get(key)
            if bucket:
                bucket.discard(canonical_event_id)
                if not bucket:
                    del self._by_key[key]

    def find_candidates(self, report: EventReport, max_candidates: int = 50) -> List[CanonicalEvent]:
        """
        Return plausible canonical events for this report.

        `max_candidates` is a hard bound: a pathological block (a very
        common entity in a busy week) must never make one report's
        processing unbounded. Candidates are ordered by how many
        blocking keys they share, so the most plausible survive the cut.
        """
        keys = blocking_keys_for_report(report, self.bucket_days)
        hit_counts: Dict[str, int] = {}
        for key in keys:
            for event_id in self._by_key.get(key, ()):
                hit_counts[event_id] = hit_counts.get(event_id, 0) + 1

        ranked = sorted(hit_counts.items(), key=lambda kv: kv[1], reverse=True)[:max_candidates]
        return [self._events[event_id] for event_id in (eid for eid, _ in ranked) if event_id in self._events]

    def size(self) -> int:
        return len(self._events)

    def key_count(self) -> int:
        return len(self._by_key)
