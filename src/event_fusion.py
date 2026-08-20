"""
event_fusion.py
-------------------
Event Fusion module for MarketLens.

RESPONSIBILITY:
Merge multiple articles about the SAME underlying event — e.g. Nvidia's
Q3 earnings, reported independently by Reuters, CNBC, and Bloomberg —
into a SINGLE Event object with its own timeline, instead of treating
each article as an unrelated, independent mention. This is the natural
next step after Event Detector (which tags each article's event
type(s) individually) and Confidence Score (which aggregates by
ENTITY, not by specific event) — Event Fusion aggregates by the actual
EVENT, answering "how many independent sources confirmed THIS specific
happening, and when did each first report it?" rather than just "how
much coverage did this company get overall?".

DESIGN — grouping key and clustering:
Two articles are considered the "same event" if they:
  1. mention at least one common company (by canonical_name), AND
  2. share at least one common event_type (from Event Detector), AND
  3. were published within `time_window_hours` of each other — using
     CHAIN-LINKING (an article joins the current cluster if it's
     within the window of the MOST RECENT article already in it, not
     strictly within the window of the very first one), so a slowly
     unfolding story (e.g. an acquisition that develops over several
     days of follow-up coverage) is captured as one evolving event
     rather than arbitrarily split into disconnected fragments.

WHAT THIS DELIBERATELY DOES NOT DO: it never tries to verify that two
articles describe the literal same facts beyond company + event type +
time proximity — this is a precision/recall trade-off, same
philosophy as the rest of this project's rule-based detectors. A
false merge (two genuinely different EARNINGS stories for the same
company within the window) is possible but rare in practice; flagged
here rather than hidden.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.event_fusion")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class EventFusion:
    """
    Groups processed articles (already tagged by Company Detector and
    Event Detector) into fused Event objects.
    """

    def __init__(self, time_window_hours: float = 72.0):
        """
        Args:
            time_window_hours: maximum gap, in hours, between an
                article and the most recent article already in a
                cluster, for it to still be considered part of the
                SAME unfolding event. Default 72h (3 days) — long
                enough to capture follow-up coverage of a real event,
                short enough that two genuinely separate EARNINGS
                reports (e.g. two different quarters) won't merge.
        """
        self.time_window_hours = time_window_hours

    def _parse_timestamp(self, article: Dict[str, Any]) -> Optional[datetime]:
        """
        Parse an article's effective timestamp (published_at,
        falling back to collected_at) — same convention already used
        by Confidence Score and the Dashboard's representative-article
        selection. Never raises; returns None for anything unparseable.
        """
        raw = article.get("published_at") or article.get("collected_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            return None

    def _cluster_by_time(self, sorted_articles: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        Chain-link cluster a TIME-SORTED list of articles: an article
        joins the current cluster if it's within `time_window_hours`
        of the most recent article already in it; otherwise it starts
        a new cluster. An article with an unparseable timestamp is
        conservatively kept in the current cluster (never used to
        split one) rather than dropped or forced into its own cluster.
        """
        if not sorted_articles:
            return []

        clusters: List[List[Dict[str, Any]]] = []
        current_cluster: List[Dict[str, Any]] = []
        last_known_time: Optional[datetime] = None

        for article in sorted_articles:
            timestamp = self._parse_timestamp(article)
            if not current_cluster:
                current_cluster = [article]
            elif timestamp is None or last_known_time is None:
                current_cluster.append(article)
            elif (timestamp - last_known_time) <= timedelta(hours=self.time_window_hours):
                current_cluster.append(article)
            else:
                clusters.append(current_cluster)
                current_cluster = [article]

            if timestamp is not None:
                last_known_time = timestamp

        if current_cluster:
            clusters.append(current_cluster)
        return clusters

    def _build_event(self, entity: str, event_type: str, cluster_articles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build one fused Event object from a cluster of articles about the same entity + event type."""
        timestamps = [self._parse_timestamp(a) for a in cluster_articles]
        valid_timestamps = [t for t in timestamps if t is not None]

        sources = sorted({a.get("source") for a in cluster_articles if a.get("source")})

        def impact_of(article: Dict[str, Any]) -> float:
            return (article.get("impact") or {}).get("score", 0.0) or 0.0

        representative = max(cluster_articles, key=impact_of, default=None)

        timeline = [
            {
                "title": a.get("title"),
                "source": a.get("source"),
                "url": a.get("url"),
                "published_at": a.get("published_at") or a.get("collected_at"),
            }
            for a in sorted(
                cluster_articles,
                key=lambda a: self._parse_timestamp(a) or datetime.min.replace(tzinfo=timezone.utc),
            )
        ]

        return {
            "entity": entity,
            "event_type": event_type,
            "article_count": len(cluster_articles),
            "source_count": len(sources),
            "sources": sources,
            "confirmed_by_multiple_sources": len(sources) >= 2,
            "first_reported_at": min(valid_timestamps).isoformat() if valid_timestamps else None,
            "last_reported_at": max(valid_timestamps).isoformat() if valid_timestamps else None,
            "representative_title": representative.get("title") if representative else None,
            "representative_url": representative.get("url") if representative else None,
            "representative_source": representative.get("source") if representative else None,
            "timeline": timeline,
        }

    def fuse_events(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Group a batch of processed articles into fused Event objects.

        Args:
            articles: articles already run through the standard
                processing chain (must already carry
                `companies_mentioned` from Company Detector and
                `events` from Event Detector — articles with no
                detected event type are excluded, since Event Fusion
                only makes sense for articles ABOUT a specific
                identifiable event, not routine coverage).

        Returns:
            A list of Event dicts (see _build_event for shape), one
            per (entity, event_type, time cluster) group found.
        """
        taggable = [a for a in articles if a.get("events")]
        if not taggable:
            return []

        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for article in taggable:
            companies = [
                c["canonical_name"] if isinstance(c, dict) else c
                for c in (article.get("companies_mentioned") or [])
            ]
            event_types = {e["event_type"] for e in article["events"]}
            for company in companies:
                for event_type in event_types:
                    groups.setdefault((company, event_type), []).append(article)

        events: List[Dict[str, Any]] = []
        for (entity, event_type), group_articles in groups.items():
            sorted_articles = sorted(
                group_articles,
                key=lambda a: self._parse_timestamp(a) or datetime.min.replace(tzinfo=timezone.utc),
            )
            for cluster in self._cluster_by_time(sorted_articles):
                events.append(self._build_event(entity, event_type, cluster))

        logger.info(
            "Event Fusion: %d article(s) with a detected event fused into %d event(s)",
            len(taggable), len(events),
        )
        return events
