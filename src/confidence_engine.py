"""
confidence_engine.py
-----------------------
Confidence Score module for MarketLens.

ARCHITECTURAL SHIFT (documented explicitly, since every prior module
tagged individual articles): this module operates at the ENTITY level
(a company or a standalone ticker like an ETF), not the article level.
It AGGREGATES every article that mentions a given entity and produces
ONE confidence assessment per entity — this is the direct
implementation of the project's central rule: "the recommendation must
NEVER be based on a single article." Confidence Score is where that
combination across multiple articles actually happens.

RESPONSIBILITY:
For each entity mentioned anywhere in the article batch, combine:

1. VOLUME — how many articles mention this entity, TIME-DECAY WEIGHTED
   (see below). More coverage is more evidence, up to a saturation
   point, but a two-month-old article contributes far less than one
   from this morning.
2. SOURCE DIVERSITY — how many DISTINCT sources contributed those
   articles. Five articles from one outlet is NOT the same evidence
   strength as five articles from five different outlets. Kept
   UNWEIGHTED by recency (see "TIME DECAY" section below for why).
3. SENTIMENT CONSISTENCY — do the articles agree on direction, TIME-
   DECAY WEIGHTED? If every article about an entity is positive (or
   every one negative), confidence is high; if they're split,
   confidence is low. A contradictory signal is one of the strongest
   reasons to distrust a conclusion, so this is the highest-weighted
   factor.
4. AVERAGE IMPACT — the TIME-DECAY WEIGHTED mean Impact Engine score
   across the entity's articles, so an entity backed by high-relevance,
   RECENT stories counts for more than one backed by old or marginal
   ones.

TIME DECAY (added once Database made multi-day accumulation possible):
Once articles persist across many days, treating a two-month-old
article identically to one from this morning is wrong for a system
meant to reflect CURRENT market sentiment. Each article's contribution
to volume, sentiment consistency, and average impact is weighted by
TimeDecayCalculator — its influence decays exponentially with age.

WHAT IS DELIBERATELY *NOT* TIME-WEIGHTED:
- `article_count` and the `sufficient_data` gate remain a RAW count.
  The "never a single article" rule needs to stay simple and literal —
  an old article still literally existed and still counts toward
  having "more than one source", even if its opinion carries reduced
  weight elsewhere. Weighting the gate itself would make an already
  subtle rule harder to reason about.
- `distinct_source_count` (source diversity) is also left unweighted:
  it measures breadth of independent coverage ever received, which is
  a meaningfully different question from "how fresh is the evidence".
"""

import logging
from typing import List, Dict, Any, Set, Optional
from datetime import datetime

from time_decay import TimeDecayCalculator

