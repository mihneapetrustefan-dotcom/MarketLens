"""
tests/news/test_news_repository.py
---------------------------------------
Tests for the news storage layer: idempotent upsert, update
detection/versioning, bounded dedup candidates, entity links, and the
internal query API with cursor pagination.
"""

import sys
import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.news_schema import initialize_news_schema
from src.data_access.news_repository import NewsRepository
from src.domain.news_models import (
    RawArticle, NormalizedArticle, ProcessingStatus, DuplicateMatchLevel, IngestionCheckpoint,
)

PUB = datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 20, 14, 35, tzinfo=timezone.utc)


def make_article(article_id="a1", title="Nvidia beats", summary="Strong demand.",
                 source_name="Reuters", published_at=PUB, provider="rss",
                 canonical_url="https://reuters.com/a", status=ProcessingStatus.NORMALIZED):
    return NormalizedArticle(
        article_id=article_id, provider=provider, title=title, summary=summary,
        source_name=source_name, canonical_url=canonical_url,
        published_at=published_at, ingested_at=FETCHED, processing_status=status,
    )


def new_repo():
    conn = sqlite3.connect(":memory:")
    initialize_news_schema(conn)
    return NewsRepository(conn)


class TestRawArticleStorage(unittest.TestCase):
    def test_save_and_get_raw_preserves_payload(self):
        repo = new_repo()
        raw = RawArticle(raw_id="r1", provider="finnhub", provider_article_id="fh-1",
                          fetched_at=FETCHED, payload={"headline": "X", "nested": {"a": 1}})
        repo.save_raw(raw)
        loaded = repo.get_raw("r1")
        self.assertEqual(loaded.payload["nested"]["a"], 1)
        self.assertEqual(loaded.fetched_at, FETCHED)

    def test_get_missing_raw_returns_none(self):
        self.assertIsNone(new_repo().get_raw("nope"))


class TestIdempotentUpsert(unittest.TestCase):
    """Spec §6: the same article arriving repeatedly must not create multiple rows."""

    def setUp(self):
        self.repo = new_repo()

    def test_first_insert_reports_inserted(self):
        self.assertEqual(self.repo.upsert(make_article()), "inserted")

    def test_identical_reingest_is_unchanged_and_creates_no_duplicate_row(self):
        self.repo.upsert(make_article())
        result = self.repo.upsert(make_article())
        self.assertEqual(result, "unchanged")
        self.assertEqual(self.repo.count(), 1)

    def test_five_identical_ingests_yield_one_row(self):
        for _ in range(5):
            self.repo.upsert(make_article())
        self.assertEqual(self.repo.count(), 1)

    def test_round_trip_preserves_all_key_fields(self):
        article = make_article()
        article.sentiment_label = "positive"
        article.sentiment_score = Decimal("0.75")
        article.impact_score = Decimal("0.60")
        self.repo.upsert(article)
        loaded = self.repo.get("a1")
        self.assertEqual(loaded.sentiment_label, "positive")
        self.assertEqual(loaded.sentiment_score, Decimal("0.75"))
        self.assertEqual(loaded.published_at, PUB)
        self.assertEqual(loaded.ingested_at, FETCHED)


class TestArticleUpdates(unittest.TestCase):
    """Spec §8: an updated article is a new VERSION, not an unrelated new article."""

    def setUp(self):
        self.repo = new_repo()

    def test_changed_title_reports_updated_and_bumps_version(self):
        self.repo.upsert(make_article(title="Original headline"))
        result = self.repo.upsert(make_article(title="Corrected headline"))
        self.assertEqual(result, "updated")
        loaded = self.repo.get("a1")
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.title, "Corrected headline")

    def test_update_sets_updated_at_but_preserves_original_publication_time(self):
        self.repo.upsert(make_article(title="Original"))
        later = make_article(title="Revised")
        later.ingested_at = FETCHED + timedelta(hours=2)
        self.repo.upsert(later)

        loaded = self.repo.get("a1")
        self.assertEqual(loaded.published_at, PUB)          # original publication time preserved
        self.assertEqual(loaded.ingested_at, FETCHED)        # original first-seen time preserved
        self.assertIsNotNone(loaded.updated_at)              # revision time recorded separately

    def test_update_still_yields_only_one_row(self):
        self.repo.upsert(make_article(title="A"))
        self.repo.upsert(make_article(title="B"))
        self.assertEqual(self.repo.count(), 1)


class TestDedupCandidates(unittest.TestCase):
    """The candidate set must be BOUNDED — dedup never scans the whole table."""

    def setUp(self):
        self.repo = new_repo()

    def test_only_returns_articles_within_the_time_window(self):
        self.repo.upsert(make_article("same-day", published_at=PUB + timedelta(hours=3)))
        self.repo.upsert(make_article("far-past", published_at=PUB - timedelta(days=30)))
        candidates = self.repo.find_dedup_candidates(make_article("new"))
        ids = {c.article_id for c in candidates}
        self.assertIn("same-day", ids)
        self.assertNotIn("far-past", ids)

    def test_excludes_the_article_itself(self):
        self.repo.upsert(make_article("a1"))
        candidates = self.repo.find_dedup_candidates(make_article("a1"))
        self.assertEqual(candidates, [])

    def test_article_without_publication_time_returns_no_candidates(self):
        article = make_article("no-date")
        article.published_at = None
        self.assertEqual(self.repo.find_dedup_candidates(article), [])


