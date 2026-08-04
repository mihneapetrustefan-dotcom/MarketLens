"""
sector_aggregator.py
------------------------
Sector Aggregator module for MarketLens.

RESPONSIBILITY:
Aggregate article-level sentiment/impact up to the SECTOR level (not
just per-entity), giving a macro view alongside individual company
recommendations — e.g. "the Energy sector shows sustained negative
sentiment this week", independent of any single company's rating.

DESIGN DECISION — mirrors ConfidenceEngine's shape, at sector
granularity: groups articles by the `sectors` field already produced
by Sector Detector, then computes article count, distinct source
count, dominant sentiment, sentiment consistency, and average impact —
the same kind of computation ConfidenceEngine already does per ENTITY,
applied here per SECTOR instead. This module does NOT compute a
confidence score or a BUY/SELL-style recommendation — sectors aren't
tradable entities; it's purely a descriptive macro view.
"""

import logging
from typing import List, Dict, Any

logger = logging.getLogger("marketlens.sector_aggregator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class SectorAggregator:
    """
    Aggregates articles by sector and computes descriptive sentiment/
    impact statistics per sector.
    """

    def aggregate_by_sector(self, articles: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Group articles by every sector they were classified into (an
        article touching multiple sectors appears under each).
        """
        sector_map: Dict[str, List[Dict[str, Any]]] = {}
        for article in articles:
            for sector_entry in (article.get("sectors") or []):
                sector_map.setdefault(sector_entry["sector"], []).append(article)
        return sector_map

    def _sentiment_breakdown(self, articles: List[Dict[str, Any]]) -> Dict[str, int]:
        counts = {"positive": 0, "negative": 0, "neutral": 0}
        for article in articles:
            label = (article.get("sentiment") or {}).get("label", "neutral")
            counts[label] = counts.get(label, 0) + 1
        return counts

    def score_sector(self, sector_name: str, articles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute descriptive sentiment/impact statistics for one sector's articles."""
        article_count = len(articles)
        distinct_sources = len({a.get("source") for a in articles if a.get("source")})

        sentiment_breakdown = self._sentiment_breakdown(articles)
        positive = sentiment_breakdown["positive"]
        negative = sentiment_breakdown["negative"]
        directional_total = positive + negative

        if directional_total == 0:
            dominant_sentiment = "neutral"
            sentiment_consistency = 0.0
        else:
            majority = max(positive, negative)
            sentiment_consistency = round(majority / directional_total, 3)
            if positive > negative:
                dominant_sentiment = "positive"
            elif negative > positive:
                dominant_sentiment = "negative"
            else:
                dominant_sentiment = "mixed"

        impact_scores = [(a.get("impact") or {}).get("score", 0.0) for a in articles]
        average_impact = round(sum(impact_scores) / len(impact_scores), 3) if impact_scores else 0.0

        return {
            "sector": sector_name,
            "article_count": article_count,
            "distinct_source_count": distinct_sources,
            "sentiment_breakdown": sentiment_breakdown,
            "dominant_sentiment": dominant_sentiment,
            "sentiment_consistency": sentiment_consistency,
            "average_impact": average_impact,
        }

    def score_all_sectors(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Aggregate the full article batch by sector and score every
        sector found.

        Returns:
            A list of sector statistics dicts, sorted by article count
            descending (most-covered sector first).
        """
        sector_map = self.aggregate_by_sector(articles)
        results = [self.score_sector(name, arts) for name, arts in sector_map.items()]
        results.sort(key=lambda r: r["article_count"], reverse=True)

        logger.info("Sector Aggregator: %d sector(s) scored from %d articles", len(results), len(articles))
        return results
