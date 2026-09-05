"""
api_collector.py
-------------------
API Collector module for MarketLens.

STATUS: DEPRECATED, KEPT (TD-08, reviewed Phase 18)
--------------------------------------------------------
Nothing in the running system imports this module. `run_daily.py` --
the only scheduled production job -- collects through the RSS, Finnhub
and AlphaVantage collectors instead. Verified again on 2026-09-05: a
repository-wide search for consumers outside this file and its own
tests returns nothing.

It is kept rather than deleted because it is a working, tested
implementation of a real collection strategy, and the cost of keeping
it is one file. It should be removed if it still has no consumer after
one more phase.

Do not treat this notice as permission to delete it silently: removing
a collector is a decision about what news the system can reach, and
belongs in a phase that says so.

RESPONSIBILITY:
Collect news articles from sources that expose a structured JSON API
instead of an RSS feed. Many financial/news outlets run on WordPress,
whose built-in REST API (typically at `/wp-json/wp/v2/posts`) returns
JSON out of the box — keyless and public, no authentication needed.
This module produces the exact same standardized NewsArticle output as
RSSCollector; downstream modules never need to know whether an article
came from RSS or a JSON API.

DESIGN DECISION — declarative field mapping, not per-source code:
Rather than hardcoding one specific API's response shape, each
configured source declares a small FIELD MAP (which JSON keys hold the
title/summary/url/date, using dotted paths for nested values). This
makes the collector reusable across many different JSON APIs by
adding a config entry, not by writing new extraction code per source —
the same "config, not code" principle already used by sources.py,
company_registry.py, etc.
"""

import re
import json
import html as html_lib
import logging
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from typing import List, Dict, Any, Optional

from models import NewsArticle

logger = logging.getLogger("marketlens.api_collector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class APICollector:
    """
    Collects articles from JSON API sources, normalizing different
    response shapes into NewsArticle objects via a per-source field map.
    """

    def __init__(self, sources: List[Dict[str, Any]]):
        """
        Args:
            sources: list of dicts, each describing one API source:
                {
                    "name": "Example Blog",
                    "url": "https://example.com/wp-json/wp/v2/posts",
                    "category": "stocks",
                    "results_path": None,   # dotted path to the list of
                                             # items in the response, or
                                             # None if the response body
                                             # IS the list directly
                    "field_map": {
                        "title": "title.rendered",
                        "summary": "excerpt.rendered",
                        "url": "link",
                        "published_at": "date_gmt",
                    },
                }
        """
        self.sources = sources

    def fetch_json(self, url: str) -> Any:
        """
        Download and parse a JSON API response.

        Isolated as its own method — exactly like RSSCollector.fetch_feed
        — so unit tests can mock it directly with no real network call,
        and so this is the single point of contact with the network,
        should the fetching mechanism ever need to change.
        """
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (MarketLens/1.0)"})
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_by_path(self, obj: Any, dotted_path: str) -> Optional[Any]:
        """
        Retrieve a nested value from a dict using a dotted path string
        (e.g. "title.rendered" -> obj["title"]["rendered"]).

        WHY THIS EXISTS: different JSON APIs nest fields differently;
        this one small helper lets `field_map` describe ANY nesting
        depth declaratively, avoiding custom extraction code per source.
        """
        if not dotted_path:
            return None
        current = obj
        for part in dotted_path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _strip_html(self, text: Optional[str]) -> str:
        """
        Minimal HTML-tag removal for summary/excerpt fields — many JSON
        APIs (e.g. WordPress) return excerpts containing HTML markup,
        similar to RSS summaries. Reuses the same lightweight,
        dependency-free approach as News Cleaner rather than adding a
        parsing dependency here too.
        """
        if not text:
            return ""
        without_tags = re.sub(r"<[^>]+>", " ", text)
        return html_lib.unescape(without_tags).strip()

    def _parse_date(self, raw_value: Optional[str]) -> Optional[datetime]:
        """
        Parse an ISO-8601-ish date string into a UTC datetime,
        tolerating missing or malformed values (never raises).
        """
        if not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Many APIs (e.g. WordPress's *_gmt fields) omit the
                # timezone but are implicitly UTC.
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            logger.warning("Could not parse date: %r", raw_value)
            return None

    def _item_to_article(self, item: Dict[str, Any], source: Dict[str, Any]) -> NewsArticle:
        """Convert one raw JSON item into a standardized NewsArticle, using the source's field_map."""
        field_map = source["field_map"]
        title = self._get_by_path(item, field_map.get("title", "")) or ""
        summary = self._get_by_path(item, field_map.get("summary", "")) or ""
        url = self._get_by_path(item, field_map.get("url", "")) or ""
        published_raw = self._get_by_path(item, field_map.get("published_at", ""))

        return NewsArticle(
            title=self._strip_html(title),
            summary=self._strip_html(summary),
            url=url,
            source=source["name"],
            category=source["category"],
            published_at=self._parse_date(published_raw),
        )

    def collect_from_source(self, source: Dict[str, Any]) -> List[NewsArticle]:
        """
        Collect all articles from ONE configured API source.

        NEVER raises — mirrors RSSCollector.collect_from_source's
        resilience: one broken/unreachable source must never take down
        the collection of every other source.
        """
        articles: List[NewsArticle] = []
        try:
            response = self.fetch_json(source["url"])
            results_path = source.get("results_path")
            items = self._get_by_path(response, results_path) if results_path else response

            if not isinstance(items, list):
                logger.warning("Source '%s' did not return a list of items", source["name"])
                return []

            for item in items:
                article = self._item_to_article(item, source)
                if not article.title and not article.url:
                    continue
                articles.append(article)

            logger.info("Collected %d articles from '%s'", len(articles), source["name"])

        except Exception as exc:  # noqa: BLE001 — intentional broad catch
            logger.error("Failed to collect from '%s' (%s): %s", source["name"], source["url"], exc)

        return articles

    def collect_all(self) -> List[Dict[str, Any]]:
        """Collect articles from ALL configured API sources, returning standardized dicts."""
        all_news: List[Dict[str, Any]] = []
        for source in self.sources:
            articles = self.collect_from_source(source)
            all_news.extend(article.to_dict() for article in articles)
        logger.info("API Collector: TOTAL articles collected: %d", len(all_news))
        return all_news
