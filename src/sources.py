"""
sources.py
-------------
RSS feed source configuration for MarketLens's News Collector.

Each entry:
- name: human-readable source name, used in the article's "source" field
- url: the RSS feed URL, fetched directly by feedparser
- category: "bvb" / "stocks" / "crypto" — mirrors the categories used
  by company_registry.py, so downstream modules can filter consistently

NOTE ON COVERAGE:
Not every outlet in the project brief publishes a public, stable RSS feed
(e.g. Financial Times and Binance Blog require authenticated APIs or
scraping, not RSS). Those sources are intentionally NOT listed here —
they belong to the future API Collector / Web Scraper modules. Listing
a source we can't reliably parse via RSS would silently produce empty
or broken data, which violates the "production-quality" requirement.

VERIFICATION NOTE (v1.4): every source below has been directly
confirmed — either by fetching its feed and seeing real, current
content, or by a matching official statement from the outlet itself —
before being added. Several other outlets from the project brief were
explicitly CHECKED and rejected, for concrete, documented reasons
rather than silently skipped:
- Seeking Alpha: feed technically exists (seekingalpha.com/feed.xml),
  but the site's robots.txt explicitly disallows automated access —
  respected here rather than worked around.
- The Motley Fool: no single, stable, unambiguous public feed URL
  could be confirmed (third-party listings reference a truncated
  partner/Google feed URL that couldn't be verified directly).
- Business Insider: the outlet's OWN help center FAQ states "No, we
  do not currently offer an RSS feed for our content" — trusted over
  third-party aggregator listings that may reference a discontinued
  feed.
These three may be worth revisiting later via the API Collector / Web
Scraper modules instead of RSS, but are deliberately excluded here.
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
    {"name": "European Central Bank", "url": "https://www.ecb.europa.eu/rss/press.xml", "category": "stocks"},

    # --- Crypto ---
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "crypto"},
    {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss", "category": "crypto"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed/rss", "category": "crypto"},
]
