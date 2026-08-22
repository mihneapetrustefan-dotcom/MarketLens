"""
tests/news/test_normalizer.py
----------------------------------
Tests for RawArticle -> NormalizedArticle conversion, provider field
mapping, validation, and deterministic id generation.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.news.normalizer import ArticleNormalizer, parse_timestamp
from src.domain.news_models import RawArticle, ProcessingStatus

FETCHED = datetime(2026, 8, 20, 14, 35, tzinfo=timezone.utc)


def make_raw(provider="rss", provider_article_id=None, payload=None, raw_id="r1", fetched_at=FETCHED):
    return RawArticle(
        raw_id=raw_id, provider=provider, provider_article_id=provider_article_id,
        fetched_at=fetched_at, payload=payload or {},
    )


VALID_RSS_PAYLOAD = {
    "title": "Nvidia reports record quarterly revenue",
    "description": "Nvidia beat estimates on AI chip demand.",
    "link": "https://reuters.com/nvidia-q2",
    "published": "2026-08-20T14:31:00Z",
    "source": "Reuters",
}


class TestParseTimestamp(unittest.TestCase):
    def test_iso_with_z(self):
        self.assertEqual(parse_timestamp("2026-08-20T14:31:00Z"),
                          datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc))

    def test_iso_with_offset_converted_to_utc(self):
        result = parse_timestamp("2026-08-20T17:31:00+03:00")
        self.assertEqual(result, datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc))

    def test_unix_epoch_seconds(self):
        # Finnhub's format.
        epoch = int(datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parse_timestamp(epoch), datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc))

    def test_naive_datetime_treated_as_utc(self):
        result = parse_timestamp(datetime(2026, 8, 20, 14, 31))
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_unparseable_returns_none_not_error(self):
        self.assertIsNone(parse_timestamp("not-a-date"))
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))


class TestNormalizationHappyPath(unittest.TestCase):
    def setUp(self):
        self.normalizer = ArticleNormalizer()

    def test_valid_rss_article_normalizes(self):
        article = self.normalizer.normalize(make_raw(payload=VALID_RSS_PAYLOAD))
        self.assertEqual(article.processing_status, ProcessingStatus.NORMALIZED)
        self.assertEqual(article.title, "Nvidia reports record quarterly revenue")
        self.assertEqual(article.source_name, "Reuters")

    def test_publication_and_ingestion_times_kept_distinct(self):
        """The core look-ahead-bias protection (spec §9)."""
        article = self.normalizer.normalize(make_raw(payload=VALID_RSS_PAYLOAD))
        self.assertEqual(article.published_at, datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc))
        self.assertEqual(article.ingested_at, FETCHED)
        self.assertNotEqual(article.published_at, article.ingested_at)

    def test_canonical_url_computed(self):
        payload = dict(VALID_RSS_PAYLOAD, link="https://www.reuters.com/nvidia-q2?utm_source=x")
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.canonical_url, "https://reuters.com/nvidia-q2")

    def test_fingerprints_computed_for_valid_article(self):
        article = self.normalizer.normalize(make_raw(payload=VALID_RSS_PAYLOAD))
        self.assertIsNotNone(article.fingerprint)
        self.assertIsNotNone(article.content_fingerprint)


class TestProviderFieldMapping(unittest.TestCase):
    """Each provider's own field names map to the same canonical shape — no provider-specific logic leaks downstream."""

    def setUp(self):
        self.normalizer = ArticleNormalizer()

    def test_finnhub_headline_and_epoch_datetime(self):
        payload = {
            "headline": "Nvidia beats estimates",
            "summary": "Strong AI demand.",
            "url": "https://finnhub.io/news/1",
            "datetime": int(datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc).timestamp()),
            "source": "Reuters",
        }
        article = self.normalizer.normalize(make_raw(provider="finnhub", payload=payload))
        self.assertEqual(article.title, "Nvidia beats estimates")
        self.assertEqual(article.published_at, datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc))

    def test_alpha_vantage_authors_list_flattened(self):
        payload = {
            "title": "Market wrap",
            "summary": "Stocks rose.",
            "url": "https://av.co/news/1",
            "time_published": "2026-08-20T14:31:00Z",
            "source": "Alpha Vantage",
            "authors": ["Jane Doe", "John Roe"],
        }
        article = self.normalizer.normalize(make_raw(provider="alpha_vantage", payload=payload))
        self.assertEqual(article.author, "Jane Doe, John Roe")

    def test_unknown_provider_falls_back_to_generic_mapping(self):
        article = self.normalizer.normalize(make_raw(provider="some_new_provider", payload=VALID_RSS_PAYLOAD))
        self.assertEqual(article.title, "Nvidia reports record quarterly revenue")

    def test_known_provider_falls_back_to_generic_keys_when_its_own_are_absent(self):
        """
        A provider that renames a response field must degrade
        gracefully rather than causing its whole feed to be rejected —
        here 'finnhub' is declared but the payload uses generic keys.
        """
        article = self.normalizer.normalize(make_raw(provider="finnhub", payload=VALID_RSS_PAYLOAD))
        self.assertEqual(article.processing_status, ProcessingStatus.NORMALIZED)
        self.assertEqual(article.canonical_url, "https://reuters.com/nvidia-q2")

    def test_provider_own_keys_take_priority_over_generic_ones(self):
        payload = {
            "headline": "Finnhub headline", "title": "Generic title",
            "url": "https://finnhub.io/a", "datetime": 1787000000, "source": "Reuters",
        }
        article = self.normalizer.normalize(make_raw(provider="finnhub", payload=payload))
        self.assertEqual(article.title, "Finnhub headline")