logger = logging.getLogger("marketlens.confidence_engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# Default per-category half-life override for the TimeDecayCalculator
# this module builds when no custom one is supplied. Crypto markets
# move noticeably faster than traditional stocks/BVB — a month-old
# crypto story arguably should fade faster than a month-old earnings
# story. Only applies to ConfidenceEngine's OWN default calculator;
# passing a custom `time_decay=` instance overrides this entirely.
_DEFAULT_CATEGORY_HALF_LIVES = {"crypto": 120.0}  # 5 days, vs. the 480h (20-day) default


class ConfidenceEngine:
    """
    Aggregates articles by entity (company or standalone ticker) and
    computes a confidence score for the combined signal about each,
    weighting each article's contribution by how recent it is.
    """

    # Weights, saturation points, and gate thresholds are now
    # constructor parameters (see __init__) rather than fixed class
    # constants — see __init__'s docstring for why (v1.3 tuning need).

    def __init__(
        self,
        time_decay: Optional[TimeDecayCalculator] = None,
        volume_weight: float = 0.25,
        source_diversity_weight: float = 0.25,
        consistency_weight: float = 0.30,
        impact_weight: float = 0.20,
        volume_saturation: int = 5,
        source_saturation: int = 4,
        min_articles_for_confidence: int = 2,
        max_single_article_confidence: float = 0.3,
    ):
        """
        Args:
            time_decay: the TimeDecayCalculator used to weight each
                article by recency. Defaults to a calculator with the
                standard half-life. Injectable so unit tests can use a
                custom half-life without touching the default
                configuration.
            volume_weight / source_diversity_weight / consistency_weight
                / impact_weight: weights for the 4 combined confidence
                factors (see class docstring). Default to the same
                values previously hardcoded as class constants — sum
                to 1.0 by construction; changing them is now a
                constructor call, not a code edit.
            volume_saturation: (time-decay-weighted) article count at
                which volume_score saturates to 1.0. Default 5.
            source_saturation: distinct-source count at which
                source_diversity_score saturates to 1.0. Default 4.
            min_articles_for_confidence: HARD RULE minimum (RAW,
                un-decayed) article count before "sufficient data" —
                the direct implementation of "never a single article".
                Default 2, the minimum that rule can mean.
            max_single_article_confidence: confidence ceiling applied
                when an entity has fewer than
                min_articles_for_confidence articles. Default 0.3.

        WHY CONFIGURABLE NOW (v1.3): these were originally fixed class
        constants. A real tuning need came up in practice — very few
        entities were clearing the BUY/SELL gates with limited data —
        and adjusting a hardcoded constant meant editing source code
        each time. Exposing them here means the same tuning is now a
        constructor call, with no behavior change for any caller that
        doesn't pass them (defaults are unchanged).
        """
        self.time_decay = time_decay if time_decay is not None else TimeDecayCalculator(
            category_half_lives=_DEFAULT_CATEGORY_HALF_LIVES
        )
        self.volume_weight = volume_weight
        self.source_diversity_weight = source_diversity_weight
        self.consistency_weight = consistency_weight
        self.impact_weight = impact_weight
        self.volume_saturation = volume_saturation
        self.source_saturation = source_saturation
        self.min_articles_for_confidence = min_articles_for_confidence
        self.max_single_article_confidence = max_single_article_confidence

    def _entity_keys_for_article(self, article: Dict[str, Any]) -> Set[str]:
        """
        Determine every entity (by canonical name) this article counts
        toward. Combines two sources that already share the same
        naming scheme by construction:

        - `companies_mentioned` (from Company Detector): canonical
          company names directly.
        - `tickers_mentioned` (from Ticker Detector): each ticker's
          `name` field, which — for stock/BVB/crypto tickers — was
          derived FROM company_registry.py's canonical_name, so a bare
          "TLV" mention resolves to the exact same entity key as a
          "Banca Transilvania" name mention, with no extra mapping code
          required.

        Unrecognized tickers (name=None) contribute no entity key.
        """
        keys: Set[str] = set()
        for company in article.get("companies_mentioned", []) or []:
            name = company.get("company")
            if name:
                keys.add(name)
        for ticker in article.get("tickers_mentioned", []) or []:
            name = ticker.get("name")
            if name:
                keys.add(name)
        return keys

    def aggregate_by_entity(self, articles: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Group articles by every entity they mention.

        Returns:
            A dict mapping entity name -> list of articles mentioning
            it. An article mentioning multiple entities appears under
            EACH of them — this fan-out is intentional, since each
            entity's confidence must be judged on its own merits.
        """
        entity_map: Dict[str, List[Dict[str, Any]]] = {}
        for article in articles:
            for key in self._entity_keys_for_article(article):
                entity_map.setdefault(key, []).append(article)
        return entity_map

    def _sentiment_breakdown(self, articles: List[Dict[str, Any]]) -> Dict[str, int]:
        """Count how many of the given articles fall into each sentiment label."""
        counts = {"positive": 0, "negative": 0, "neutral": 0}
        for article in articles:
            label = (article.get("sentiment") or {}).get("label", "neutral")
            counts[label] = counts.get(label, 0) + 1
        return counts

    def score_entity(
        self,
        entity_name: str,
        articles: List[Dict[str, Any]],
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Compute the full confidence assessment for one entity, given
        every article that mentions it.

        Args:
            reference_time: the "now" used for time-decay weighting.
                Defaults to the real current time; exposed here (and
                passed through from score_all_entities) so tests are
                fully deterministic, and so a past report could be
                regenerated "as of" a specific moment if ever needed.
        """
        article_count = len(articles)
        distinct_sources = len({a.get("source") for a in articles if a.get("source")})

        # One recency weight per article, computed once and reused
        # below for every weighted factor (volume, sentiment, impact) —
        # avoids recomputing the same weight three times per article.
        #
        # DATE SOURCE — bug found and fixed via real-world testing:
        # uses `published_at` (the article's REAL publication date)
        # when available, falling back to `collected_at` only if
        # `published_at` is missing. Using `collected_at` alone was
        # wrong: every article collected in a single run — including
        # ones pulled from weeks/months ago via Google News Historical
        # Backfill — shares essentially the same `collected_at` (the
        # moment of that run), so decay would treat 2-month-old
        # backfilled news as if it were published this very second,
        # defeating the entire purpose of both Time Decay and the
        # Historical Backfill module.
        weights = [
            self.time_decay.compute_weight(
                a.get("published_at") or a.get("collected_at"), reference_time, category=a.get("category")
            )
            for a in articles
        ]
        weighted_volume = sum(weights)

        sentiment_breakdown = self._sentiment_breakdown(articles)
        positive_weight = 0.0
        negative_weight = 0.0
        for article, weight in zip(articles, weights):
            label = (article.get("sentiment") or {}).get("label", "neutral")
            if label == "positive":
                positive_weight += weight
            elif label == "negative":
                negative_weight += weight

        directional_weight_total = positive_weight + negative_weight
        if directional_weight_total == 0:
            # No article expressed a clear direction at all (all neutral).
            sentiment_consistency = 0.0
            dominant_sentiment = "neutral"
        else:
            majority_weight = max(positive_weight, negative_weight)
            sentiment_consistency = majority_weight / directional_weight_total
            if positive_weight > negative_weight:
                dominant_sentiment = "positive"
            elif negative_weight > positive_weight:
                dominant_sentiment = "negative"
            else:
                dominant_sentiment = "mixed"  # exact tie, e.g. equal weighted support each way

        weighted_impact_sum = sum(
            (a.get("impact") or {}).get("score", 0.0) * weight
            for a, weight in zip(articles, weights)
        )
        average_impact = weighted_impact_sum / weighted_volume if weighted_volume > 0 else 0.0

        volume_score = min(1.0, weighted_volume / self.volume_saturation)
        source_diversity_score = min(1.0, distinct_sources / self.source_saturation)

        raw_score = (
            self.volume_weight * volume_score
            + self.source_diversity_weight * source_diversity_score
            + self.consistency_weight * sentiment_consistency
            + self.impact_weight * average_impact
        )
        confidence_score = round(min(1.0, raw_score), 3)

        # RAW (unweighted) count — see module docstring for why this
        # gate must not be time-decay weighted.
        sufficient_data = article_count >= self.min_articles_for_confidence
        if not sufficient_data:
            confidence_score = min(confidence_score, self.max_single_article_confidence)
            reason = (
                f"Only {article_count} article(s) found — MarketLens requires at least "
                f"{self.min_articles_for_confidence} independent articles before a "
                "recommendation can be issued for this entity"
            )
        else:
            reason = f"Based on {article_count} articles from {distinct_sources} distinct source(s)"

        return {
            "entity": entity_name,
            "article_count": article_count,
            "distinct_source_count": distinct_sources,
            "sentiment_breakdown": sentiment_breakdown,
            "dominant_sentiment": dominant_sentiment,
            "sentiment_consistency": round(sentiment_consistency, 3),
            "average_impact": round(average_impact, 3),
            "average_recency_weight": round(weighted_volume / article_count, 3) if article_count else 0.0,
            # Exposed for transparency — e.g. a Dashboard breakdown
            # showing exactly how the confidence score was built, not
            # just the final number.
            "volume_score": round(volume_score, 3),
            "source_diversity_score": round(source_diversity_score, 3),
            "confidence_score": confidence_score,
            "sufficient_data": sufficient_data,
            "reason": reason,
        }

    def score_all_entities(
        self,
        articles: List[Dict[str, Any]],
        reference_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Aggregate the full article batch by entity and score every
        entity found.

        Args:
            reference_time: passed through to score_entity for
                deterministic time-decay weighting; defaults to the
                real current time when omitted.

        Returns:
            A list of entity confidence dicts, sorted by
            `confidence_score` descending (highest-confidence entities
            first — the natural reading order for a report).
        """
        entity_map = self.aggregate_by_entity(articles)
        results = [
            self.score_entity(name, entity_articles, reference_time=reference_time)
            for name, entity_articles in entity_map.items()
        ]
        results.sort(key=lambda r: r["confidence_score"], reverse=True)

        sufficient = sum(1 for r in results if r["sufficient_data"])
        logger.info(
            "Confidence Engine: %d entities found, %d with sufficient data (>= %d articles)",
            len(results), sufficient, self.min_articles_for_confidence,
        )
        return results
