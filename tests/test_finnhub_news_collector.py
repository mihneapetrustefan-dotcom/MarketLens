"""
test_finnhub_news_collector.py
----------------------------------
Unit tests for Finnhub News Collector v1.

TESTING STRATEGY: fetch_raw() is mocked directly (exactly like every
other network call in this project), so tests are offline,
deterministic, and never touch the real Finnhub API.
"""

import unittest
from unittest.mock import patch

from finnhub_news_collector import FinnhubNewsCollector


FAKE_RESPONSE = [
    {
        "id": 123, "headline": "Tesla reports record quarterly deliveries",
        "summary": "Tesla beat estimates.", "url": "http://example.com/1",
        "source": "Reuters", "datetime": 1735689600,
    },
    {
        "id": 124, "headline": "Tesla announces new factory location",
        "summary": "Expansion plans.", "url": "http://example.com/2",
        "source": "CNBC", "datetime": 1735776000,
    },
]


class TestIsConfigured(unittest.TestCase):
    def test_with_api_key_is_configured(self):
        self.assertTrue(FinnhubNewsCollector(api_key="abc").is_configured())

    def test_without_api_key_is_not_configured(self):
        self.assertFalse(FinnhubNewsCollector(api_key=None).is_configured())


class TestCollectForTicker(unittest.TestCase):
    def setUp(self):
        self.collector = FinnhubNewsCollector(api_key="fake-key")

    def test_parses_articles_into_standard_schema(self):
        with patch.object(self.collector, "fetch_raw", return_value=FAKE_RESPONSE):
            articles = self.collector.collect_for_ticker("TSLA")

        self.assertEqual(len(articles), 2)
        article = articles[0]
        for field in ("article_id", "title", "summary", "url", "source", "category", "published_at", "collected_at"):
            self.assertIn(field, article)
        self.assertEqual(article["title"], "Tesla reports record quarterly deliveries")

    def test_not_configured_returns_empty_without_network_call(self):
        collector = FinnhubNewsCollector(api_key=None)
        with patch.object(collector, "fetch_raw") as mock_fetch:
            articles = collector.collect_for_ticker("TSLA")
        self.assertEqual(articles, [])
        mock_fetch.assert_not_called()

    def test_fetch_exception_returns_empty_list_gracefully(self):
        with patch.object(self.collector, "fetch_raw", side_effect=RuntimeError("network down")):
            articles = self.collector.collect_for_ticker("TSLA")
        self.assertEqual(articles, [])

    def test_unexpected_response_shape_returns_empty_list(self):
        with patch.object(self.collector, "fetch_raw", return_value={"error": "bad request"}):
            articles = self.collector.collect_for_ticker("TSLA")
        self.assertEqual(articles, [])

    def test_items_missing_headline_or_url_are_skipped(self):
        malformed = [{"id": 1, "headline": None, "url": "http://x"}, {"id": 2, "headline": "Real", "url": None}]
        with patch.object(self.collector, "fetch_raw", return_value=malformed):
            articles = self.collector.collect_for_ticker("TSLA")
        self.assertEqual(articles, [])

    def test_fetch_exception_from_bad_datetime_handled_gracefully(self):
        with patch.object(self.collector, "fetch_raw", side_effect=TypeError("bad datetime")):
            articles = self.collector.collect_for_ticker("TSLA")
        self.assertEqual(articles, [])


class TestCollectBatch(unittest.TestCase):
    def setUp(self):
        self.collector = FinnhubNewsCollector(api_key="fake-key")

    def test_fetches_multiple_tickers(self):
        with patch.object(self.collector, "fetch_raw", return_value=FAKE_RESPONSE):
            articles = self.collector.collect_batch(["TSLA", "AAPL"])
        self.assertEqual(len(articles), 4)  # 2 articles x 2 tickers

    def test_one_failing_ticker_does_not_block_others(self):
        def fake_fetch(ticker, date_from, date_to):
            if ticker == "BAD":
                raise RuntimeError("simulated failure")
            return FAKE_RESPONSE

        with patch.object(self.collector, "fetch_raw", side_effect=fake_fetch):
            articles = self.collector.collect_batch(["GOOD", "BAD"])
        self.assertEqual(len(articles), 2)  # only GOOD's articles

    def test_empty_ticker_list_returns_empty(self):
        self.assertEqual(self.collector.collect_batch([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
