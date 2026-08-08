"""
test_alpha_vantage_news_collector.py
----------------------------------------
Unit tests for Alpha Vantage News Collector v1.

TESTING STRATEGY: fetch_raw() is mocked directly (exactly like every
other network call in this project), so tests are offline,
deterministic, and never touch the real Alpha Vantage API (whose free
quota is only 25 requests/day — precious even for accidental testing).
"""

import unittest
from unittest.mock import patch

from alpha_vantage_news_collector import AlphaVantageNewsCollector


FAKE_RESPONSE = {
    "feed": [
        {
            "title": "Tesla shares rise on delivery beat", "url": "http://example.com/1",
            "summary": "Tesla beat delivery estimates.", "source": "Reuters",
            "time_published": "20260801T090000",
        },
        {
            "title": "Apple unveils new product roadmap", "url": "http://example.com/2",
            "summary": "New products coming.", "source": "CNBC",
            "time_published": "20260802T100000",
        },
    ]
}


class TestIsConfigured(unittest.TestCase):
    def test_with_api_key_is_configured(self):
        self.assertTrue(AlphaVantageNewsCollector(api_key="abc").is_configured())

    def test_without_api_key_is_not_configured(self):
        self.assertFalse(AlphaVantageNewsCollector(api_key=None).is_configured())


class TestCollectBatch(unittest.TestCase):
    def setUp(self):
        self.collector = AlphaVantageNewsCollector(api_key="fake-key")

    def test_sends_all_tickers_in_a_single_call(self):
        with patch.object(self.collector, "fetch_raw", return_value=FAKE_RESPONSE) as mock_fetch:
            self.collector.collect_batch(["TSLA", "AAPL", "MSFT"])
        mock_fetch.assert_called_once()  # exactly ONE API call for the whole batch
        called_with = mock_fetch.call_args[0][0]
        self.assertIn("TSLA", called_with)
        self.assertIn("AAPL", called_with)
        self.assertIn("MSFT", called_with)

    def test_parses_articles_into_standard_schema(self):
        with patch.object(self.collector, "fetch_raw", return_value=FAKE_RESPONSE):
            articles = self.collector.collect_batch(["TSLA"])

        self.assertEqual(len(articles), 2)
        article = articles[0]
        for field in ("article_id", "title", "summary", "url", "source", "category", "published_at", "collected_at"):
            self.assertIn(field, article)
        self.assertEqual(article["title"], "Tesla shares rise on delivery beat")

    def test_not_configured_returns_empty_without_network_call(self):
        collector = AlphaVantageNewsCollector(api_key=None)
        with patch.object(collector, "fetch_raw") as mock_fetch:
            articles = collector.collect_batch(["TSLA"])
        self.assertEqual(articles, [])
        mock_fetch.assert_not_called()

    def test_empty_ticker_list_returns_empty_without_network_call(self):
        with patch.object(self.collector, "fetch_raw") as mock_fetch:
            articles = self.collector.collect_batch([])
        self.assertEqual(articles, [])
        mock_fetch.assert_not_called()

    def test_fetch_exception_returns_empty_list_gracefully(self):
        with patch.object(self.collector, "fetch_raw", side_effect=RuntimeError("network down")):
            articles = self.collector.collect_batch(["TSLA"])
        self.assertEqual(articles, [])

    def test_quota_exhausted_response_returns_empty_list_not_crash(self):
        quota_response = {"Information": "You have exceeded the daily rate limit."}
        with patch.object(self.collector, "fetch_raw", return_value=quota_response):
            articles = self.collector.collect_batch(["TSLA"])
        self.assertEqual(articles, [])

    def test_items_missing_title_or_url_are_skipped(self):
        malformed = {"feed": [{"title": None, "url": "http://x"}, {"title": "Real", "url": None}]}
        with patch.object(self.collector, "fetch_raw", return_value=malformed):
            articles = self.collector.collect_batch(["TSLA"])
        self.assertEqual(articles, [])

    def test_invalid_time_published_falls_back_to_none(self):
        malformed = {"feed": [{"title": "Title", "url": "http://x", "time_published": "not-a-date"}]}
        with patch.object(self.collector, "fetch_raw", return_value=malformed):
            articles = self.collector.collect_batch(["TSLA"])
        self.assertEqual(len(articles), 1)
        self.assertIsNone(articles[0]["published_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
