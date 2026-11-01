"""
tests/scripts/test_compute_features_archive.py
-----------------------------------------------------------
Tests that archived articles reach Phase 8 feature computation.

THE PRODUCTION FAILURE THIS REPRODUCES
--------------------------------------
`archive_old_articles.py --keep-days 60` moves old articles out of the
live `articles` table. Every news- and sentiment-derived feature read
`FROM articles`, so an archived article was indistinguishable from one
that never existed. Measured on 2026-11-01 production output
(docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md):

    sentiment.mean_7d          581/581 = 100%  ->  133/1295 = 10.3%
    news.source_diversity_7d   581/581 = 100%  ->  133/1295 = 10.3%
    sentiment.dispersion_7d    572/581 =  98%  ->  111/1295 =  8.6%

while every price-derived feature held at ~96%. Under
`_clean_rows`'s listwise deletion over the union of feature columns,
the rarest column caps complete-case rows, so the design matrix lost
every usable row and `fit_ridge` returned `insufficient_data`.

The first test below is that failure in miniature: one observation,
articles only in the archive, and a sentiment feature that is null
without the archive reader and populated with it.

WHAT MUST NOT CHANGE
--------------------
A missing value must stay missing. Reading the archive adds ARTICLES,
never values: an article with no parseable sentiment still contributes
no score, and a feature with nothing to compute from is still null
rather than zero.
"""

import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.compute_features import (
    FEATURE_SOURCE, load_archived_articles, load_entity_articles, main,
)
from src.data_access.article_archive import ArticleArchive
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.news_schema import initialize_news_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.research_schema import initialize_research_schema
from src.data_access.schema import initialize_schema
from src.news_database import NewsDatabase

ANCHOR = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)

#: Features that can only be computed from articles. These are the
#: ones that collapsed in production.
ARTICLE_FEATURES = ("sentiment.mean_7d", "sentiment.dispersion_7d",
                    "news.source_diversity_7d")


def archived_record(article_id, published_at, source="Reuters", score=0.3):
    return {
        "article_id": article_id, "url": f"http://x/{article_id}",
        "title": "T", "summary": "S", "source": source,
        "published_at": published_at, "collected_at": published_at,
        "stored_at": published_at,
        "sentiment": None if score is None else json.dumps(
            {"score": score, "label": "positive", "confidence": 0.6}),
    }


