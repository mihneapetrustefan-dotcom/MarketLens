"""
recommendation_engine.py
---------------------------
Recommendation Engine module for MarketLens — the FINAL module in the
pipeline.

RESPONSIBILITY:
For each entity already scored by Confidence Score, produce a final
BUY / SELL / HOLD recommendation, with a detailed, human-readable
explanation. This module makes NO independent measurements of its own
— it is a decision layer on top of everything already computed
(article volume, source diversity, sentiment consistency, impact),
combining them into one final call per entity.

DECISION RULES (in order — each is a gate the entity must pass before
the next rule is even considered):

1. SUFFICIENT DATA GATE (inherited from Confidence Score): if
   `sufficient_data` is False, the recommendation is unconditionally
   HOLD. This is the module's direct enforcement of the project's
   central rule — a recommendation is NEVER BUY or SELL based on a
   single article, no matter how positive or negative it reads.

2. MINIMUM CONFIDENCE GATE: even with 2+ articles, if the overall
   confidence score is below `_MIN_CONFIDENCE_FOR_ACTION`, the signal
   is too weak or too inconsistent (contradictory sentiment, single
   source, etc.) to act on -> HOLD.

3. MINIMUM IMPACT GATE: even with strong, consistent sentiment, if the
   average market impact is below `_MIN_IMPACT_FOR_ACTION`, the
   underlying news isn't significant enough to justify a directional
   call (e.g. routine, low-relevance coverage) -> HOLD.

4. DIRECTIONAL CALL: only once all three gates are passed does the
   dominant sentiment decide BUY (positive) vs SELL (negative). A
   "neutral" or "mixed" dominant sentiment still resolves to HOLD —
   there is no directional consensus to act on.

Every recommendation includes a full explanation string citing the
actual numbers behind the decision (article count, source count,
confidence, impact) — never just a bare label.
"""

import logging
from typing import List, Dict, Any

