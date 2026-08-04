"""
test_rss_collector.py
----------------------
Unit tests for News Collector v1 (models.py + rss_collector.py).

TESTING STRATEGY:
- All tests below are OFFLINE and use a mocked `fetch_feed` method, so
  they are deterministic, fast, and never depend on any real website
  being reachable. This is essential: network conditions must never
  determine whether this module "passes".
- A separate LIVE test class is included, clearly marked and skipped
  by default, for manual sanity-checking against real feeds when you
  have network access (e.g. in Google Colab).
"""

import unittest
from unittest.mock import patch
from types import SimpleNamespace

from models import NewsArticle
from rss_collector import RSSCollector


def _fake_feed(entries, bozo=0):
    """Builds an object mimicking feedparser.parse()'s return value."""
    return SimpleNamespace(entries=entries, bozo=bozo, bozo_exception="parse error")


class TestNewsArticle(unittest.TestCase):
    """Tests for the standardized NewsArticle data model."""

    def test_article_gets_unique_uuid(self):
        a1 = NewsArticle(title="A")
        a2 = NewsArticle(title="B")
        self.assertNotEqual(a1.article_id, a2.article_id)
        self.assertTrue(len(a1.article_id) > 0)

    def test_to_dict_is_json_safe(self):
        article = NewsArticle(title="Test", source="X", category="stocks")
        d = article.to_dict()
        self.assertIsInstance(d, dict)
        self.assertIsInstance(d["collected_at"], str)  # datetime -> str
        self.assertIn("article_id", d)
        self.assertEqual(d["title"], "Test")


class TestRSSCollector(unittest.TestCase):
    """Offline, deterministic tests for the RSSCollector class."""

    def setUp(self):
        # A single fake source, independent of sources.py, so these tests
        # never break if the real feed list changes.
        self.fake_source = {"name": "TestSource", "url": "http://fake.test/rss", "category": "stocks"}
        self.collector = RSSCollector(feeds=[self.fake_source])

    def test_collect_from_source_standardizes_fields(self):
        entry = SimpleNamespace(
            title="Company X beats earnings",
            summary="Short summary text",
            link="http://fake.test/article1",
            published_parsed=(2024, 5, 1, 12, 0, 0, 0, 0, 0),
        )
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([entry])):
            articles = self.collector.collect_from_source(self.fake_source)

        self.assertEqual(len(articles), 1)
        art = articles[0]
        self.assertEqual(art.title, "Company X beats earnings")
        self.assertEqual(art.url, "http://fake.test/article1")
        self.assertEqual(art.source, "TestSource")
        self.assertEqual(art.category, "stocks")
        self.assertIsNotNone(art.published_at)
        self.assertTrue(art.article_id)

    def test_collect_from_source_skips_empty_entries(self):
        empty_entry = SimpleNamespace(title="", summary="", link="")
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([empty_entry])):
            articles = self.collector.collect_from_source(self.fake_source)
        self.assertEqual(len(articles), 0)

    def test_collect_from_source_handles_exceptions_gracefully(self):
        with patch.object(self.collector, "fetch_feed", side_effect=RuntimeError("network down")):
            articles = self.collector.collect_from_source(self.fake_source)
        self.assertEqual(articles, [])  # must never raise; must return []

    def test_collect_from_source_handles_bozo_malformed_feed(self):
        entry = SimpleNamespace(title="Still parses", summary="", link="http://fake.test/x")
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([entry], bozo=1)):
            articles = self.collector.collect_from_source(self.fake_source)
        # Even a "malformed" (bozo) feed should still yield whatever
        # entries it did manage to parse.
        self.assertEqual(len(articles), 1)

    def test_missing_date_returns_none(self):
        entry = SimpleNamespace(title="No date entry", summary="", link="http://fake.test/nodate")
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([entry])):
            articles = self.collector.collect_from_source(self.fake_source)
        self.assertIsNone(articles[0].published_at)

    def test_falls_back_to_updated_parsed(self):
        entry = SimpleNamespace(
            title="Uses updated date",
            summary="",
            link="http://fake.test/updated",
            updated_parsed=(2023, 12, 25, 8, 0, 0, 0, 0, 0),
        )
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([entry])):
            articles = self.collector.collect_from_source(self.fake_source)
        self.assertIsNotNone(articles[0].published_at)
        self.assertEqual(articles[0].published_at.year, 2023)

    def test_collect_all_returns_list_of_plain_dicts(self):
        entry = SimpleNamespace(
            title="Test headline",
            summary="Summary",
            link="http://fake.test/a",
            published_parsed=(2024, 1, 1, 0, 0, 0, 0, 0, 0),
        )
        with patch.object(self.collector, "fetch_feed", return_value=_fake_feed([entry])):
            all_news = self.collector.collect_all()

        self.assertIsInstance(all_news, list)
        self.assertEqual(len(all_news), 1)
        self.assertIsInstance(all_news[0], dict)  # NOT a NewsArticle instance
        self.assertIn("url", all_news[0])
        self.assertIn("article_id", all_news[0])

    def test_collect_all_aggregates_multiple_sources(self):
        source_a = {"name": "SourceA", "url": "http://a.test/rss", "category": "stocks"}
        source_b = {"name": "SourceB", "url": "http://b.test/rss", "category": "crypto"}
        collector = RSSCollector(feeds=[source_a, source_b])

        entry_a = SimpleNamespace(title="A1", summary="", link="http://a.test/1")
        entry_b = SimpleNamespace(title="B1", summary="", link="http://b.test/1")

        def fake_fetch(url):
            return _fake_feed([entry_a]) if url == source_a["url"] else _fake_feed([entry_b])

        with patch.object(collector, "fetch_feed", side_effect=fake_fetch):
            all_news = collector.collect_all()

        self.assertEqual(len(all_news), 2)
        sources_seen = {a["source"] for a in all_news}
        self.assertEqual(sources_seen, {"SourceA", "SourceB"})

    def test_one_broken_source_does_not_block_others(self):
        good_source = {"name": "Good", "url": "http://good.test/rss", "category": "stocks"}
        bad_source = {"name": "Bad", "url": "http://bad.test/rss", "category": "stocks"}
        collector = RSSCollector(feeds=[good_source, bad_source])

        good_entry = SimpleNamespace(title="Good article", summary="", link="http://good.test/1")

        def fake_fetch(url):
            if url == bad_source["url"]:
                raise RuntimeError("simulated network failure")
            return _fake_feed([good_entry])

        with patch.object(collector, "fetch_feed", side_effect=fake_fetch):
            all_news = collector.collect_all()

        # The failure of "Bad" must not prevent "Good" from being collected.
        self.assertEqual(len(all_news), 1)
        self.assertEqual(all_news[0]["source"], "Good")


@unittest.skip("Live network test — enable manually when you have internet access (e.g. in Colab)")
class TestRSSCollectorLive(unittest.TestCase):
    """
    LIVE integration test — disabled by default.
    Remove the @unittest.skip decorator to verify real-world feeds parse
    correctly. Not run automatically since network access is unreliable
    in sandboxed/CI environments and results would be non-deterministic.
    """

    def test_live_collection_returns_articles(self):
        collector = RSSCollector()  # uses real RSS_FEEDS from sources.py
        all_news = collector.collect_all()
        self.assertIsInstance(all_news, list)
        # No minimum-count assertion: real feed availability varies.


if __name__ == "__main__":
    unittest.main(verbosity=2)