class ArchiveFeatureFixture(unittest.TestCase):
    """
    A database with candles, one observation and article->company
    links, but NO live article rows — exactly the shape production is
    in after 60-day archiving.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db_path = os.path.join(self.root, "marketlens.db")
        self.archive_dir = os.path.join(self.root, "archives")
        NewsDatabase(self.db_path).close()
        conn = sqlite3.connect(self.db_path)
        for fn in (initialize_schema, initialize_news_schema,
                   initialize_fusion_schema, initialize_price_cache_schema,
                   initialize_research_schema):
            fn(conn)

        conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id,name,country) "
                     "VALUES ('US_AND_INTL','X','US')")
        conn.execute("INSERT OR IGNORE INTO companies (company_id, canonical_name) "
                     "VALUES ('nvidia','NVIDIA')")
        conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, instrument_type) "
                     "VALUES ('nvidia-common','nvidia','common_stock')")
        conn.execute("INSERT OR IGNORE INTO instruments "
                     "(instrument_id, security_id, exchange_id, ticker, asset_class) "
                     "VALUES ('inst-nvda','nvidia-common','US_AND_INTL','NVDA','stock')")

        for i in range(-120, 11):
            ts = (ANCHOR + timedelta(days=i)).replace(hour=20, minute=0)
            price = 100.0 + i * 0.15 + (i % 7) * 0.4
            conn.execute("""INSERT INTO price_candle_cache
                (instrument_id, interval, timestamp, open, high, low, close,
                 adjusted_close, volume, source, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("inst-nvda", "1d", ts.isoformat(), price, price + 1, price - 1,
                 price, price, 1_000_000 + (i % 5) * 50_000, "test", "now"))

        conn.execute("""INSERT INTO canonical_events
            (canonical_event_id, event_type, category, lifecycle_state,
             corroboration_state, first_reported_at, independent_source_count,
             quality_confidence)
            VALUES ('ce-1','acquisition','corporate_action','reported',
                    'single_source',?,2,0.8)""", (ANCHOR.isoformat(),))
        conn.execute("INSERT INTO canonical_event_participants "
                     "(canonical_event_id, entity_id, role) VALUES ('ce-1','nvidia','primary')")

        # The links survive archiving. This is what makes the repair
        # possible: the database still knows WHICH articles to ask the
        # archive for.
        self.archived_ids = [f"old-{k}" for k in range(5)]
        for article_id in self.archived_ids:
            conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                         "VALUES (?,'company','nvidia')", (article_id,))

        conn.execute("""INSERT INTO research_observations
            (observation_id, event_id, instrument_id, observation_created_at,
             information_cutoff, event_cluster_id, quality_level, dataset_version,
             event_type)
            VALUES ('obs-1','ce-1','inst-nvda',?,?,'inst-nvda','high','v1','acquisition')""",
            (ANCHOR.isoformat(), ANCHOR.isoformat()))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_archive(self, records, month="2026-08", raw=None):
        os.makedirs(self.archive_dir, exist_ok=True)
        path = os.path.join(self.archive_dir, f"articles_{month}.jsonl.gz")
        if raw is not None:
            with open(path, "wb") as handle:
                handle.write(raw)
            return path
        with gzip.open(path, "at", encoding="utf-8") as handle:
            for rec in records:
                handle.write(json.dumps(rec) + "\n")
        return path

    def seed_archive(self, score=0.3, sources=("Reuters", "CNBC", "Bloomberg", "FT", "WSJ")):
        """Five archived articles inside the 7-day window before cutoff."""
        records = []
        for k, article_id in enumerate(self.archived_ids):
            published = (ANCHOR - timedelta(days=k % 5, hours=2)).isoformat()
            records.append(archived_record(
                article_id, published, source=sources[k % len(sources)],
                score=None if score is None else score + k * 0.05))
        return self.write_archive(records)

    def run_features(self, extra=()):
        argv = sys.argv
        sys.argv = ["compute_features.py", "--db", self.db_path] + list(extra)
        try:
            return main()
        finally:
            sys.argv = argv

    def feature_values(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                name: json.loads(value) if value is not None else None
                for name, value in conn.execute(
                    "SELECT qualified_name, value_json FROM research_features "
                    "WHERE observation_id = 'obs-1' AND source = ?", (FEATURE_SOURCE,))
            }
        finally:
            conn.close()


class TestArchivedArticlesRestoreFeatures(ArchiveFeatureFixture):
    def test_without_the_archive_article_features_are_null(self):
        # The production failure, reproduced. Not an error — a null.
        self.assertEqual(self.run_features(["--apply"]), 0)
        values = self.feature_values()
        for name in ARTICLE_FEATURES:
            self.assertIn(name, values, f"{name} should be attempted")
            self.assertIsNone(values[name],
                              f"{name} should be null with no articles at all")

    def test_with_the_archive_article_features_are_populated(self):
        self.seed_archive()
        self.assertEqual(self.run_features(["--apply"]), 0)
        values = self.feature_values()
        for name in ARTICLE_FEATURES:
            self.assertIsNotNone(
                values.get(name),
                f"{name} is still null even though its articles are archived")

    def test_price_features_are_unaffected_either_way(self):
        # The control: price-derived features held at ~96% in
        # production and must not move because of this change.
        self.assertEqual(self.run_features(["--apply"]), 0)
        without = self.feature_values()
        self.seed_archive()
        self.assertEqual(self.run_features(["--apply"]), 0)
        with_archive = self.feature_values()
        for name in ("technical.rsi_14", "market.return_20d", "volatility.realized_20d"):
            self.assertEqual(without.get(name), with_archive.get(name), name)

    def test_source_diversity_reflects_the_archived_sources(self):
        # The feature is a RATIO of distinct sources to articles, so
        # five archived articles from two distinct sources give 0.4.
        # Asserting the exact value proves the archived rows reached
        # the feature with their sources intact, not merely that
        # something non-null came out.
        self.seed_archive(sources=("Reuters", "CNBC", "Reuters", "CNBC", "Reuters"))
        self.assertEqual(self.run_features(["--apply"]), 0)
        diversity = self.feature_values().get("news.source_diversity_7d")
        self.assertIsNotNone(diversity)
        self.assertAlmostEqual(diversity, 2 / 5)

    def test_the_archive_does_not_bypass_the_cutoff(self):
        # Leakage discipline is FeatureContext's job and must still
        # apply to articles that arrive from the archive.
        future = [archived_record(article_id,
                                  (ANCHOR + timedelta(days=30 + k)).isoformat())
                  for k, article_id in enumerate(self.archived_ids)]
        self.write_archive(future)
        self.assertEqual(self.run_features(["--apply"]), 0)
        values = self.feature_values()
        for name in ARTICLE_FEATURES:
            self.assertIsNone(values.get(name),
                              f"{name} used an article published after the cutoff")


