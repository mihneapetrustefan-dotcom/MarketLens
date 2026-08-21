"""
source_credibility.py
-------------------------
Source Credibility module for MarketLens.

WHAT THIS DELIBERATELY IS, AND IS NOT: this is a TRANSPARENT
CLASSIFICATION of source TYPE (official government/central-bank
release, established wire/financial press, or specialized/aggregator
outlet) — not a "fake news detector". A rule-based system without
real fact-checking or cross-referencing capability cannot honestly
claim to detect misinformation; claiming otherwise would be the kind
of overclaiming this project avoids everywhere else (see the "facts,
no verdict" policy on the Date de piață market table). What this DOES
provide honestly: letting a person see, for any entity's coverage,
what proportion comes from official/major sources versus more
specialized or unclassified ones — real, verifiable information about
WHERE a story came from, not a verdict on whether it's true.

CURRENT SCOPE: informational/transparency only. This is NOT wired into
ConfidenceEngine or RecommendationEngine — it doesn't change any
recommendation. That's a deliberate, separate decision to make later,
not an oversight — folding source tier into the confidence formula
would need its own careful calibration and testing, same as every
other scoring change in this project.
"""

from collections import Counter
from typing import Dict, List, Any, Optional

# source name (must match exactly what appears in an article's
# "source" field — see sources.py) -> credibility tier.
SOURCE_TIERS: Dict[str, str] = {
    # --- Official (government / central bank primary releases) ---
    "SEC Press Releases": "official",
    "Federal Reserve Press Releases": "official",
    "European Central Bank": "official",

    # --- Wire services & established financial press ---
    "Reuters": "wire_and_major_press",
    "CNBC Top News": "wire_and_major_press",
    "CNBC": "wire_and_major_press",
    "Bloomberg": "wire_and_major_press",
    "MarketWatch Top Stories": "wire_and_major_press",
    "MarketWatch": "wire_and_major_press",
    "Yahoo Finance": "wire_and_major_press",
    "Ziarul Financiar": "wire_and_major_press",
    "Profit.ro": "wire_and_major_press",

    # --- Specialized / aggregator outlets (legitimate, but more
    # specialized or less institutionally established than the above) ---
    "Investing.com Stock Market News": "specialized_or_aggregator",
    "Investing.com": "specialized_or_aggregator",
    "Economedia": "specialized_or_aggregator",
    "CoinDesk": "specialized_or_aggregator",
    "CoinTelegraph": "specialized_or_aggregator",
    "Decrypt": "specialized_or_aggregator",
}

# Display order and Romanian labels for the Dashboard.
TIER_LABELS: Dict[str, str] = {
    "official": "Surse oficiale (guvern / bancă centrală)",
    "wire_and_major_press": "Agenții de presă și presă financiară majoră",
    "specialized_or_aggregator": "Surse specializate / agregatoare",
    "unclassified": "Neclasificate încă",
}
TIER_ORDER: List[str] = ["official", "wire_and_major_press", "specialized_or_aggregator", "unclassified"]


def get_source_tier(source_name: Optional[str]) -> str:
    """
    Return the credibility tier for a source name. Returns
    "unclassified" for anything not in SOURCE_TIERS (e.g. a new RSS
    feed not yet categorized, or a dynamic source name coming from
    Finnhub/Alpha Vantage's own per-article "source" field) — never
    guesses, never raises.
    """
    if not source_name:
        return "unclassified"
    return SOURCE_TIERS.get(source_name, "unclassified")


def summarize_sources(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Summarize how many articles (in the given batch) come from each
    credibility tier, and which specific sources make up each tier.

    Returns:
        A list of {"tier", "tier_label", "article_count", "sources":
        [{"name", "article_count"}, ...]}, ordered by TIER_ORDER, most
        authoritative first. A tier with zero articles is omitted.
    """
    tier_counts: Counter = Counter()
    source_counts: Dict[str, Counter] = {tier: Counter() for tier in TIER_ORDER}

    for article in articles:
        source_name = article.get("source")
        tier = get_source_tier(source_name)
        tier_counts[tier] += 1
        if source_name:
            source_counts[tier][source_name] += 1

    summary = []
    for tier in TIER_ORDER:
        if tier_counts[tier] == 0:
            continue
        sources = [
            {"name": name, "article_count": count}
            for name, count in source_counts[tier].most_common()
        ]
        summary.append({
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "article_count": tier_counts[tier],
            "sources": sources,
        })
    return summary
