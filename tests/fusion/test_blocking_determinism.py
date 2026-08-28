"""
tests/fusion/test_blocking_determinism.py
-----------------------------------------------------------
Regression guard for candidate-ranking determinism.

BlockingIndex stores candidate ids in sets. Iterating a set of strings
varies between processes under hash randomization, and fusion grouping
depends on candidate order — so without a total ordering the same
input can produce different canonical events on different runs, which
breaks reproducibility.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.fusion.blocking import BlockingIndex
from src.domain.fusion_models import CanonicalEvent
from src.domain.event_models import EventParticipant, ParticipationRole
from src.events.taxonomy import EventType, category_for

PUB = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_event(event_id, fingerprint, entity_id="acme"):
    return CanonicalEvent(
        canonical_event_id=event_id,
        event_type=EventType.ACQUISITION,
        category=category_for(EventType.ACQUISITION),
        title="Acme acquires Beta",
        participants=[EventParticipant(entity_id=entity_id, role=ParticipationRole.PRIMARY)],
        event_time=PUB,
        first_reported_at=PUB,
        fingerprint=fingerprint,
    )


class TestCandidateOrderingIsTotal(unittest.TestCase):
    def test_equal_hit_counts_are_broken_deterministically(self):
        # Many events sharing the same blocking keys => identical hit
        # counts => the tiebreak is what decides order.
        index = BlockingIndex()
        for i in range(30):
            index.add(make_event(f"ce-{i:04d}", f"fp-{i:04d}"))

        from src.domain.fusion_models import EventReport
        from src.domain.event_models import (
            StructuredEvent, EventEvidence, EventConfidence, ExtractionTier, EventStatus,
        )
        se = StructuredEvent(
            event_id="evt-1", event_type=EventType.ACQUISITION,
            category=category_for(EventType.ACQUISITION), title="Acme acquires Beta",
            participants=[EventParticipant(entity_id="acme", role=ParticipationRole.PRIMARY)],
            evidence=[EventEvidence(article_id="a1", source_name="Reuters", published_at=PUB)],
            publication_time=PUB, event_time=PUB,
            confidence=EventConfidence(), status=EventStatus.DETECTED,
            extraction_tier=ExtractionTier.DETERMINISTIC_RULE,
        )
        report = EventReport(report_id="evt-1", structured_event=se)

        first = [e.canonical_event_id for e in index.find_candidates(report)]
        for _ in range(5):
            again = [e.canonical_event_id for e in index.find_candidates(report)]
            self.assertEqual(first, again)

    def test_ordering_follows_fingerprint_on_ties(self):
        index = BlockingIndex()
        index.add(make_event("ce-zzz", "fp-aaa"))
        index.add(make_event("ce-aaa", "fp-zzz"))

        from src.domain.fusion_models import EventReport
        from src.domain.event_models import (
            StructuredEvent, EventEvidence, EventConfidence, ExtractionTier, EventStatus,
        )
        se = StructuredEvent(
            event_id="evt-1", event_type=EventType.ACQUISITION,
            category=category_for(EventType.ACQUISITION), title="Acme acquires Beta",
            participants=[EventParticipant(entity_id="acme", role=ParticipationRole.PRIMARY)],
            evidence=[EventEvidence(article_id="a1", source_name="Reuters", published_at=PUB)],
            publication_time=PUB, event_time=PUB,
            confidence=EventConfidence(), status=EventStatus.DETECTED,
            extraction_tier=ExtractionTier.DETERMINISTIC_RULE,
        )
        report = EventReport(report_id="evt-1", structured_event=se)
        order = [e.canonical_event_id for e in index.find_candidates(report)]
        # fp-aaa sorts before fp-zzz, regardless of the ids.
        self.assertEqual(order[0], "ce-zzz")


if __name__ == "__main__":
    unittest.main()
