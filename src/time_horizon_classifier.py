"""
time_horizon_classifier.py
------------------------------
Time Horizon Classifier module for MarketLens.

RESPONSIBILITY:
Classify each entity as better suited for a SHORT-TERM or LONG-TERM
view — or "mixed"/"unknown" when the evidence doesn't clearly support
either — based ONLY on how its supporting articles are distributed
over TIME. This is deliberately independent of sentiment or price: it
answers "does this look like one event, or an unfolding trend?", not
"is the news good or bad". It directly uses the real historical spread
that Database + Google News Historical Backfill make available.

RATIONALE:
- A SHORT-TERM signal looks like a single triggering EVENT: articles
  clustered within a tight recent window (e.g. an earnings beat, a
  hack, a lawsuit). Markets typically react and settle within days, so
  this suits a short holding-period view.
- A LONG-TERM signal looks like a sustained TREND: coverage spread
  across many distinct days over a longer span (e.g. weeks of
  consistently framed articles about an expansion plan) — an unfolding
  story relevant to a longer holding period, not a single reactive
  trade.

DESIGN DECISION — reuses ConfidenceEngine's entity grouping:
This module does NOT re-implement "group articles by entity" — it
consumes the exact same entity -> articles mapping produced by
ConfidenceEngine.aggregate_by_entity(), so there is only ever one
place in the codebase that defines what "this article belongs to this
entity" means.
"""

import logging
from datetime import datetime, date as date_type
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.time_horizon_classifier")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class TimeHorizonClassifier:
    """
    Classifies entities by the temporal shape of their supporting
    article coverage: a tight recent cluster (short-term event) vs.
    a spread-out trend (long-term).
    """

    # Span (latest - earliest article date, inclusive, in days) at or
    # below which coverage is treated as ONE tightly-clustered EVENT
    # rather than a sustained trend.
    _SHORT_TERM_MAX_SPAN_DAYS = 3

    # Minimum number of DISTINCT calendar days with at least one
    # article — required (alongside a long-enough span AND enough
    # density, below) to call something a sustained TREND.
    _MIN_DISTINCT_DAYS_FOR_TREND = 4

    # Minimum fraction of days within the span that must actually have
    # coverage (distinct_days / span_days) to count as a genuine
    # sustained trend. WHY THIS EXISTS — a real miscalibration found in
    # production: once Google News Historical Backfill covers a wide
    # ~60-day window for every tracked company, almost any company ends
    # up with a HANDFUL of scattered, incidental mentions somewhere in
    # that window (e.g. 4 stray days out of 60), which used to clear
    # the distinct-days bar alone and get misclassified as "long-term"
    # even though the coverage is sparse, not sustained. Requiring a
    # minimum density (at least this fraction of the span actually
    # covered) distinguishes a real, ongoing narrative from incidental,
    # spread-out mentions that don't reflect sustained attention.
    _MIN_DENSITY_FOR_TREND = 0.15

    def _parse_date(self, value: Any) -> Optional[date_type]:
        """Parse a `collected_at` timestamp into a plain date, tolerating missing/malformed values."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def classify_entity(self, entity_name: str, articles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Classify one entity's time horizon from its supporting articles.

        DATE SOURCE — bug found and fixed via real-world testing:
        Uses `published_at` (the article's REAL publication date) when
        available, falling back to `collected_at` (when OUR system
        fetched it) only if `published_at` is missing. An earlier
        version used `collected_at` exclusively — which is wrong,
        because every article collected in a single run (including
        ones pulled from weeks/months ago via Google News Historical
        Backfill) shares essentially the same `collected_at` (the
        moment of that run), making every entity look like a tight,
        single-day event regardless of when its news actually happened.

        Returns:
            {"entity", "time_horizon" ("short-term"/"long-term"/"mixed"/
            "unknown"), "span_days", "distinct_days", "reason"}
        """
        dates = [
            d for d in (self._parse_date(a.get("published_at") or a.get("collected_at")) for a in articles)
            if d is not None
        ]

        if not dates:
            return {
                "entity": entity_name,
                "time_horizon": "unknown",
                "span_days": None,
                "distinct_days": None,
                "coverage_density": None,
                "reason": "No valid article dates available to classify time horizon",
            }

        distinct_days = len(set(dates))
        span_days = (max(dates) - min(dates)).days + 1
        coverage_density = round(distinct_days / span_days, 3)

        if span_days <= self._SHORT_TERM_MAX_SPAN_DAYS:
            horizon = "short-term"
            reason = (
                f"All coverage falls within a {span_days}-day window — consistent with a "
                "single triggering event rather than a sustained trend"
            )
        elif distinct_days >= self._MIN_DISTINCT_DAYS_FOR_TREND and coverage_density >= self._MIN_DENSITY_FOR_TREND:
            horizon = "long-term"
            reason = (
                f"Coverage spans {span_days} days across {distinct_days} distinct days "
                f"({round(coverage_density * 100)}% of the span) — consistent with a sustained "
                "trend rather than one isolated event"
            )
        else:
            horizon = "mixed"
            reason = (
                f"Coverage spans {span_days} days but only on {distinct_days} distinct day(s) "
                f"({round(coverage_density * 100)}% of the span) — too sparse to call a sustained "
                "trend, but not a tight single event either"
            )

        return {
            "entity": entity_name,
            "time_horizon": horizon,
            "span_days": span_days,
            "distinct_days": distinct_days,
            "coverage_density": coverage_density,
            "reason": reason,
        }

    def classify_batch(self, entity_articles_map: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        """
        Classify every entity in an entity -> articles mapping (the
        exact shape produced by ConfidenceEngine.aggregate_by_entity()).

        Returns:
            A dict mapping entity name -> its time-horizon classification.
        """
        results = {
            entity: self.classify_entity(entity, articles)
            for entity, articles in entity_articles_map.items()
        }

        horizon_counts: Dict[str, int] = {}
        for r in results.values():
            horizon_counts[r["time_horizon"]] = horizon_counts.get(r["time_horizon"], 0) + 1
        logger.info("Time Horizon Classifier: %s (of %d entities)", horizon_counts, len(results))

        return results