logger = logging.getLogger("marketlens.recommendation_engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class RecommendationEngine:
    """
    Converts an entity's Confidence Score assessment into a final
    BUY / SELL / HOLD recommendation with a full explanation.
    """

    def __init__(
        self,
        min_confidence_for_action: float = 0.5,
        min_impact_for_action: float = 0.3,
    ):
        """
        Args:
            min_confidence_for_action: below this confidence score, the
                entity's signal is not treated as strong/consistent
                enough to act on directionally, even with enough
                articles to pass the sufficient-data gate. Default 0.5.
            min_impact_for_action: below this average impact score, the
                underlying news isn't significant enough to justify a
                directional call — this is what prevents a string of
                low-relevance routine articles with accidentally
                consistent sentiment from producing a BUY/SELL.
                Default 0.3.

        WHY CONFIGURABLE NOW (v1.3): previously fixed class constants;
        made constructor parameters for the same reason as
        ConfidenceEngine's weights — real-world tuning (too few
        entities were clearing these gates with limited data) should be
        a constructor call, not a source-code edit. Defaults are
        unchanged from the original hardcoded values.
        """
        self.min_confidence_for_action = min_confidence_for_action
        self.min_impact_for_action = min_impact_for_action

    def _build_explanation(self, ec: Dict[str, Any], recommendation: str, gate_reason: str = "") -> str:
        """
        Compose a full, human-readable explanation for one
        recommendation, citing the actual figures behind the decision.
        """
        entity = ec["entity"]
        article_count = ec["article_count"]
        source_count = ec["distinct_source_count"]
        confidence = ec["confidence_score"]
        consistency_pct = round(ec["sentiment_consistency"] * 100)
        impact = ec["average_impact"]
        sentiment = ec["dominant_sentiment"]

        base = (
            f"{recommendation} recommendation for {entity}, based on {article_count} "
            f"article(s) from {source_count} distinct source(s). "
            f"Dominant sentiment: {sentiment} ({consistency_pct}% agreement among "
            f"directional articles). Average market impact: {impact}. "
            f"Overall confidence: {confidence}/1.0."
        )
        if gate_reason:
            base += f" {gate_reason}"
        return base

    def recommend_entity(self, entity_confidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Produce the final recommendation for one entity's Confidence
        Score assessment (as returned by ConfidenceEngine.score_entity
        or score_all_entities).

        Returns:
            A dict with the recommendation, its explanation, and the
            underlying figures it was based on — enough for a report to
            be generated without re-fetching anything.
        """
        entity = entity_confidence["entity"]

        # --- Gate 1: sufficient data (never a single article) ---
        if not entity_confidence["sufficient_data"]:
            recommendation = "HOLD"
            explanation = self._build_explanation(
                entity_confidence, recommendation,
                gate_reason=(
                    "Reason: insufficient data — MarketLens never issues a BUY or SELL "
                    "call based on a single article, regardless of its tone."
                ),
            )

        # --- Gate 2: minimum confidence ---
        elif entity_confidence["confidence_score"] < self.min_confidence_for_action:
            recommendation = "HOLD"
            explanation = self._build_explanation(
                entity_confidence, recommendation,
                gate_reason=(
                    f"Reason: confidence score is below the "
                    f"{self.min_confidence_for_action} threshold required for a "
                    "directional call — the signal is too weak or inconsistent to act on."
                ),
            )

        # --- Gate 3: minimum impact ---
        elif entity_confidence["average_impact"] < self.min_impact_for_action:
            recommendation = "HOLD"
            explanation = self._build_explanation(
                entity_confidence, recommendation,
                gate_reason=(
                    f"Reason: average impact is below the "
                    f"{self.min_impact_for_action} threshold — the confirmed news "
                    "isn't significant enough to justify a directional call."
                ),
            )

        # --- Directional call: all gates passed ---
        else:
            dominant = entity_confidence["dominant_sentiment"]
            if dominant == "positive":
                recommendation = "BUY"
                explanation = self._build_explanation(entity_confidence, recommendation)
            elif dominant == "negative":
                recommendation = "SELL"
                explanation = self._build_explanation(entity_confidence, recommendation)
            else:
                # "neutral" or "mixed" — sufficient data and impact,
                # but no clear directional consensus to act on.
                recommendation = "HOLD"
                explanation = self._build_explanation(
                    entity_confidence, recommendation,
                    gate_reason=(
                        "Reason: sentiment across articles shows no clear positive or "
                        "negative consensus, despite sufficient data and impact."
                    ),
                )

        return {
            "entity": entity,
            "recommendation": recommendation,
            "explanation": explanation,
            "confidence_score": entity_confidence["confidence_score"],
            "sufficient_data": entity_confidence["sufficient_data"],
            "article_count": entity_confidence["article_count"],
            "distinct_source_count": entity_confidence["distinct_source_count"],
            "dominant_sentiment": entity_confidence["dominant_sentiment"],
            "average_impact": entity_confidence["average_impact"],
            # Propagated for transparency (e.g. a Dashboard breakdown of
            # exactly what built the confidence score). .get() with a
            # default keeps this backward-compatible with any
            # entity_confidence dict that predates these fields.
            "volume_score": entity_confidence.get("volume_score", 0.0),
            "source_diversity_score": entity_confidence.get("source_diversity_score", 0.0),
            "sentiment_consistency": entity_confidence.get("sentiment_consistency", 0.0),
        }

    def recommend_all(self, entity_confidence_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Produce recommendations for an entire list of entity confidence
        assessments (the output of ConfidenceEngine.score_all_entities).

        Returns:
            A list of recommendation dicts, one per entity, in the same
            order as the input.
        """
        results = [self.recommend_entity(ec) for ec in entity_confidence_list]

        counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for r in results:
            counts[r["recommendation"]] += 1

        logger.info(
            "Recommendation Engine: %d BUY, %d SELL, %d HOLD (of %d entities)",
            counts["BUY"], counts["SELL"], counts["HOLD"], len(results),
        )
        return results
