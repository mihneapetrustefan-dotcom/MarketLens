"""
recommendation_engine.py
----------------------------
Recommendation Engine module for MarketLens.

RESPONSIBILITY:
Convert an entity's Confidence Score assessment into a final
recommendation — BUY / SELL / HOLD, or the "strong" tier of either —
with a full, human-readable explanation. This is the last step before
a result becomes user-facing: it applies the gates that decide whether
a signal is trustworthy enough to act on at all.

CHANGE LOG (v1.3) — STRONG_BUY / STRONG_SELL:
Previously only 3 outcomes existed (BUY/SELL/HOLD). A BUY backed by
overwhelming, near-unanimous evidence looked identical to a BUY that
barely cleared the minimum bar — both were just "BUY". STRONG_BUY and
STRONG_SELL are an ADDITIONAL, stricter tier layered on top of the
existing BUY/SELL gates (never a replacement for them): a
recommendation only ever becomes "strong" after it has already passed
every existing check, and only when confidence AND sentiment
consistency both clear a second, higher bar. HOLD is unaffected —
there is no "strong HOLD".
"""

from typing import Dict, Any, List, Optional


class RecommendationEngine:
    """
    Converts an entity's Confidence Score assessment into a final
    recommendation with a full explanation.
    """

    def __init__(
        self,
        min_confidence_for_action: float = 0.5,
        min_impact_for_action: float = 0.3,
        strong_confidence_threshold: float = 0.85,
        strong_consistency_threshold: float = 0.85,
    ):
        """
        Args:
            min_confidence_for_action: below this confidence score, the
                entity's signal is not treated as strong/consistent
                enough to act on directionally, even with enough
                articles to pass the sufficient-data gate. Default 0.5.
            min_impact_for_action: below this average impact score, the
                underlying news isn't significant enough to justify a
                directional call. Default 0.3.
            strong_confidence_threshold: minimum confidence score
                required, ON TOP of already qualifying for BUY/SELL,
                for the recommendation to be upgraded to STRONG_BUY /
                STRONG_SELL. Default 0.85.
            strong_consistency_threshold: minimum sentiment consistency
                (from Confidence Score's own breakdown — how unanimous
                the coverage is, not just how much of it there is)
                required for the same upgrade. Default 0.85. Requiring
                BOTH confidence and consistency to be high means a
                "strong" call reflects overwhelming, largely
                one-directional evidence — not just a lot of it.
        """
        self.min_confidence_for_action = min_confidence_for_action
        self.min_impact_for_action = min_impact_for_action
        self.strong_confidence_threshold = strong_confidence_threshold
        self.strong_consistency_threshold = strong_consistency_threshold

    def _maybe_upgrade_to_strong(self, recommendation: str, entity_confidence: Dict[str, Any]) -> str:
        """
        Upgrade a BUY/SELL to STRONG_BUY/STRONG_SELL if confidence AND
        sentiment consistency both clear the (higher) strong-tier
        bars. Never touches HOLD, and never applies to a recommendation
        that hasn't already independently qualified as BUY/SELL.
        """
        if recommendation not in ("BUY", "SELL"):
            return recommendation

        confidence_score = entity_confidence.get("confidence_score") or 0.0
        consistency = entity_confidence.get("sentiment_consistency") or 0.0

        if confidence_score >= self.strong_confidence_threshold and consistency >= self.strong_consistency_threshold:
            return f"STRONG_{recommendation}"
        return recommendation

    def recommend_entity(self, entity_confidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Produce a recommendation for one entity from its Confidence
        Score assessment (the output of
        ConfidenceEngine.score_entity()).

        Returns:
            {"entity", "recommendation" (STRONG_BUY/BUY/HOLD/SELL/
            STRONG_SELL), "confidence_score", "explanation"}
        """
        entity = entity_confidence["entity"]
        confidence_score = entity_confidence.get("confidence_score")
        sufficient_data = entity_confidence.get("sufficient_data", False)
        dominant_sentiment = entity_confidence.get("dominant_sentiment")
        average_impact = entity_confidence.get("average_impact") or 0.0

        explanation_parts: List[str] = []

        if not sufficient_data:
            recommendation = "HOLD"
            explanation_parts.append(
                f"Date insuficiente pentru o decizie ({entity_confidence.get('article_count', 0)} articol(e) — "
                f"sub pragul minim necesar)."
            )
        elif (confidence_score or 0.0) < self.min_confidence_for_action:
            recommendation = "HOLD"
            explanation_parts.append(
                f"Încredere {confidence_score} sub pragul de {self.min_confidence_for_action} necesar pentru o acțiune."
            )
        elif average_impact < self.min_impact_for_action:
            recommendation = "HOLD"
            explanation_parts.append(
                f"Impact mediu {average_impact} sub pragul de {self.min_impact_for_action} — "
                f"știrile nu par suficient de semnificative pentru o acțiune."
            )
        elif dominant_sentiment == "positive":
            recommendation = "BUY"
            explanation_parts.append(f"Sentiment predominant pozitiv, încredere {confidence_score}.")
        elif dominant_sentiment == "negative":
            recommendation = "SELL"
            explanation_parts.append(f"Sentiment predominant negativ, încredere {confidence_score}.")
        else:
            recommendation = "HOLD"
            explanation_parts.append("Sentiment neutru sau amestecat — fără o direcție clară.")

        recommendation = self._maybe_upgrade_to_strong(recommendation, entity_confidence)
        if recommendation.startswith("STRONG_"):
            explanation_parts.append(
                f"Semnal puternic: încredere ≥ {self.strong_confidence_threshold} și "
                f"consistență ≥ {self.strong_consistency_threshold} — dovezi aproape unanime."
            )

        return {
            "entity": entity,
            "recommendation": recommendation,
            "confidence_score": confidence_score,
            "explanation": " ".join(explanation_parts),
        }

    def recommend_all(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Produce recommendations for a whole list of entity Confidence Score assessments."""
        recommendations = [self.recommend_entity(e) for e in entities]

        counts: Dict[str, int] = {}
        for r in recommendations:
            counts[r["recommendation"]] = counts.get(r["recommendation"], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        import logging
        logging.getLogger("marketlens.recommendation_engine").info(
            "Recommendation Engine: %s (of %d entities)", summary, len(recommendations)
        )

        return recommendations
