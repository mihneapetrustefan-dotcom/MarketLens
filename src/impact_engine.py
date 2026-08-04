"""
impact_engine.py
-------------------
Impact Engine module for MarketLens.

RESPONSIBILITY:
Determine how IMPORTANT/RELEVANT each article is for markets, by
combining three signals already produced by earlier modules:

1. RELEVANCE GATE (mandatory): an article must mention at least one
   known company, ticker, OR sector before it can have ANY impact
   score at all. WHY THIS IS A HARD GATE, not just a weighted factor:
   a real production case surfaced exactly this problem — an article
   about a castle renovation scored "positive" sentiment (the word
   "redresare"/recovery appeared, referring to EU funding) despite
   having zero market relevance. Sentiment strength or source
   corroboration on an irrelevant article must never produce a
   non-zero impact score; the gate enforces that unconditionally,
   before any weighting is applied.

2. CORROBORATION (from Duplicate Detector's `duplicate_group_size`):
   more independent sources reporting the same story = higher
   confidence this is a real, significant event, not noise from one
   outlet.

3. SENTIMENT STRENGTH (from Sentiment Engine's `score` + `confidence`):
   a strong sentiment signal, and one backed by several matched
   keywords (not just one), is more market-moving than a faint or
   thinly-supported one.

OUTPUT SHAPE per article, under the `impact` key:
    {
        "score": float in [0.0, 1.0],
        "level": "none" | "low" | "medium" | "high",
        "relevance_score": float in [0.0, 1.0],
        "corroboration_score": float in [0.0, 1.0],
        "sentiment_strength": float in [0.0, 1.0],
        "reason": str  # human-readable explanation, esp. for "none"
    }
"""

import logging
from typing import List, Dict, Any

logger = logging.getLogger("marketlens.impact_engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class ImpactEngine:
    """
    Scores each article's market impact by combining a mandatory
    relevance gate with corroboration and sentiment-strength signals.
    """

    # Weights for combining the three (post-gate) signals into one
    # score. Relevance gets the largest weight because it reflects HOW
    # MANY distinct market entities/sectors are involved — an article
    # touching several known names/sectors is inherently more
    # significant than one touching a single one, independent of tone.
    # Sum to 1.0 by construction, documented explicitly so future tuning
    # is a one-line change with a clear contract to preserve.
    _RELEVANCE_WEIGHT = 0.4
    _CORROBORATION_WEIGHT = 0.3
    _SENTIMENT_WEIGHT = 0.3

    # Number of distinct relevance signals (companies + tickers +
    # sectors combined) at which relevance_score saturates to 1.0.
    # WHY 3: an article naming 3+ distinct known entities/sectors is
    # already clearly a significant, multi-faceted story; requiring
    # more would just flatten most real articles to the same low score.
    _RELEVANCE_SATURATION_COUNT = 3

    # Number of ADDITIONAL independent sources (beyond the first) at
    # which corroboration_score saturates to 1.0. WHY 4: a story
    # confirmed by 5 total independent outlets (1 + 4 additional) is
    # about as corroborated as financial news realistically gets.
    _CORROBORATION_SATURATION_COUNT = 4

    def _compute_relevance(self, article: Dict[str, Any]) -> float:
        """
        Count distinct relevance signals (companies + tickers +
        sectors) and normalize to [0.0, 1.0].
        """
        num_companies = len(article.get("companies_mentioned", []) or [])
        num_tickers = len(article.get("tickers_mentioned", []) or [])
        num_sectors = len(article.get("sectors", []) or [])
        total_signals = num_companies + num_tickers + num_sectors
        return min(1.0, total_signals / self._RELEVANCE_SATURATION_COUNT)

    def _compute_corroboration(self, article: Dict[str, Any]) -> float:
        """
        Convert `duplicate_group_size` into a normalized [0.0, 1.0]
        corroboration score. Defaults to a group size of 1 (a single,
        uncorroborated source) if the field is missing, which yields a
        score of 0.0 — no bonus without confirmed independent sourcing.
        """
        group_size = article.get("duplicate_group_size", 1) or 1
        additional_sources = max(0, group_size - 1)
        return min(1.0, additional_sources / self._CORROBORATION_SATURATION_COUNT)

    def _compute_sentiment_strength(self, article: Dict[str, Any]) -> float:
        """
        Combine sentiment score magnitude with its confidence into one
        [0.0, 1.0] strength value. A strong score with LOW confidence
        (based on very few matched words) is deliberately dampened —
        multiplying the two means both must be high for the strength
        to be high.
        """
        sentiment = article.get("sentiment") or {}
        score_magnitude = abs(sentiment.get("score", 0.0))
        confidence = sentiment.get("confidence", 0.0)
        return score_magnitude * confidence

    def _classify_level(self, score: float) -> str:
        """
        Map a numeric impact score to a human-readable level. "none" is
        reserved exclusively for the relevance-gate-failed case (see
        score_article) — it is never reached through this method, which
        only ever sees scores from articles that passed the gate.
        """
        if score >= 0.66:
            return "high"
        if score >= 0.33:
            return "medium"
        return "low"

    def score_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute the impact score for one article and return a NEW
        article dict tagged with `impact`.

        Follows the same copy-don't-mutate discipline as every other
        module in this pipeline.
        """
        relevance_score = self._compute_relevance(article)

        tagged = dict(article)

        if relevance_score == 0.0:
            # HARD GATE: no known company, ticker, or sector at all ->
            # impact is unconditionally zero, regardless of sentiment
            # or corroboration. See module docstring for why this must
            # be a gate, not just a low-weighted factor.
            tagged["impact"] = {
                "score": 0.0,
                "level": "none",
                "relevance_score": 0.0,
                "corroboration_score": 0.0,
                "sentiment_strength": 0.0,
                "reason": "No known company, ticker, or sector detected in this article",
            }
            return tagged

        corroboration_score = self._compute_corroboration(article)
        sentiment_strength = self._compute_sentiment_strength(article)

        raw_score = (
            self._RELEVANCE_WEIGHT * relevance_score
            + self._CORROBORATION_WEIGHT * corroboration_score
            + self._SENTIMENT_WEIGHT * sentiment_strength
        )
        score = round(min(1.0, raw_score), 3)

        tagged["impact"] = {
            "score": score,
            "level": self._classify_level(score),
            "relevance_score": round(relevance_score, 3),
            "corroboration_score": round(corroboration_score, 3),
            "sentiment_strength": round(sentiment_strength, 3),
            "reason": "Scored from relevance, source corroboration, and sentiment strength",
        }
        return tagged

    def score_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run impact scoring over an entire batch of articles.

        Returns:
            A new list, same order, every article tagged with
            `impact`.
        """
        tagged_articles = [self.score_article(article) for article in articles]

        level_counts: Dict[str, int] = {"none": 0, "low": 0, "medium": 0, "high": 0}
        for article in tagged_articles:
            level_counts[article["impact"]["level"]] += 1

        logger.info(
            "Impact Engine: %d none, %d low, %d medium, %d high (of %d total)",
            level_counts["none"], level_counts["low"], level_counts["medium"],
            level_counts["high"], len(tagged_articles),
        )
        return tagged_articles
