"""
tests/test_dashboard_canonical_corpus.py
------------------------------------------------
TD-02b: the dashboard reads the canonical news corpus.

WHY ONLY THIS READER MOVED
------------------------------
Phase 17 listed seven modules on the legacy `articles` table. Reading
them one at a time gave a different count and a different answer:

  impact_engine.py          NOT A READER — it scores dicts handed to
                            it and never queries the table. Miscounted.
  news_database.py          the WRITER
  archive_old_articles.py   the PRUNER; must stay on the legacy table,
                            because pruning it is what creates the
                            retention divergence on purpose
  backfill_article_entities needs companies_mentioned and
                            tickers_mentioned — columns the canonical
                            table does not have
  compute_features.py       canonical reaches back three more years, so
                            repointing changes MODEL INPUTS
  populate_events.py        same; would mint events from 2023 articles
  dashboard.py              three read-only aggregates — moves safely

WHAT THESE DEFEND
---------------------
1. The counts come from `news_articles` when it holds rows.
2. They fall back to `articles` when it does not — a health page
   reporting zero because a table is missing is worse than one
   reporting the older number.
3. The page SAYS which corpus it used. §19 forbids silently changing
   user-visible history, and this history genuinely changes: 48,906 to
   48,955 rows, oldest 2026-07-07 to 2023-12-15.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator

LEGACY_DDL = """
    CREATE TABLE articles (article_id TEXT PRIMARY KEY, url TEXT, title TEXT,
        summary TEXT, source TEXT, category TEXT, published_at TEXT,
        collected_at TEXT, duplicate_group_id TEXT, duplicate_group_size INTEGER,
        companies_mentioned TEXT, tickers_mentioned TEXT, sectors TEXT,
        sentiment TEXT, impact TEXT, stored_at TEXT);
"""

CANONICAL_DDL = """
    CREATE TABLE news_articles (article_id TEXT PRIMARY KEY, provider TEXT,
        source_name TEXT, title TEXT, published_at TEXT, sentiment_label TEXT,
        sentiment_score REAL, impact_score REAL, duplicate_of TEXT);
"""


class CorpusCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(LEGACY_DDL + CANONICAL_DDL)

    def tearDown(self):
        self.conn.close()

    def add_legacy(self, article_id, source="Reuters", published_at="2026-08-01T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO articles (article_id, source, published_at, title) "
            "VALUES (?,?,?,'t')", (article_id, source, published_at))
        self.conn.commit()

    def add_canonical(self, article_id, source_name="Reuters",
                      published_at="2026-08-01T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO news_articles (article_id, source_name, published_at, title) "
            "VALUES (?,?,?,'t')", (article_id, source_name, published_at))
        self.conn.commit()

    def health(self):
        generator = DashboardGenerator.__new__(DashboardGenerator)
        return generator._collect_health(self.conn)


class TestItPrefersCanonical(CorpusCase):

    def test_the_count_comes_from_the_canonical_table(self):
        self.add_legacy("a-1")
        self.add_canonical("a-1")
        self.add_canonical("a-2")   # the row the legacy table has lost
        health = self.health()
        self.assertEqual(health["total_articles"], 2)
        self.assertEqual(health["news_table"], "news_articles")

    def test_the_legacy_count_is_still_reported_alongside(self):
        """Both numbers are shown so the difference is explainable."""
        self.add_legacy("a-1")
        self.add_canonical("a-1")
        self.add_canonical("a-2")
        health = self.health()
        self.assertEqual(health["legacy_articles"], 1)
        self.assertEqual(health["total_articles"], 2)

    def test_the_source_count_uses_the_canonical_column_name(self):
        """`source` on the legacy table, `source_name` on canonical."""
        self.add_canonical("a-1", source_name="Reuters")
        self.add_canonical("a-2", source_name="Bloomberg")
        self.assertEqual(self.health()["sources"], 2)

    def test_the_date_range_reaches_back_over_the_canonical_corpus(self):
        self.add_legacy("a-1", published_at="2026-07-07T00:00:00+00:00")
        self.add_canonical("a-1", published_at="2026-07-07T00:00:00+00:00")
        self.add_canonical("a-0", published_at="2023-12-15T00:00:00+00:00")
        health = self.health()
        self.assertTrue(health["oldest_article"].startswith("2023-12-15"))


class TestItFallsBackRatherThanReportingZero(CorpusCase):

    def test_an_empty_canonical_table_falls_back_to_legacy(self):
        """
        The state every database was in before the migration ran. An
        empty canonical table is not evidence that there is no news.
        """
        self.add_legacy("a-1")
        self.add_legacy("a-2")
        health = self.health()
        self.assertEqual(health["total_articles"], 2)
        self.assertEqual(health["news_table"], "articles")

    def test_an_absent_canonical_table_falls_back_to_legacy(self):
        self.conn.execute("DROP TABLE news_articles")
        self.add_legacy("a-1")
        health = self.health()
        self.assertEqual(health["total_articles"], 1)
        self.assertEqual(health["news_table"], "articles")

    def test_the_fallback_uses_the_legacy_source_column(self):
        self.conn.execute("DROP TABLE news_articles")
        self.add_legacy("a-1", source="Reuters")
        self.add_legacy("a-2", source="Bloomberg")
        self.assertEqual(self.health()["sources"], 2)

    def test_neither_table_yields_zero_rather_than_raising(self):
        self.conn.execute("DROP TABLE news_articles")
        self.conn.execute("DROP TABLE articles")
        self.assertEqual(self.health()["total_articles"], 0)


class TestTheChangeIsNotSilent(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "src", "dashboard.py")
        with open(path, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_the_page_names_the_corpus_it_used(self):
        self.assertIn("D.health.news_table", self.source)

    def test_the_page_explains_why_the_two_counts_differ(self):
        self.assertIn("taiata la 60 de zile", self.source)
        self.assertIn("Diferenta este intentionata", self.source)


class TestTheOtherReadersWereNotMovedBlindly(unittest.TestCase):
    """
    §20: do not perform bulk replacement without validating query
    semantics. These pin the specific blockers, so a future bulk
    replace fails here with the reason rather than in production with a
    wrong number.
    """

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_the_pruner_still_prunes_the_legacy_table(self):
        """
        Repointing it would delete the canonical history, which is the
        one thing the migration exists to preserve.
        """
        body = self._read("scripts", "archive_old_articles.py")
        self.assertIn("DELETE FROM articles", body)
        self.assertNotIn("DELETE FROM news_articles", body)

    def test_the_entity_backfill_needs_columns_canonical_lacks(self):
        body = self._read("scripts", "backfill_article_entities.py")
        self.assertIn("companies_mentioned", body)
        self.assertIn("tickers_mentioned", body)

    def test_impact_engine_is_not_a_reader_at_all(self):
        """
        Phase 17 counted it as one of seven. It queries nothing.
        """
        body = self._read("src", "impact_engine.py")
        self.assertNotIn("FROM articles", body)
        self.assertNotIn("FROM news_articles", body)


if __name__ == "__main__":
    unittest.main()
