"""
daily_summary.py
-------------------
Daily Summary module for MarketLens.

RESPONSIBILITY:
Turn already-computed, structured results (recommendations, sector
statistics, upgrade/downgrade changes) into a short, readable
natural-language paragraph — 2-4 sentences a person can scan in
seconds, instead of reading through tables.

DESIGN DECISION — template-based, NOT an external AI call:
Every sentence is built from a fixed template filled in with real
numbers already computed elsewhere in the pipeline. This keeps the
summary fully deterministic, reproducible, and free of any risk of an
AI model inventing a claim not actually supported by the data — a
critical property for anything resembling investment content.
"""

from typing import List, Dict, Any


class DailySummaryGenerator:
    """
    Generates a short natural-language daily summary from structured
    MarketLens results.
    """

    def generate(
        self,
        recommendations: List[Dict[str, Any]],
        sector_scores: List[Dict[str, Any]],
        upgrade_downgrade_results: List[Dict[str, Any]],
    ) -> str:
        """
        Build the summary paragraph.

        Args:
            recommendations: output of RecommendationEngine.recommend_all()
            sector_scores: output of SectorAggregator.score_all_sectors()
            upgrade_downgrade_results: output of
                UpgradeDowngradeTracker.compare_batch()

        Returns:
            A short paragraph (string), built entirely from real,
            already-computed figures.
        """
        sentences: List[str] = []

        buy_count = sum(1 for r in recommendations if r["recommendation"] == "BUY")
        sell_count = sum(1 for r in recommendations if r["recommendation"] == "SELL")
        sentences.append(
            f"Today: {buy_count} BUY and {sell_count} SELL recommendation(s) "
            f"across {len(recommendations)} tracked entities."
        )

        upgrades = [r for r in upgrade_downgrade_results if r["change"] == "upgrade"]
        downgrades = [r for r in upgrade_downgrade_results if r["change"] == "downgrade"]

        if upgrades:
            names = ", ".join(r["entity"] for r in upgrades[:3])
            more = f" (+{len(upgrades) - 3} more)" if len(upgrades) > 3 else ""
            sentences.append(f"Upgraded since last check: {names}{more}.")
        if downgrades:
            names = ", ".join(r["entity"] for r in downgrades[:3])
            more = f" (+{len(downgrades) - 3} more)" if len(downgrades) > 3 else ""
            sentences.append(f"Downgraded since last check: {names}{more}.")

        if sector_scores:
            top_sector = sector_scores[0]
            sentences.append(
                f"Most-covered sector: {top_sector['sector']} "
                f"({top_sector['article_count']} articles, {top_sector['dominant_sentiment']} sentiment)."
            )

        if not upgrades and not downgrades and buy_count == 0 and sell_count == 0:
            sentences.append("No actionable changes today — all tracked entities remain at HOLD.")

        return " ".join(sentences)