class TestMissingValuesStayMissing(ArchiveFeatureFixture):
    def test_archived_articles_without_sentiment_give_no_score_not_zero(self):
        self.seed_archive(score=None)
        self.assertEqual(self.run_features(["--apply"]), 0)
        values = self.feature_values()
        self.assertIsNone(values.get("sentiment.mean_7d"),
                          "a mean over no scores must be null, never 0.0")

    def test_article_counts_still_compute_without_sentiment(self):
        # A count over an empty set is legitimately zero; a mean is
        # not. This asymmetry is why coverage collapsed unevenly in
        # production and it must be preserved.
        self.seed_archive(score=None)
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertIsNotNone(self.feature_values().get("news.article_count_7d"))

    def test_a_single_archived_article_leaves_dispersion_null(self):
        self.write_archive([archived_record(
            self.archived_ids[0], (ANCHOR - timedelta(days=1)).isoformat())])
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertIsNone(self.feature_values().get("sentiment.dispersion_7d"),
                          "dispersion of one observation is undefined, not zero")

    def test_an_archived_article_with_no_timestamp_is_skipped(self):
        self.write_archive([dict(archived_record(self.archived_ids[0],
                                                 ANCHOR.isoformat()),
                                 published_at=None)])
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertIsNone(self.feature_values().get("sentiment.mean_7d"))


class TestDamagedArchiveDoesNotBreakTheRun(ArchiveFeatureFixture):
    def test_absent_archive_directory_is_not_fatal(self):
        self.assertFalse(os.path.isdir(self.archive_dir))
        self.assertEqual(self.run_features(["--apply"]), 0)

    def test_file_that_is_not_gzip_is_not_fatal(self):
        self.write_archive(None, raw=b"this is not gzip")
        self.assertEqual(self.run_features(["--apply"]), 0)

    def test_malformed_line_does_not_lose_the_readable_records(self):
        path = self.seed_archive()
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertIsNotNone(self.feature_values().get("sentiment.mean_7d"))

    def test_partially_unreadable_archive_still_contributes_what_it_can(self):
        self.seed_archive()
        self.write_archive(None, month="2026-07", raw=b"\x1f\x8b broken")
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertIsNotNone(self.feature_values().get("sentiment.mean_7d"))


