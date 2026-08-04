"""
test_api_collector.py
-------------------------
Unit tests for API Collector v1 (api_collector.py).

TESTING STRATEGY:
All tests mock fetch_json() directly (exactly like RSSCollector's
tests mock fetch_feed()), so they are offline, deterministic, and never
depend on any real API being reachable.
"""

import unittest
from unittest.mock import patch

from api_collector import APICollector


WORDPRESS_STYLE_SOURCE = {
    "name": "Example Blog",
    "url": "https://example.com/wp-json/wp/v2/posts",
    "category": "stocks",
    "results_path": None,  # response body IS the list directly
    "field_map": {
        "title": "title.rendered",
        "summary": "excerpt.rendered",
        "url": "link",
        "published_at": "date_gmt",
    },
}

WRAPPED_SOURCE = {
    "name": "Wrapped API",
    "url": "https://example.com/api/articles",
    "category": "crypto",
    "results_path": "data.articles",  # list is nested inside the response
    "field_map": {
        "title": "headline",
        "summary": "description",
        "url": "permalink",
        "published_at": "published",
    },
}


class TestGetByPath(unittest.TestCase):
    """Tests for the dotted-path nested value extraction helper."""

    def setUp(self):
        self.collector = APICollector(sources=[])

    def test_retrieves_top_level_value(self):
        self.assertEqual(self.collector._get_by_path({"link": "http://x"}, "link"), "http://x")

    def test_retrieves_nested_value(self):
        obj = {"title": {"rendered": "Hello World"}}
        self.assertEqual(self.collector._get_by_path(obj, "title.rendered"), "Hello World")

    def test_returns_none_for_missing_path(self):
        self.assertIsNone(self.collector._get_by_path({"title": {}}, "title.rendered"))

    def test_returns_none_for_empty_path(self):
        self.assertIsNone(self.collector._get_by_path({"a": 1}, ""))


class TestStripHtmlAndDateParsing(unittest.TestCase):
    def setUp(self):
        self.collector = APICollector(sources=[])

    def test_strip_html_removes_tags_and_decodes_entities(self):
        result = self.collector._strip_html("<p>Stocks &amp; bonds</p>")
        self.assertEqual(result, "Stocks & bonds")

    def test_strip_html_handles_empty_input(self):
        self.assertEqual(self.collector._strip_html(""), "")
        self.assertEqual(self.collector._strip_html(None), "")

    def test_parse_date_handles_gmt_style_string(self):
        result = self.collector._parse_date("2026-08-01T10:00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_parse_date_handles_malformed_string_gracefully(self):
        self.assertIsNone(self.collector._parse_date("not-a-date"))

    def test_parse_date_handles_missing_value(self):
        self.assertIsNone(self.collector._parse_date(None))


class TestCollectFromSource(unittest.TestCase):
    """Tests for collect_from_source(): the core per-source collection logic."""

    def test_flat_response_standardizes_fields(self):
        collector = APICollector(sources=[WORDPRESS_STYLE_SOURCE])
        fake_response = [
            {
                "title": {"rendered": "Markets rally on strong data"},
                "excerpt": {"rendered": "<p>Stocks rose sharply today</p>"},
                "link": "https://example.com/markets-rally",
                "date_gmt": "2026-08-01T09:00:00",
            }
        ]
        with patch.object(collector, "fetch_json", return_value=fake_response):
            articles = collector.collect_from_source(WORDPRESS_STYLE_SOURCE)

        self.assertEqual(len(articles), 1)
        art = articles[0]
        self.assertEqual(art.title, "Markets rally on strong data")
        self.assertEqual(art.summary, "Stocks rose sharply today")
        self.assertEqual(art.url, "https://example.com/markets-rally")
        self.assertEqual(art.source, "Example Blog")
        self.assertIsNotNone(art.published_at)

    def test_wrapped_response_extracts_nested_list_via_results_path(self):
        collector = APICollector(sources=[WRAPPED_SOURCE])
        fake_response = {
            "data": {
                "articles": [
                    {"headline": "Bitcoin surges", "description": "", "permalink": "https://x.com/btc", "published": "2026-08-01T00:00:00"},
                ]
            }
        }
        with patch.object(collector, "fetch_json", return_value=fake_response):
            articles = collector.collect_from_source(WRAPPED_SOURCE)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Bitcoin surges")

    def test_non_list_response_returns_empty_list(self):
        collector = APICollector(sources=[WORDPRESS_STYLE_SOURCE])
        with patch.object(collector, "fetch_json", return_value={"unexpected": "shape"}):
            articles = collector.collect_from_source(WORDPRESS_STYLE_SOURCE)
        self.assertEqual(articles, [])

    def test_exception_during_fetch_returns_empty_list_not_raise(self):
        collector = APICollector(sources=[WORDPRESS_STYLE_SOURCE])
        with patch.object(collector, "fetch_json", side_effect=RuntimeError("network down")):
            articles = collector.collect_from_source(WORDPRESS_STYLE_SOURCE)
        self.assertEqual(articles, [])

    def test_skips_items_with_no_title_and_no_url(self):
        collector = APICollector(sources=[WORDPRESS_STYLE_SOURCE])
        fake_response = [{"title": {"rendered": ""}, "excerpt": {"rendered": ""}, "link": "", "date_gmt": None}]
        with patch.object(collector, "fetch_json", return_value=fake_response):
            articles = collector.collect_from_source(WORDPRESS_STYLE_SOURCE)
        self.assertEqual(articles, [])


class TestCollectAll(unittest.TestCase):
    """Tests for collect_all(): the full-batch orchestration method."""

    def test_aggregates_across_multiple_sources(self):
        collector = APICollector(sources=[WORDPRESS_STYLE_SOURCE, WRAPPED_SOURCE])

        def fake_fetch(url):
            if url == WORDPRESS_STYLE_SOURCE["url"]:
                return [{"title": {"rendered": "A1"}, "excerpt": {"rendered": ""}, "link": "http://a/1", "date_gmt": None}]
            return {"data": {"articles": [{"headline": "B1", "description": "", "permalink": "http://b/1", "published": None}]}}

        with patch.object(collector, "fetch_json", side_effect=fake_fetch):
            all_news = collector.collect_all()

        self.assertEqual(len(all_news), 2)
        self.assertTrue(all(isinstance(a, dict) for a in all_news))

    def test_one_broken_source_does_not_block_others(self):
        good = WORDPRESS_STYLE_SOURCE
        bad = {**WRAPPED_SOURCE, "name": "Bad"}
        collector = APICollector(sources=[good, bad])

        def fake_fetch(url):
            if url == bad["url"]:
                raise RuntimeError("simulated failure")
            return [{"title": {"rendered": "Good article"}, "excerpt": {"rendered": ""}, "link": "http://good/1", "date_gmt": None}]

        with patch.object(collector, "fetch_json", side_effect=fake_fetch):
            all_news = collector.collect_all()

        self.assertEqual(len(all_news), 1)
        self.assertEqual(all_news[0]["source"], "Example Blog")

    def test_empty_sources_list_returns_empty_result(self):
        collector = APICollector(sources=[])
        self.assertEqual(collector.collect_all(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
