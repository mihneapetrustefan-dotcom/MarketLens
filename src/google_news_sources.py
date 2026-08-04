"""
google_news_sources.py
--------------------------
Historical News Backfill via Google News RSS search.

RESPONSIBILITY:
Generate RSS-shaped source configs — compatible directly with the
ALREADY-BUILT RSSCollector, no new collector code required — that
search Google News for a SPECIFIC company/entity over a chosen time
window in the past (e.g. the last 60 days).

WHY THIS EXISTS: a normal outlet's RSS feed (Reuters, CNBC, Ziarul
Financiar, ...) is a live "ticker tape" — it shows what's recent, not
an archive. To see what was said about ONE specific company TWO MONTHS
AGO, a general feed can't help; what's needed is a targeted, per-
company SEARCH. Google News' public search RSS endpoint supports
exactly this, via a "when:Nd" query modifier, aggregating across
thousands of outlets, with NO API key required.

CONCRETE MOTIVATING EXAMPLE: today's news says a company laid off
workers; a month ago it announced a large multi-year investment. A
single outlet's live feed would only ever show whichever story is
CURRENT right now — this module lets both stories be pulled back into
the pipeline together, so Confidence Score (combined with Time Decay)
can weigh them against each other properly, instead of only ever
seeing whatever happens to be in this week's headlines.

HONESTY NOTE (documented, not hidden): this relies on an UNOFFICIAL,
undocumented feature of Google News' search RSS endpoint (the "when:Nd"
modifier). It has worked reliably for a long time and is widely used,
but Google could change or restrict it at any time without notice.
It has NOT been verified against a live request from this environment
(no internet access here) — test it yourself in Colab before depending
on it for anything important.
"""

from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus


def build_entity_search_sources(
    entity_names: List[str],
    days_back: int = 60,
    category_lookup: Optional[Dict[str, str]] = None,
    language: str = "en",
    country: str = "US",
) -> List[Dict[str, Any]]:
    """
    Build one Google-News-search RSS source config per entity name.

    Args:
        entity_names: company/asset names to search for (e.g. drawn
            from COMPANY_REGISTRY's canonical_name field).
        days_back: how many days into the past to search, via Google's
            "when:Nd" modifier. Default 60 (~2 months).
        category_lookup: optional dict mapping entity name -> category
            (stocks/bvb/crypto), so generated sources are tagged
            consistently with the rest of the registry. Entities not
            found in the lookup default to category "stocks".
        language: Google News UI language code (e.g. "en", "ro").
            Use "ro" for Romanian-language results — relevant when
            searching for BVB companies.
        country: Google News edition/country code (e.g. "US", "RO").

    Returns:
        A list of source dicts shaped EXACTLY like sources.py's
        RSS_FEEDS entries — {"name", "url", "category"} — so they can
        be passed directly to `RSSCollector(feeds=...)` with zero new
        collector code.
    """
    category_lookup = category_lookup or {}
    sources: List[Dict[str, Any]] = []

    for entity in entity_names:
        # Quoting the entity name (via %22 around it) narrows the
        # search to the exact phrase, reducing false matches against
        # unrelated articles that merely share a common word with the
        # company name.
        query = quote_plus(f'"{entity}" when:{days_back}d')
        url = (
            f"https://news.google.com/rss/search?q={query}"
            f"&hl={language}&gl={country}&ceid={country}:{language}"
        )
        sources.append({
            "name": f"Google News: {entity}",
            "url": url,
            "category": category_lookup.get(entity, "stocks"),
        })

    return sources
