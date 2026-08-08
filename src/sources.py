"""
sources.py
----------
Central registry of RSS feed sources used by the News Collector.

DESIGN DECISION:
Feed configuration is kept completely separate from collection logic
(separation of concerns). Adding, removing, or fixing a feed URL should
only ever require editing this file — never rss_collector.py. This also
makes RSSCollector trivially unit-testable, since tests can inject their
own fake feed lists without touching this config at all.

Each entry has:
- name:     human-readable source name -> stored in NewsArticle.source
- url:      the RSS feed URL
- category: default market category for this source (stocks / crypto / bvb).
            This is a *default* — the future Sector Detector module may
            refine/override it per individual article.

NOTE ON COVERAGE:
Not every outlet in the project brief publishes a public, stable RSS feed
(e.g. Bursa.ro's feed endpoint needs verification; Financial Times and
Binance Blog require authenticated APIs or scraping, not RSS). Those
sources are intentionally NOT listed here — they belong to the future
API Collector / Web Scraper modules. Listing a source we can't reliably
parse via RSS would silently produce empty or broken data, which
violates the "production-quality" requirement.

VERIFICATION NOTE (v1.4 additions): Federal Reserve and Investing.com
were added after directly confirming their feed URLs are real and
currently serving content (Federal Reserve fetched live; Investing.com
sourced from a third-party feed directory, not fetched directly —
slightly lower confidence, flagged here rather than hidden). Further
outlets mentioned in the project brief (Seeking Alpha, Motley Fool,
Financial Times, Barron's, Decrypt, ECB, IMF, etc.) were deliberately
NOT added without the same verification — guessing URLs risks silently
shipping a broken/wrong source, which RSSCollector would fail on
gracefully but would still misrepresent actual coverage.
"""

from typing import List, Dict

RSS_FEEDS: List[Dict[str, str]] = [
    # --- Romania (BVB market) ---
    {"name": "Ziarul Financiar", "url": "https://www.zf.ro/rss", "category": "bvb"},
    {"name": "Profit.ro", "url": "https://www.profit.ro/rss", "category": "bvb"},
    {"name": "Economedia", "url": "https://economedia.ro/feed", "category": "bvb"},

    # --- International (Stocks / ETF / Macro) ---
    {"name": "CNBC Top News", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "stocks"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "stocks"},
    {"name": "MarketWatch Top Stories", "url": "http://feeds.marketwatch.com/marketwatch/topstories/", "category": "stocks"},
    {"name": "SEC Press Releases", "url": "https://www.sec.gov/news/pressreleases.rss", "category": "stocks"},
    {"name": "Federal Reserve Press Releases", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "category": "stocks"},
    {"name": "Investing.com Stock Market News", "url": "https://www.investing.com/rss/news_25.rss", "category": "stocks"},

    # --- Crypto ---
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "crypto"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss", "category": "crypto"},
]
