"""
rss_collector.py
----------------
News Collector v1 — RSS Collector for MarketLens.

RESPONSIBILITY (Single Responsibility Principle):
This module ONLY fetches RSS feeds and converts their entries into
standardized NewsArticle objects. It does NOT clean text, does NOT
detect duplicates, does NOT do NLP or sentiment analysis. Those concerns
belong to separate modules (News Cleaner, Duplicate Detector, Sentiment
Engine, ...) so each can be built, tested, and replaced independently
without ever touching this file.
"""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

import feedparser

from models import NewsArticle
from sources import RSS_FEEDS

# Module-level logger instead of print(): this lets the whole platform
# later plug into a central logging/monitoring stack (e.g. writing to a
# file, or forwarding to a monitoring dashboard) without changing a
# single line of collection logic.
logger = logging.getLogger("marketlens.rss_collector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class RSSCollector:
    """
    Collects news articles from a configurable list of RSS feeds and
    returns them as `all_news`: a flat list of standardized dictionaries.
    """

    def __init__(self, feeds: Optional[List[Dict[str, str]]] = None):
        """
        Args:
            feeds: list of feed configs (each a dict with 'name', 'url',
                   'category'). Defaults to RSS_FEEDS from sources.py.

        DESIGN DECISION — dependency injection of `feeds`:
        Allowing feeds to be passed in (rather than hardcoded) means the
        collector can be unit-tested with fake, controlled feed lists,
        completely decoupled from the real production config and from
        the network.
        """
        self.feeds = feeds if feeds is not None else RSS_FEEDS

    def _parse_published_date(self, entry: Any) -> Optional[datetime]:
        """
        Extract and normalize an entry's publication date.

        WHY THIS EXISTS:
        RSS feeds are inconsistent in practice: some entries expose
        `published_parsed`, others only `updated_parsed`, and some expose
        neither. Centralizing this fallback logic in one helper avoids
        duplicating it (DRY) anywhere else NewsArticle objects are built.

        Returns:
            A timezone-aware UTC datetime, or None if no usable date
            field was present or it failed to parse.
        """
        time_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if time_struct is None:
            return None
        try:
            # feedparser returns a time.struct_time already normalized to
            # UTC per the RSS/Atom specs, so we build an aware UTC datetime
            # directly from its first 6 fields (Y, M, D, H, M, S).
            return datetime(*time_struct[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # A malformed date must never crash the whole collection run.
            logger.warning("Could not parse publication date: %s", time_struct)
            return None

    def _entry_to_article(self, entry: Any, source_name: str, category: str) -> NewsArticle:
        """
        Convert one feedparser entry into a standardized NewsArticle.

        This is the actual "standardization" step required by the spec:
        no matter which RSS feed produced the entry, the output object
        always has the same fields, in the same shape.
        """
        return NewsArticle(
            title=getattr(entry, "title", "").strip(),
            summary=getattr(entry, "summary", "").strip(),
            url=getattr(entry, "link", "").strip(),
            source=source_name,
            category=category,
            published_at=self._parse_published_date(entry),
        )

    # Some sources (observed with Profit.ro) reject feedparser's default
    # User-Agent ("python-feedparser/x.x ...") and respond with an HTML
    # "are you a robot" page instead of the real XML feed — feedparser
    # then flags this as bozo=1 with a "not an XML media type" error.
    # Sending a realistic browser User-Agent avoids that block. This is
    # a class-level constant (not per-call) since it's a fixed identity
    # we present to every source, not something callers should vary.
    _REQUEST_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    def fetch_feed(self, feed_url: str) -> Any:
        """
        Download and parse a single RSS feed via feedparser.

        Kept as its own method (rather than inlined into
        collect_from_source) so that:
        1. Unit tests can mock it directly — no real network call needed.
        2. It's the single point of contact with the external library,
           should we ever need to swap feedparser for something else.

        A browser-like User-Agent is passed via `request_headers` because
        some sources otherwise silently serve an HTML page instead of the
        real feed (see _REQUEST_HEADERS comment above).
        """
        return feedparser.parse(feed_url, request_headers=self._REQUEST_HEADERS)

    def collect_from_source(self, source: Dict[str, str]) -> List[NewsArticle]:
        """
        Collect all articles from ONE configured source.

        Returns:
            A list of NewsArticle objects. NEVER raises — on any failure
            (timeout, DNS error, malformed XML, etc.) it logs the error
            and returns an empty list, so one broken feed can never take
            down the collection of every other source.
        """
        name, url, category = source["name"], source["url"], source["category"]
        articles: List[NewsArticle] = []

        try:
            parsed_feed = self.fetch_feed(url)

            # feedparser sets bozo=1 when the feed doesn't parse as strict
            # XML. We log a warning but still use whatever entries did
            # parse — partial data beats none for a news aggregator.
            if getattr(parsed_feed, "bozo", 0):
                logger.warning(
                    "Feed '%s' flagged as malformed (bozo): %s",
                    name, getattr(parsed_feed, "bozo_exception", ""),
                )

            for entry in parsed_feed.entries:
                article = self._entry_to_article(entry, source_name=name, category=category)
                # Skip entries carrying no usable information at all —
                # they would only pollute downstream deduplication/NLP.
                if not article.title and not article.url:
                    continue
                articles.append(article)

            logger.info("Collected %d articles from '%s'", len(articles), name)

        except Exception as exc:  # noqa: BLE001 — intentional broad catch
            logger.error("Failed to collect from '%s' (%s): %s", name, url, exc)

        return articles

    def collect_all(self) -> List[Dict[str, Any]]:
        """
        Collect articles from ALL configured feeds.

        Returns:
            all_news: a flat list of plain dictionaries (one per
            article), exactly as required by the spec. Dataclasses are
            converted to dicts right here, at the output boundary of
            this module, so every other module downstream only ever has
            to deal with plain dicts/JSON — never with NewsArticle
            objects directly.
        """
        all_news: List[Dict[str, Any]] = []

        for source in self.feeds:
            articles = self.collect_from_source(source)
            all_news.extend(article.to_dict() for article in articles)

        logger.info("TOTAL articles collected across all feeds: %d", len(all_news))
        return all_news