class TestDeterminism(ArchiveFeatureFixture):
    def test_repeated_runs_write_identical_values(self):
        self.seed_archive()
        self.assertEqual(self.run_features(["--apply"]), 0)
        first = self.feature_values()
        self.assertEqual(self.run_features(["--apply"]), 0)
        second = self.feature_values()
        self.assertEqual(self.run_features(["--apply"]), 0)
        third = self.feature_values()
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_duplicate_archive_records_do_not_change_the_result(self):
        path = self.seed_archive()
        with gzip.open(path, "at", encoding="utf-8") as handle:
            for article_id in self.archived_ids:
                handle.write(json.dumps(archived_record(
                    article_id, (ANCHOR - timedelta(days=1)).isoformat(),
                    source="Duplicate", score=-0.9)) + "\n")
        self.assertEqual(self.run_features(["--apply"]), 0)
        with_duplicates = self.feature_values()

        shutil.rmtree(self.archive_dir)
        self.seed_archive()
        self.assertEqual(self.run_features(["--apply"]), 0)
        self.assertEqual(with_duplicates, self.feature_values(),
                         "a conflicting duplicate changed a feature value")

    def test_dry_run_writes_nothing(self):
        self.seed_archive()
        self.assertEqual(self.run_features([]), 0)
        self.assertEqual(self.feature_values(), {})


class TestLoaderBehaviour(ArchiveFeatureFixture):
    def test_load_archived_articles_requests_only_linked_non_live_ids(self):
        self.seed_archive()
        # An archived article nobody links to must not be loaded, and a
        # live article must not be fetched from the archive.
        self.write_archive([archived_record("unlinked",
                                            (ANCHOR - timedelta(days=1)).isoformat())])
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO articles
            (article_id,url,title,summary,source,published_at,sentiment,stored_at)
            VALUES ('live-1','http://x/l','T','S','LiveSource',?,?,'now')""",
            ((ANCHOR - timedelta(days=1)).isoformat(),
             json.dumps({"score": 0.5, "label": "positive"})))
        conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                     "VALUES ('live-1','company','nvidia')")
        conn.commit()
        self.write_archive([archived_record("live-1", ANCHOR.isoformat(),
                                            source="StaleCopy", score=-0.99)])

        archived = load_archived_articles(conn, ArticleArchive(self.archive_dir))
        conn.close()
        self.assertEqual(sorted(archived), sorted(self.archived_ids))
        self.assertNotIn("unlinked", archived)
        self.assertNotIn("live-1", archived,
                         "a live article was overridden by an archived copy")

    def test_live_and_archived_articles_are_merged_for_one_instrument(self):
        self.seed_archive()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO articles
            (article_id,url,title,summary,source,published_at,sentiment,stored_at)
            VALUES ('live-1','http://x/l','T','S','LiveSource',?,?,'now')""",
            ((ANCHOR - timedelta(days=1)).isoformat(),
             json.dumps({"score": 0.5, "label": "positive"})))
        conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                     "VALUES ('live-1','company','nvidia')")
        conn.commit()

        archived = load_archived_articles(conn, ArticleArchive(self.archive_dir))
        live_only = load_entity_articles(conn, "inst-nvda")
        merged = load_entity_articles(conn, "inst-nvda", archived)
        conn.close()
        self.assertEqual(len(live_only), 1)
        self.assertEqual(len(merged), 1 + len(self.archived_ids))

    def test_merged_articles_are_returned_in_timestamp_order(self):
        self.seed_archive()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""INSERT INTO articles
            (article_id,url,title,summary,source,published_at,sentiment,stored_at)
            VALUES ('live-1','http://x/l','T','S','LiveSource',?,?,'now')""",
            ((ANCHOR - timedelta(days=3, hours=5)).isoformat(),
             json.dumps({"score": 0.5})))
        conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                     "VALUES ('live-1','company','nvidia')")
        conn.commit()
        archived = load_archived_articles(conn, ArticleArchive(self.archive_dir))
        merged = load_entity_articles(conn, "inst-nvda", archived)
        conn.close()
        stamps = [a.published_at for a in merged]
        self.assertEqual(stamps, sorted(stamps))

    def test_omitting_the_archive_map_keeps_the_old_behaviour(self):
        self.seed_archive()
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(load_entity_articles(conn, "inst-nvda"), [])
        conn.close()

    def test_load_archived_articles_returns_empty_without_an_archive(self):
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            load_archived_articles(conn, ArticleArchive(self.archive_dir)), {})
        conn.close()


if __name__ == "__main__":
    unittest.main()
