"""
tests/news/test_ingestion.py
---------------------------------
Tests for the provider abstraction and the ingestion pipeline:
idempotency, retry, rate limits, checkpoint/resume, and the
spec-required "provider fails halfway" scenario.
"""

import sys
import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.news_schema import initialize_news_schema
from src.data_access.news_repository import NewsRepository
from src.news.ingestion import IngestionEngine
from src.news.providers import (
    NewsProvider, FetchResult, ProviderError, RateLimitError,
    fetch_with_retry, RateLimiter, ExistingCollectorProvider,
)
from src.domain.news_models import RawArticle, ProcessingStatus

FETCHED = datetime(2026, 8, 20, 14, 35, tzinfo=timezone.utc)


#: Genuinely DISTINCT article bodies. Deliberately not templated
#: ("Article number 0/1/..."), because near-identical wording is
#: correctly flagged as duplicate content by the dedup engine — a test
#: fixture must not accidentally exercise that path when it means to
#: test something else.
_DISTINCT_BODIES = [
    ("Nvidia reports record data center revenue", "The chipmaker cited sustained AI infrastructure demand across cloud customers."),
    ("Oil prices slide as OPEC raises output quota", "Crude fell sharply after the cartel agreed to increase production targets."),
    ("Federal Reserve holds interest rates steady", "Policymakers signalled patience amid mixed inflation readings this quarter."),
    ("Tesla deliveries miss analyst expectations", "Vehicle handovers came in below consensus on softer European demand."),
    ("Gold hits fresh high on safe haven buying", "Bullion advanced as investors sought protection from currency volatility."),
    ("Shopify raises full year merchant guidance", "The commerce platform pointed to stronger subscription revenue growth."),
    ("Boeing wins long haul aircraft order", "A flag carrier committed to dozens of widebody jets over the next decade."),
    ("Bitcoin retreats after exchange outflows", "Traders reduced exposure following a sizeable withdrawal from custody wallets."),
    ("Pfizer advances late stage oncology trial", "The drugmaker reported encouraging survival data from its pivotal study."),
    ("Copper demand strengthens on grid spending", "Utilities accelerated transmission upgrades, tightening physical supply."),
]


def make_payload(i=0, title=None, url=None):
    body_title, body_summary = _DISTINCT_BODIES[i % len(_DISTINCT_BODIES)]
    return {
        "title": title or body_title,
        "description": body_summary,
        "link": url or f"https://reuters.com/story-{i}",
        "published": "2026-08-20T14:31:00Z",
        "source": "Reuters",
    }


def make_raw(i=0, provider="rss", provider_article_id=None, payload=None):
    return RawArticle(
        raw_id=f"raw-{i}", provider=provider, provider_article_id=provider_article_id,
        fetched_at=FETCHED, payload=payload or make_payload(i),
    )


class FakeProvider(NewsProvider):
    """A controllable provider: serves scripted pages, can fail on a chosen page."""

    def __init__(self, pages, name="fake", configured=True, historical=True, fail_on_page=None, fail_with=None):
        self.name = name
        self.pages = pages
        self._configured = configured
        self._historical = historical
        self.fail_on_page = fail_on_page
        self.fail_with = fail_with or ProviderError("simulated provider outage")
        self.calls = 0

    def is_configured(self):
        return self._configured

    def supports_historical(self):
        return self._historical

    def fetch(self, cursor=None, since=None, until=None, limit=100):
        page_index = int(cursor) if cursor else 0
        self.calls += 1
        if self.fail_on_page is not None and page_index == self.fail_on_page:
            raise self.fail_with
        if page_index >= len(self.pages):
            return FetchResult([], None, False)
        has_more = page_index + 1 < len(self.pages)
        return FetchResult(
            raw_articles=self.pages[page_index],
            next_cursor=str(page_index + 1) if has_more else None,
            has_more=has_more,
        )


