"""
web_scraper.py
-----------------
Web Scraper module for MarketLens.

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
Collect news articles from sources that expose NEITHER an RSS feed NOR
a JSON API — only a plain HTML page listing recent articles. This is
the last-resort collector in the pipeline's priority order: try RSS
first (RSSCollector), then a JSON API (APICollector), and only reach
for Web Scraper when a source offers neither.

APPROACH — dependency-free HTML parsing:
A minimal parser built on Python's built-in `html.parser.HTMLParser`
extracts every `<a>` tag's href + link text from a listing page. This
avoids adding BeautifulSoup/lxml as a project dependency, consistent
with the "stdlib-first" choice already made throughout MarketLens
(News Cleaner's regex-based tag stripping, API Collector's plain
`urllib` usage).

Each configured source declares:
- the listing page URL to scrape
- a URL substring that real article links contain (to filter out
  navigation, ads, and footer links that don't lead to articles)
- a minimum link-text length (filters out short nav links like "Home"
  or "Next" that might otherwise accidentally match the URL pattern)
"""

import logging
from html.parser import HTMLParser
from urllib.request import urlopen, Request
from urllib.parse import urljoin
from typing import List, Dict, Any, Optional, Tuple

from models import NewsArticle

logger = logging.getLogger("marketlens.web_scraper")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class _ArticleLinkParser(HTMLParser):
    """
    Minimal HTML parser that extracts every (href, link_text) pair from
    a page's `<a>` tags. Subclassing the standard library's HTMLParser
    avoids adding BeautifulSoup/lxml as a dependency — the same
    "stdlib first" choice already made in News Cleaner.
    """

    def __init__(self):
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._current_href = href
                self._current_text_parts = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            text = "".join(self._current_text_parts).strip()
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text_parts = []


class WebScraper:
    """
    Collects article links from a plain HTML listing page, for sources
    with no RSS feed or JSON API available.
    """

    def __init__(self, sources: List[Dict[str, Any]]):
        """
        Args:
            sources: list of dicts, each describing one scrape target:
                {
                    "name": "Example News Site",
                    "url": "https://example.com/news",
                    "category": "stocks",
                    "article_url_pattern": "/news/",  # substring every
                                                        # real article
                                                        # link's href
                                                        # must contain
                    "min_title_length": 15,  # optional, default 15 —
                                              # filters out short nav
                                              # links that might
                                              # coincidentally match
                                              # the URL pattern
                }
        """
        self.sources = sources

    def fetch_html(self, url: str) -> str:
        """
        Download the raw HTML of a page.

        Isolated as its own method — exactly like RSSCollector.fetch_feed
        and APICollector.fetch_json — so unit tests can mock it with no
        real network call, and so this is the single point of contact
        with the network for this module.
        """
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (MarketLens/1.0)"})
        with urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8", errors="replace")

    def _extract_article_links(self, html_content: str, source: Dict[str, Any]) -> List[Tuple[str, str]]:
        """
        Parse the page and keep only links that look like real
        articles: the href contains the source's configured pattern,
        and the link text is long enough to be a real headline.

        Returns a list of (absolute_url, link_text) tuples, with
        duplicate URLs removed (a listing page often links to the same
        article twice — e.g. from both a thumbnail and its headline).
        """
        parser = _ArticleLinkParser()
        parser.feed(html_content)

        pattern = source["article_url_pattern"]
        min_length = source.get("min_title_length", 15)

        seen_urls = set()
        results: List[Tuple[str, str]] = []
        for href, text in parser.links:
            if pattern not in href:
                continue
            if len(text) < min_length:
                continue
            full_url = urljoin(source["url"], href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            results.append((full_url, text))
        return results

    def collect_from_source(self, source: Dict[str, Any]) -> List[NewsArticle]:
        """
        Scrape ONE configured source's listing page.

        NEVER raises — same resilience discipline as every other
        collector in this pipeline: one broken/unreachable source must
        never take down the collection of every other source.
        """
        articles: List[NewsArticle] = []
        try:
            html_content = self.fetch_html(source["url"])
            links = self._extract_article_links(html_content, source)

            for url, title in links:
                articles.append(NewsArticle(
                    title=title,
                    summary="",  # a listing page rarely offers more than a headline
                    url=url,
                    source=source["name"],
                    category=source["category"],
                    published_at=None,  # not reliably available from a listing page alone
                ))

            logger.info("Scraped %d articles from '%s'", len(articles), source["name"])

        except Exception as exc:  # noqa: BLE001 — intentional broad catch
            logger.error("Failed to scrape '%s' (%s): %s", source["name"], source["url"], exc)

        return articles

    def collect_all(self) -> List[Dict[str, Any]]:
        """Scrape ALL configured sources, returning standardized dicts."""
        all_news: List[Dict[str, Any]] = []
        for source in self.sources:
            articles = self.collect_from_source(source)
            all_news.extend(article.to_dict() for article in articles)
        logger.info("Web Scraper: TOTAL articles collected: %d", len(all_news))
        return all_news