class TestValidationRejectsCorruptData(unittest.TestCase):
    """Corrupt data is FLAGGED and kept, never silently accepted or discarded."""

    def setUp(self):
        self.normalizer = ArticleNormalizer()

    def test_missing_title_rejected(self):
        payload = dict(VALID_RSS_PAYLOAD); payload.pop("title")
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.processing_status, ProcessingStatus.REJECTED)
        self.assertIn("title", article.rejection_reason)

    def test_missing_url_rejected(self):
        payload = dict(VALID_RSS_PAYLOAD); payload.pop("link")
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.processing_status, ProcessingStatus.REJECTED)
        self.assertIn("url", article.rejection_reason)

    def test_missing_timestamp_rejected(self):
        payload = dict(VALID_RSS_PAYLOAD); payload.pop("published")
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.processing_status, ProcessingStatus.REJECTED)
        self.assertIn("timestamp", article.rejection_reason)

    def test_future_publication_timestamp_flagged_as_clock_error(self):
        payload = dict(VALID_RSS_PAYLOAD, published="2026-09-20T14:31:00Z")  # a month after fetch
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.processing_status, ProcessingStatus.REJECTED)
        self.assertIn("clock error", article.rejection_reason)

    def test_rejected_article_is_still_returned_not_dropped(self):
        article = self.normalizer.normalize(make_raw(payload={}))
        self.assertIsNotNone(article)
        self.assertIsNotNone(article.article_id)

    def test_slightly_future_timestamp_within_tolerance_accepted(self):
        # Minor clock skew between provider and us is normal, not an error.
        payload = dict(VALID_RSS_PAYLOAD, published="2026-08-20T14:40:00Z")  # 5 min after fetch
        article = self.normalizer.normalize(make_raw(payload=payload))
        self.assertEqual(article.processing_status, ProcessingStatus.NORMALIZED)


class TestDeterministicIds(unittest.TestCase):
    """Deterministic ids are what make re-ingestion idempotent (spec §6)."""

    def setUp(self):
        self.normalizer = ArticleNormalizer()

    def test_same_provider_id_yields_same_article_id(self):
        a = self.normalizer.normalize(make_raw(provider="finnhub", provider_article_id="fh-1", payload=VALID_RSS_PAYLOAD))
        b = self.normalizer.normalize(make_raw(provider="finnhub", provider_article_id="fh-1", payload=VALID_RSS_PAYLOAD, raw_id="r2"))
        self.assertEqual(a.article_id, b.article_id)

    def test_same_url_yields_same_article_id_without_provider_id(self):
        a = self.normalizer.normalize(make_raw(payload=VALID_RSS_PAYLOAD))
        b = self.normalizer.normalize(make_raw(payload=dict(VALID_RSS_PAYLOAD, link="https://www.reuters.com/nvidia-q2/"), raw_id="r2"))
        self.assertEqual(a.article_id, b.article_id)

    def test_different_articles_yield_different_ids(self):
        a = self.normalizer.normalize(make_raw(payload=VALID_RSS_PAYLOAD))
        b = self.normalizer.normalize(make_raw(payload=dict(VALID_RSS_PAYLOAD, link="https://reuters.com/other")))
        self.assertNotEqual(a.article_id, b.article_id)


class TestBatchNormalization(unittest.TestCase):
    def test_batch_resolves_source_ids_by_name(self):
        normalizer = ArticleNormalizer()
        raws = [make_raw(payload=VALID_RSS_PAYLOAD)]
        results = normalizer.normalize_batch(raws, source_id_by_name={"Reuters": "reuters"})
        self.assertEqual(results[0].source_id, "reuters")

    def test_batch_handles_mixed_valid_and_invalid(self):
        normalizer = ArticleNormalizer()
        raws = [make_raw(payload=VALID_RSS_PAYLOAD), make_raw(payload={}, raw_id="r2")]
        results = normalizer.normalize_batch(raws)
        self.assertEqual(len(results), 2)
        statuses = {r.processing_status for r in results}
        self.assertEqual(statuses, {ProcessingStatus.NORMALIZED, ProcessingStatus.REJECTED})


if __name__ == "__main__":
    unittest.main(verbosity=2)