def new_engine():
    conn = sqlite3.connect(":memory:")
    initialize_news_schema(conn)
    repo = NewsRepository(conn)
    return IngestionEngine(repo), repo


class TestRateLimiterAndRetry(unittest.TestCase):
    def test_rate_limiter_waits_between_calls(self):
        slept = []
        limiter = RateLimiter(min_interval_seconds=0.5)
        limiter.wait(sleep_fn=slept.append)   # first call: no wait
        limiter.wait(sleep_fn=slept.append)   # second: must wait
        self.assertEqual(len(slept), 1)

    def test_zero_interval_never_sleeps(self):
        slept = []
        RateLimiter(0).wait(sleep_fn=slept.append)
        self.assertEqual(slept, [])

    def test_retry_succeeds_after_transient_failure(self):
        class FlakyProvider(FakeProvider):
            def fetch(self, cursor=None, since=None, until=None, limit=100):
                self.calls += 1
                if self.calls < 3:
                    raise ProviderError("transient")
                return FetchResult([make_raw(1)], None, False)

        provider = FlakyProvider(pages=[])
        result = fetch_with_retry(provider, sleep_fn=lambda s: None)
        self.assertEqual(len(result.raw_articles), 1)
        self.assertEqual(provider.calls, 3)

    def test_retry_gives_up_after_max_attempts(self):
        provider = FakeProvider(pages=[[]], fail_on_page=0)
        with self.assertRaises(ProviderError):
            fetch_with_retry(provider, max_attempts=2, sleep_fn=lambda s: None)

    def test_rate_limit_error_is_retried_not_fatal(self):
        class RateLimitedOnce(FakeProvider):
            def fetch(self, cursor=None, since=None, until=None, limit=100):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitError("429")
                return FetchResult([make_raw(1)], None, False)

        provider = RateLimitedOnce(pages=[])
        result = fetch_with_retry(provider, sleep_fn=lambda s: None)
        self.assertEqual(len(result.raw_articles), 1)


