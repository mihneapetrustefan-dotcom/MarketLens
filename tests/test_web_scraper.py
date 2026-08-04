"""
test_web_scraper.py
-----------------------
Unit tests for Web Scraper v1 (web_scraper.py).

TESTING STRATEGY:
Tests mock fetch_html() (exactly like RSSCollector mocks fetch_feed()),
feeding small, hand-crafted HTML snippets — offline, deterministic, no
real network or website dependency.
"""

import unittest
from unittest.mock import patch

from web_scraper import WebScraper, _ArticleLinkParser


SAMPLE_SOURCE = {
    "name": "Example News Site",
    "url": "https://example.com/news",
    "category": "stocks",
    "article_url_pattern": "/news/",
    "min_title_length": 15,
}

SAMPLE_HTML = """
<html><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<div class="listing">
  <a href="/news/tesla-earnings-beat">Tesla beats earnings estimates sharply</a>
  <a href="/news/tesla-earnings-beat">Tesla beats earnings estimates sharply</a>
  <a href="/news/oil-prices-surge">Oil prices surge amid supply concerns</a>
  <a href="/category/stocks">Stocks</a>
</div>
</body></html>
"""


class TestArticleLinkParser(unittest.TestCase):
    """Tests for the underlying stdlib-based HTML link parser."""

    def test_extracts_href_and_text_pairs(self):
        parser = _ArticleLinkParser()
        parser.feed('<a href="/x">Some Link Text</a>')
        self.assertEqual(parser.links, [("/x", "Some Link Text")])

    def test_ignores_non_anchor_tags(self):
        parser = _ArticleLinkParser()
        parser.feed('<div>Not a link</div><a href="/y">Real link</a>')
        self.assertEqual(parser.links, [("/y", "Real link")])

    def test_handles_multiple_links(self):
        parser = _ArticleLinkParser()
        parser.feed('<a href="/a">First</a><a href="/b">Second</a>')
        self.assertEqual(len(parser.links), 2)


class TestExtractArticleLinks(unittest.TestCase):
    """Tests for _extract_article_links(): filtering and deduplication."""

    def setUp(self):
        self.scraper = WebScraper(sources=[SAMPLE_SOURCE])

    def test_filters_by_url_pattern(self):
        links = self.scraper._extract_article_links(SAMPLE_HTML, SAMPLE_SOURCE)
        urls = [url for url, _ in links]
        self.assertTrue(all("/news/" in url for url in urls))
        # Nav/category links (no "/news/") must be excluded.
        self.assertFalse(any("/about" in url or "/category" in url for url in urls))

    def test_filters_out_short_link_text(self):
        # "Home" and "About" are both too short (< min_title_length=15)
        # even if they happened to match the URL pattern.
        links = self.scraper._extract_article_links(SAMPLE_HTML, SAMPLE_SOURCE)
        texts = [text for _, text in links]
        self.assertNotIn("Home", texts)

    def test_deduplicates_repeated_links(self):
        # The Tesla link appears twice in SAMPLE_HTML (once likely from
        # a thumbnail, once from the headline) but must appear only once.
        links = self.scraper._extract_article_links(SAMPLE_HTML, SAMPLE_SOURCE)
        urls = [url for url, _ in links]
        tesla_count = sum(1 for u in urls if "tesla-earnings-beat" in u)
        self.assertEqual(tesla_count, 1)

    def test_resolves_relative_urls_to_absolute(self):
        links = self.scraper._extract_article_links(SAMPLE_HTML, SAMPLE_SOURCE)
        urls = [url for url, _ in links]
        self.assertTrue(all(url.startswith("https://example.com") for url in urls))


class TestCollectFromSource(unittest.TestCase):
    """Tests for collect_from_source(): the core per-source scraping logic."""

    def test_standardizes_scraped_articles(self):
        scraper = WebScraper(sources=[SAMPLE_SOURCE])
        with patch.object(scraper, "fetch_html", return_value=SAMPLE_HTML):
            articles = scraper.collect_from_source(SAMPLE_SOURCE)

        self.assertEqual(len(articles), 2)  # Tesla (deduped) + Oil
        titles = {a.title for a in articles}
        self.assertIn("Tesla beats earnings estimates sharply", titles)
        self.assertIn("Oil prices surge amid supply concerns", titles)
        self.assertTrue(all(a.source == "Example News Site" for a in articles))

    def test_exception_during_fetch_returns_empty_list_not_raise(self):
        scraper = WebScraper(sources=[SAMPLE_SOURCE])
        with patch.object(scraper, "fetch_html", side_effect=RuntimeError("network down")):
            articles = scraper.collect_from_source(SAMPLE_SOURCE)
        self.assertEqual(articles, [])

    def test_page_with_no_matching_links_returns_empty_list(self):
        scraper = WebScraper(sources=[SAMPLE_SOURCE])
        with patch.object(scraper, "fetch_html", return_value="<html><body>No articles here</body></html>"):
            articles = scraper.collect_from_source(SAMPLE_SOURCE)
        self.assertEqual(articles, [])


class TestCollectAll(unittest.TestCase):
    """Tests for collect_all(): the full-batch orchestration method."""

    def test_aggregates_across_multiple_sources(self):
        source_a = {**SAMPLE_SOURCE, "name": "SiteA", "url": "https://a.com/news"}
        source_b = {**SAMPLE_SOURCE, "name": "SiteB", "url": "https://b.com/news"}
        scraper = WebScraper(sources=[source_a, source_b])

        def fake_fetch(url):
            return SAMPLE_HTML

        with patch.object(scraper, "fetch_html", side_effect=fake_fetch):
            all_news = scraper.collect_all()

        self.assertEqual(len(all_news), 4)  # 2 articles x 2 sources
        self.assertTrue(all(isinstance(a, dict) for a in all_news))

    def test_one_broken_source_does_not_block_others(self):
        good = {**SAMPLE_SOURCE, "name": "Good", "url": "https://good.com/news"}
        bad = {**SAMPLE_SOURCE, "name": "Bad", "url": "https://bad.com/news"}
        scraper = WebScraper(sources=[good, bad])

        def fake_fetch(url):
            if url == bad["url"]:
                raise RuntimeError("simulated failure")
            return SAMPLE_HTML

        with patch.object(scraper, "fetch_html", side_effect=fake_fetch):
            all_news = scraper.collect_all()

        self.assertEqual(len(all_news), 2)  # only "Good" contributed
        self.assertTrue(all(a["source"] == "Good" for a in all_news))

    def test_empty_sources_list_returns_empty_result(self):
        scraper = WebScraper(sources=[])
        self.assertEqual(scraper.collect_all(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
