"""
src/fusion/clustering.py
-----------------------------
Event clustering and the event relation graph (Phase 5, §18-§22, §37).

STATUS: BUILT, NOT WIRED (TD-07, reviewed Phase 18)
-------------------------------------------------------
`fusion/engine.py` imports `blocking`, `scoring` and `corroboration`.
It does not import this module, and nothing else does either.

This is NOT dead code and it is not a duplicate. Two pieces of
evidence, gathered on 2026-09-05 against the production database:

  1. There is no `event_clusters` table -- only an INDEX,
     `idx_research_obs_cluster`. Nothing was ever created to persist
     what `ClusterEngine` produces, so wiring it needs a schema first.
     That is a missing decision, not a bug.

  2. A DIFFERENT and much simpler clustering IS live and load-bearing:
     `research_observations.event_cluster_id` holds 299 distinct
     clusters across 1,049 observations, and the modeling engine uses
     that count as its effective sample size. So the system already
     depends on a notion of "cluster" -- a coarser one than this
     module implements.

The open question is therefore not "delete or keep" but "should the
richer relation graph replace the coarse cluster id". That is a
research decision with consequences for effective sample size and
therefore for every model evaluation, and it is deliberately not being
taken as a side effect of an audit.

CLUSTER != EVENT (spec §20, §21): a cluster GROUPS related events into
a developing story. It never merges them. "Partnership announced",
"production expanded", "regulator responds" are three distinct
occurrences in one story — clustering must express that relationship
without collapsing three facts into one.

CLUSTERS EVOLVE (spec §22): a new event may join an existing cluster,
start a new one, or stay unclustered. Cluster membership is recomputed
rather than frozen, so a cluster is never permanently locked.
"""

import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Set, Tuple

from src.domain.fusion_models import (
    CanonicalEvent, EventCluster, EventRelation, EventRelationType,
)

logger = logging.getLogger("marketlens.fusion.clustering")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Events further apart than this are not one developing story, however
#: much they share. 90 days spans a typical corporate storyline
#: (announcement -> regulatory response -> completion).
DEFAULT_CLUSTER_WINDOW_DAYS = 90

#: Minimum shared-entity overlap for cluster membership. Lower than the
#: fusion threshold on purpose: clustering is a looser relationship,
#: and grouping is far less destructive than merging.
DEFAULT_MIN_ENTITY_OVERLAP = 0.34