class TestIngestOnce(unittest.TestCase):
    def setUp(self):
        self.engine, self.repo = new_engine()

    def test_ingests_and_stores_articles(self):
        provider = FakeProvider(pages=[[make_raw(0), make_raw(1)]])
        result = self.engine.ingest_once(provider)
        self.assertEqual(result["stats"].received, 2)
        self.assertEqual(result["stats"].normalized, 2)
        self.assertEqual(self.repo.count(), 2)

    def test_unconfigured_provider_is_skipped_cleanly(self):
        provider = FakeProvider(pages=[[make_raw(0)]], configured=False)
        result = self.engine.ingest_once(provider)
        self.assertEqual(result["stats"].received, 0)
        self.assertEqual(self.repo.count(), 0)

    def test_provider_failure_is_reported_not_raised(self):
        provider = FakeProvider(pages=[[make_raw(0)]], fail_on_page=0)
        result = self.engine.ingest_once(provider, sleep_fn=lambda s: None)
        self.assertEqual(result["stats"].provider_errors, 1)
        self.assertIn("error", result)

    def test_raw_payloads_are_preserved(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        self.engine.ingest_once(provider)
        self.assertIsNotNone(self.repo.get_raw("raw-0"))

    def test_invalid_articles_are_rejected_but_stored(self):
        bad = RawArticle(raw_id="bad", provider="rss", provider_article_id=None,
                          fetched_at=FETCHED, payload={"title": "x"})  # no url, no date
        provider = FakeProvider(pages=[[bad]])
        result = self.engine.ingest_once(provider)
        self.assertEqual(result["stats"].rejected, 1)
        # Stored (recoverable), just excluded from default queries.
        self.assertEqual(self.repo.count(include_rejected=True), 1)
        self.assertEqual(self.repo.count(), 0)

    def test_stats_contain_no_credentials(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        result = self.engine.ingest_once(provider)
        log_dict = result["stats"].as_log_dict()
        serialized = str(log_dict).lower()
        for forbidden in ("api_key", "apikey", "token", "password", "secret"):
            self.assertNotIn(forbidden, serialized)


class TestIdempotency(unittest.TestCase):
    """Spec §6 + §26.4: re-ingesting identical articles must never multiply rows."""

    def setUp(self):
        self.engine, self.repo = new_engine()

    def test_same_page_ingested_five_times_yields_one_row_each(self):
        page = [make_raw(0), make_raw(1)]
        for _ in range(5):
            self.engine.ingest_once(FakeProvider(pages=[page]))
        self.assertEqual(self.repo.count(), 2)

    def test_same_article_from_different_providers_is_deduplicated(self):
        shared_url = "https://reuters.com/shared-story"
        rss = make_raw(0, provider="rss", payload=make_payload(0, url=shared_url))
        finnhub = make_raw(1, provider="finnhub", provider_article_id="fh-9",
                            payload=make_payload(0, url=shared_url))

        self.engine.ingest_once(FakeProvider(pages=[[rss]], name="rss"))
        result = self.engine.ingest_once(FakeProvider(pages=[[finnhub]], name="finnhub"))

        self.assertEqual(result["stats"].duplicates_detected, 1)
        # Both rows exist, but only the original is returned by default.
        self.assertEqual(self.repo.count(include_duplicates=True), 2)
        self.assertEqual(self.repo.count(), 1)

    def test_updated_article_bumps_version_rather_than_duplicating(self):
        original = make_raw(0, provider="rss", provider_article_id="p-1")
        revised = make_raw(0, provider="rss", provider_article_id="p-1",
                            payload=make_payload(0, title="Corrected headline"))

        self.engine.ingest_once(FakeProvider(pages=[[original]]))
        result = self.engine.ingest_once(FakeProvider(pages=[[revised]]))

        self.assertEqual(result["stats"].updated, 1)
        self.assertEqual(self.repo.count(), 1)


class TestEntityLinking(unittest.TestCase):
    def setUp(self):
        self.engine, self.repo = new_engine()

    def test_links_canonical_entity_ids_to_articles(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        articles = self.engine.ingest_once(provider)["articles"]
        article_id = articles[0].article_id

        count = self.engine.link_entities(
            articles,
            company_ids_by_article={article_id: ["nvidia"]},
            sector_ids_by_article={article_id: ["technology"]},
        )
        self.assertEqual(count, 1)
        self.assertEqual(self.repo.get_entity_ids(article_id, "company"), ["nvidia"])
        self.assertEqual(self.repo.get_entity_ids(article_id, "sector"), ["technology"])

    def test_linked_articles_are_queryable_by_entity(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        articles = self.engine.ingest_once(provider)["articles"]
        self.engine.link_entities(articles, company_ids_by_article={articles[0].article_id: ["nvidia"]})

        found = self.repo.query(entity_type="company", entity_id="nvidia")
        self.assertEqual(len(found["articles"]), 1)

    def test_articles_without_supplied_entities_are_untouched(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        articles = self.engine.ingest_once(provider)["articles"]
        self.assertEqual(self.engine.link_entities(articles), 0)


class TestHistoricalImportAndResume(unittest.TestCase):
    """Spec §15 + §26.6: a halted historical import must resume, not restart."""

    def setUp(self):
        self.engine, self.repo = new_engine()

    def test_completes_a_multi_page_import(self):
        pages = [[make_raw(0), make_raw(1)], [make_raw(2)], [make_raw(3)]]
        provider = FakeProvider(pages=pages)
        checkpoint = self.engine.run_historical_import(provider, "hist-1", sleep_fn=lambda s: None)
        self.assertTrue(checkpoint.completed)
        self.assertEqual(self.repo.count(), 4)

    def test_provider_failing_halfway_saves_a_resume_position(self):
        pages = [[make_raw(0)], [make_raw(1)], [make_raw(2)]]
        failing = FakeProvider(pages=pages, fail_on_page=1)
        checkpoint = self.engine.run_historical_import(failing, "hist-2", sleep_fn=lambda s: None)

        self.assertFalse(checkpoint.completed)
        self.assertIsNotNone(checkpoint.last_error)
        self.assertEqual(checkpoint.cursor, "1")          # resume position saved
        self.assertEqual(self.repo.count(), 1)             # page 0 was kept, not rolled back

    def test_resuming_continues_from_checkpoint_instead_of_restarting(self):
        pages = [[make_raw(0)], [make_raw(1)], [make_raw(2)]]

        failing = FakeProvider(pages=pages, fail_on_page=1)
        self.engine.run_historical_import(failing, "hist-3", sleep_fn=lambda s: None)
        self.assertEqual(self.repo.count(), 1)

        # Provider recovers; the SAME checkpoint id resumes at page 1.
        recovered = FakeProvider(pages=pages)
        checkpoint = self.engine.run_historical_import(recovered, "hist-3", sleep_fn=lambda s: None)

        self.assertTrue(checkpoint.completed)
        self.assertEqual(self.repo.count(), 3)
        self.assertIsNone(checkpoint.last_error)

    def test_already_completed_import_is_a_no_op(self):
        provider = FakeProvider(pages=[[make_raw(0)]])
        self.engine.run_historical_import(provider, "hist-4", sleep_fn=lambda s: None)
        calls_after_first = provider.calls
        self.engine.run_historical_import(provider, "hist-4", sleep_fn=lambda s: None)
        self.assertEqual(provider.calls, calls_after_first)

    def test_provider_without_historical_support_is_reported_not_silently_skipped(self):
        provider = FakeProvider(pages=[[make_raw(0)]], historical=False)
        checkpoint = self.engine.run_historical_import(provider, "hist-5", sleep_fn=lambda s: None)
        self.assertFalse(checkpoint.completed)
        self.assertIn("does not support historical", checkpoint.last_error)

    def test_max_pages_bounds_one_invocation_and_stays_resumable(self):
        pages = [[make_raw(i)] for i in range(10)]
        provider = FakeProvider(pages=pages)
        checkpoint = self.engine.run_historical_import(provider, "hist-6", max_pages=3, sleep_fn=lambda s: None)
        self.assertFalse(checkpoint.completed)
        self.assertEqual(checkpoint.cursor, "3")
        self.assertEqual(self.repo.count(), 3)


class TestExistingCollectorProvider(unittest.TestCase):
    """The adapter that lets today's collectors run through the new interface, unmodified."""

    def test_wraps_a_collector_function(self):
        provider = ExistingCollectorProvider("rss", collect_fn=lambda: [make_payload(0), make_payload(1)])
        result = provider.fetch()
        self.assertEqual(len(result.raw_articles), 2)
        self.assertEqual(result.raw_articles[0].provider, "rss")

    def test_collector_exception_becomes_provider_error(self):
        def boom():
            raise RuntimeError("feed unreachable")
        provider = ExistingCollectorProvider("rss", collect_fn=boom)
        with self.assertRaises(ProviderError):
            provider.fetch()

    def test_respects_is_configured_check(self):
        provider = ExistingCollectorProvider("finnhub", collect_fn=lambda: [], is_configured_fn=lambda: False)
        self.assertFalse(provider.is_configured())

    def test_broken_configuration_check_is_treated_as_unconfigured(self):
        def broken():
            raise RuntimeError("bad config")
        provider = ExistingCollectorProvider("x", collect_fn=lambda: [], is_configured_fn=broken)
        self.assertFalse(provider.is_configured())

    def test_reports_no_pagination_honestly(self):
        provider = ExistingCollectorProvider("rss", collect_fn=lambda: [make_payload(0)])
        result = provider.fetch()
        self.assertFalse(result.has_more)
        self.assertIsNone(result.next_cursor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
