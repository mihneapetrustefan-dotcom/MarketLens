"""
upgrade_downgrade_tracker.py
-------------------------------
Upgrade/Downgrade Tracker module for MarketLens.

RESPONSIBILITY:
Compare each entity's CURRENT recommendation against its most recent
PREVIOUSLY LOGGED recommendation (from RecommendationLog), and flag
any change in direction — the same signal professional analysts
highlight ("upgraded to Buy", "downgraded to Sell"). All the data for
this already exists once RecommendationLog is in place; this module
is purely the comparison logic.

RANKING: SELL < HOLD < BUY. Moving to a higher rank is an "upgrade",
to a lower rank a "downgrade", same rank "unchanged". An entity with
NO prior logged recommendation is "new" — nothing to compare against.
"""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.upgrade_downgrade_tracker")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

_RANK = {"SELL": 0, "HOLD": 1, "BUY": 2}


class UpgradeDowngradeTracker:
    """
    Detects recommendation direction changes by comparing current
    results against the most recent prior entry in RecommendationLog.
    """

    def _previous_recommendation_for(
        self, entity: str, logged_recommendations: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Find the most recent PRIOR logged recommendation for one entity, by generated_at."""
        matches = [r for r in logged_recommendations if r["entity"] == entity]
        if not matches:
            return None
        return max(matches, key=lambda r: r["generated_at"])

    def compare_entity(
        self, current_recommendation: Dict[str, Any], logged_recommendations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Compare one entity's current recommendation against its most
        recent prior logged one.

        Returns:
            {"entity", "current", "previous", "change" ("new"/
            "upgrade"/"downgrade"/"unchanged"), "reason"}
        """
        entity = current_recommendation["entity"]
        previous = self._previous_recommendation_for(entity, logged_recommendations)

        if previous is None:
            return {
                "entity": entity,
                "current": current_recommendation["recommendation"],
                "previous": None,
                "change": "new",
                "reason": "No prior recommendation on record for this entity",
            }

        current_rank = _RANK.get(current_recommendation["recommendation"], 1)
        previous_rank = _RANK.get(previous["recommendation"], 1)

        if current_rank > previous_rank:
            change = "upgrade"
        elif current_rank < previous_rank:
            change = "downgrade"
        else:
            change = "unchanged"

        return {
            "entity": entity,
            "current": current_recommendation["recommendation"],
            "previous": previous["recommendation"],
            "change": change,
            "reason": f"{previous['recommendation']} -> {current_recommendation['recommendation']}",
        }

    def compare_batch(
        self, current_recommendations: List[Dict[str, Any]], logged_recommendations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Compare an entire batch of current recommendations against the
        recommendation log.

        Returns:
            A list of comparison dicts, one per current recommendation.
        """
        results = [self.compare_entity(r, logged_recommendations) for r in current_recommendations]

        change_counts: Dict[str, int] = {}
        for r in results:
            change_counts[r["change"]] = change_counts.get(r["change"], 0) + 1
        logger.info("Upgrade/Downgrade Tracker: %s (of %d entities)", change_counts, len(results))

        return results