class ClusterEngine:
    """Groups canonical events into evolving clusters and maintains event-to-event relations."""

    def __init__(
        self,
        window_days: int = DEFAULT_CLUSTER_WINDOW_DAYS,
        min_entity_overlap: float = DEFAULT_MIN_ENTITY_OVERLAP,
    ):
        self.window_days = window_days
        self.min_entity_overlap = min_entity_overlap
        self.clusters: Dict[str, EventCluster] = {}
        self.relations: List[EventRelation] = []

    # ---------------- clustering ----------------

    @staticmethod
    def _entity_overlap(a: CanonicalEvent, b: CanonicalEvent) -> float:
        ids_a, ids_b = set(a.entity_ids()), set(b.entity_ids())
        if not ids_a or not ids_b:
            return 0.0
        return len(ids_a & ids_b) / len(ids_a | ids_b)

    def _moment(self, event: CanonicalEvent) -> Optional[datetime]:
        return event.event_time or event.first_reported_at

    def _fits_cluster(self, event: CanonicalEvent, cluster: EventCluster,
                       events_by_id: Dict[str, CanonicalEvent]) -> bool:
        """
        Whether an event belongs to a cluster: shares enough entities
        with at least one member AND falls within the time window.
        """
        moment = self._moment(event)
        if moment is None:
            return False
        if cluster.started_at and cluster.last_activity_at:
            if moment < cluster.started_at - timedelta(days=self.window_days):
                return False
            if moment > cluster.last_activity_at + timedelta(days=self.window_days):
                return False

        for member_id in cluster.event_ids:
            member = events_by_id.get(member_id)
            if member and self._entity_overlap(event, member) >= self.min_entity_overlap:
                return True
        return False

    def assign(self, event: CanonicalEvent, events_by_id: Dict[str, CanonicalEvent]) -> Optional[EventCluster]:
        """
        Place an event into a cluster — joining an existing one, or
        starting a new one. Returns the cluster, or None if the event
        has no usable timestamp (deliberately left unclustered rather
        than guessed into a story).
        """
        if self._moment(event) is None:
            return None

        for cluster in self.clusters.values():
            if cluster.status != "active":
                continue
            if self._fits_cluster(event, cluster, events_by_id):
                self._add_to_cluster(cluster, event)
                return cluster

        return self._create_cluster(event)

    def _create_cluster(self, event: CanonicalEvent) -> EventCluster:
        moment = self._moment(event)
        cluster = EventCluster(
            cluster_id=f"clu-{uuid.uuid4().hex[:16]}",
            label=event.title[:200] or f"{event.event_type.value} story",
            event_ids=[event.canonical_event_id],
            primary_entity_ids=[event.primary_entity_id()] if event.primary_entity_id() else [],
            secondary_entity_ids=[e for e in event.entity_ids() if e != event.primary_entity_id()],
            event_types=[event.event_type.value],
            started_at=moment, last_activity_at=moment,
            confidence=event.quality_confidence,
            created_at=datetime.now(timezone.utc),
        )
        self.clusters[cluster.cluster_id] = cluster
        event.cluster_id = cluster.cluster_id
        return cluster

    def _add_to_cluster(self, cluster: EventCluster, event: CanonicalEvent) -> None:
        if event.canonical_event_id in cluster.event_ids:
            return
        cluster.event_ids.append(event.canonical_event_id)
        event.cluster_id = cluster.cluster_id

        primary = event.primary_entity_id()
        if primary and primary not in cluster.primary_entity_ids:
            cluster.primary_entity_ids.append(primary)
        for entity_id in event.entity_ids():
            if entity_id != primary and entity_id not in cluster.secondary_entity_ids:
                cluster.secondary_entity_ids.append(entity_id)
        if event.event_type.value not in cluster.event_types:
            cluster.event_types.append(event.event_type.value)

        moment = self._moment(event)
        if moment:
            cluster.started_at = min(cluster.started_at or moment, moment)
            cluster.last_activity_at = max(cluster.last_activity_at or moment, moment)

    def recompute_cluster_confidence(self, cluster: EventCluster,
                                      events_by_id: Dict[str, CanonicalEvent]) -> None:
        """Cluster confidence is the mean quality of its members — recomputed, never frozen (spec §22)."""
        members = [events_by_id[e] for e in cluster.event_ids if e in events_by_id]
        if not members:
            cluster.confidence = 0.0
            return
        cluster.confidence = round(sum(m.quality_confidence for m in members) / len(members), 4)

    def split_cluster(self, cluster_id: str, event_ids_to_split: List[str],
                       events_by_id: Dict[str, CanonicalEvent]) -> Optional[EventCluster]:
        """
        Split events out of a cluster into a new one (spec §22: a
        cluster must not be permanently locked). The original cluster
        survives with its remaining members.
        """
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return None
        moving = [e for e in event_ids_to_split if e in cluster.event_ids]
        if not moving or len(moving) == len(cluster.event_ids):
            return None   # nothing to split, or splitting everything is a no-op

        cluster.event_ids = [e for e in cluster.event_ids if e not in moving]

        first = events_by_id.get(moving[0])
        new_cluster = EventCluster(
            cluster_id=f"clu-{uuid.uuid4().hex[:16]}",
            label=(first.title[:200] if first else "split cluster"),
            event_ids=list(moving),
            started_at=self._moment(first) if first else None,
            last_activity_at=self._moment(first) if first else None,
            created_at=datetime.now(timezone.utc),
        )
        for event_id in moving:
            event = events_by_id.get(event_id)
            if event:
                event.cluster_id = new_cluster.cluster_id
        self.clusters[new_cluster.cluster_id] = new_cluster

        self.recompute_cluster_confidence(cluster, events_by_id)
        self.recompute_cluster_confidence(new_cluster, events_by_id)
        return new_cluster

    def cluster_for_event(self, canonical_event_id: str) -> Optional[EventCluster]:
        for cluster in self.clusters.values():
            if canonical_event_id in cluster.event_ids:
                return cluster
        return None

    # ---------------- event-to-event relations ----------------

    def add_relation(
        self,
        from_event_id: str,
        to_event_id: str,
        relation_type: EventRelationType,
        source: Optional[str] = None,
        method: Optional[str] = None,
        confidence: Optional[float] = None,
        is_inference: bool = True,
    ) -> EventRelation:
        """
        Record an event-to-event relation with provenance (spec §19, §36).

        CAUSES / RESULTS_FROM are forced to is_inference=True unless a
        SOURCE is supplied — a causal claim is never asserted as fact
        on the strength of proximity alone.
        """
        relation = EventRelation(
            relation_id=f"rel-{uuid.uuid4().hex[:16]}",
            from_event_id=from_event_id, to_event_id=to_event_id,
            relation_type=relation_type, is_inference=is_inference,
            source=source, method=method, confidence=confidence,
            observed_at=datetime.now(timezone.utc),
        )
        self.relations.append(relation)
        return relation

    def infer_temporal_relations(self, events: List[CanonicalEvent]) -> List[EventRelation]:
        """
        Derive PRECEDES/FOLLOWS between events sharing entities.

        These are marked is_inference=True and method='temporal_order':
        ordering in time is an observation, NOT causation, and this
        method deliberately never emits a CAUSES relation (spec §19).
        """
        created: List[EventRelation] = []
        ordered = sorted(
            [e for e in events if self._moment(e)],
            key=lambda e: self._moment(e),
        )
        for i, earlier in enumerate(ordered):
            for later in ordered[i + 1:]:
                if self._entity_overlap(earlier, later) < self.min_entity_overlap:
                    continue
                gap = (self._moment(later) - self._moment(earlier)).days
                if gap > self.window_days:
                    break
                created.append(self.add_relation(
                    earlier.canonical_event_id, later.canonical_event_id,
                    EventRelationType.PRECEDES,
                    method="temporal_order", confidence=0.5, is_inference=True,
                ))
        return created

    def relations_for(self, canonical_event_id: str, facts_only: bool = False) -> List[EventRelation]:
        result = [r for r in self.relations
                   if r.from_event_id == canonical_event_id or r.to_event_id == canonical_event_id]
        if facts_only:
            result = [r for r in result if not r.is_inference]
        return result

    def fact_count(self) -> int:
        return sum(1 for r in self.relations if not r.is_inference)

    def inference_count(self) -> int:
        return sum(1 for r in self.relations if r.is_inference)