class TestEntityLinking(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()
        self.repo.upsert(make_article("a1"))

    def test_link_and_retrieve_company_ids(self):
        self.repo.link_entities("a1", "company", ["nvidia", "amd"])
        self.assertEqual(set(self.repo.get_entity_ids("a1", "company")), {"nvidia", "amd"})

    def test_linking_is_idempotent(self):
        self.repo.link_entities("a1", "company", ["nvidia"])
        self.repo.link_entities("a1", "company", ["nvidia"])
        self.assertEqual(self.repo.get_entity_ids("a1", "company"), ["nvidia"])

    def test_entity_types_are_kept_separate(self):
        self.repo.link_entities("a1", "company", ["nvidia"])
        self.repo.link_entities("a1", "sector", ["technology"])
        self.assertEqual(self.repo.get_entity_ids("a1", "company"), ["nvidia"])
        self.assertEqual(self.repo.get_entity_ids("a1", "sector"), ["technology"])


class TestInternalQueryApi(unittest.TestCase):
    def setUp(self):
        self.repo = new_repo()
        for i in range(10):
            self.repo.upsert(make_article(
                f"a{i}", title=f"Article {i}",
                published_at=PUB - timedelta(hours=i),
                source_name="Reuters" if i % 2 == 0 else "CNBC",
                canonical_url=f"https://x.com/{i}",
            ))

    def test_returns_newest_first(self):
        result = self.repo.query(limit=3)
        times = [a.published_at for a in result["articles"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_limit_is_respected(self):
        self.assertEqual(len(self.repo.query(limit=4)["articles"]), 4)

    def test_cursor_pagination_walks_the_whole_set_without_repeats(self):
        seen, cursor = [], None
        while True:
            page = self.repo.query(limit=3, cursor=cursor)
            seen.extend(a.article_id for a in page["articles"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 10)  # no article returned twice

    def test_filter_by_source(self):
        result = self.repo.query(source_name="CNBC", limit=50)
        self.assertTrue(all(a.source_name == "CNBC" for a in result["articles"]))
        self.assertEqual(len(result["articles"]), 5)

    def test_filter_by_date_range(self):
        result = self.repo.query(published_after=PUB - timedelta(hours=3), limit=50)
        self.assertEqual(len(result["articles"]), 4)

    def test_search_filters_title(self):
        result = self.repo.query(search="Article 7", limit=50)
        self.assertEqual(len(result["articles"]), 1)

    def test_filter_by_linked_entity(self):
        self.repo.link_entities("a3", "company", ["nvidia"])
        result = self.repo.query(entity_type="company", entity_id="nvidia", limit=50)
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0].article_id, "a3")

    def test_duplicates_excluded_by_default(self):
        dupe = make_article("dupe", canonical_url="https://x.com/dupe")
        dupe.duplicate_of = "a0"
        self.repo.upsert(dupe)
        default = self.repo.query(limit=50)
        with_dupes = self.repo.query(limit=50, include_duplicates=True)
        self.assertEqual(len(default["articles"]), 10)
        self.assertEqual(len(with_dupes["articles"]), 11)

    def test_rejected_articles_excluded_by_default_but_retrievable(self):
        rejected = make_article("bad", canonical_url="https://x.com/bad", status=ProcessingStatus.REJECTED)
        self.repo.upsert(rejected)
        self.assertEqual(len(self.repo.query(limit=50)["articles"]), 10)
        self.assertEqual(len(self.repo.query(limit=50, include_rejected=True)["articles"]), 11)

    def test_limit_is_hard_capped(self):
        result = self.repo.query(limit=99999)
        self.assertLessEqual(len(result["articles"]), 500)

    def test_count_by_source(self):
        counts = self.repo.count_by_source()
        self.assertEqual(counts["Reuters"], 5)
        self.assertEqual(counts["CNBC"], 5)


class TestCheckpoints(unittest.TestCase):
    def test_save_and_resume_checkpoint(self):
        repo = new_repo()
        checkpoint = IngestionCheckpoint(
            checkpoint_id="hist-rss-2026-08", provider="rss",
            period_start=PUB - timedelta(days=30), period_end=PUB,
            cursor="page-7", articles_ingested=350, last_updated_at=FETCHED,
        )
        repo.save_checkpoint(checkpoint)
        loaded = repo.get_checkpoint("hist-rss-2026-08")
        self.assertEqual(loaded.cursor, "page-7")
        self.assertEqual(loaded.articles_ingested, 350)
        self.assertFalse(loaded.completed)

    def test_missing_checkpoint_returns_none(self):
        self.assertIsNone(new_repo().get_checkpoint("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
