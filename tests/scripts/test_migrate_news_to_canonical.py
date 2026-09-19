"""
tests/scripts/test_migrate_news_to_canonical.py
--------------------------------------------------------
The legacy → canonical article migration (Phase 17, TD-02).

WHAT THESE DEFEND
---------------------
That a migration moving 48,392 rows across a schema boundary does not
invent data, does not modify its source, and can be run twice.

The tests that matter most assert what the migration REFUSES to do:
it will not write a titleless row as an empty-titled article, it will
not guess a language or an author the legacy schema never recorded,
and it will not touch the `articles` table under any circumstance.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.news_schema import initialize_news_schema
from src.domain.news_models import DuplicateMatchLevel, ProcessingStatus

_spec = importlib.util.spec_from_file_location(
    "migrate_news_to_canonical",
    os.path.join(os.path.dirname(__file__), "..", "..",
                 "scripts", "migrate_news_to_canonical.py"))
migrate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_mod)


LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT,
    summary TEXT, source TEXT, category TEXT, published_at TEXT,
    collected_at TEXT, duplicate_group_id TEXT, duplicate_group_size INTEGER,
    companies_mentioned TEXT, tickers_mentioned TEXT, sectors TEXT,
    sentiment TEXT, impact TEXT, stored_at TEXT NOT NULL
)
"""


def legacy_row(**overrides):
    base = dict(
        article_id="a-1", url="https://ex.com/one", title="A headline",
        summary="A summary", source="CNBC Top News", category="stocks",
        published_at="2026-08-30T21:09:11+00:00",
        collected_at="2026-08-30T21:22:37+00:00",
        duplicate_group_id="g-1", duplicate_group_size=1,
        companies_mentioned="[]", tickers_mentioned="[]", sectors="[]",
        sentiment=json.dumps({"score": 1.0, "label": "positive"}),
        impact=json.dumps({"score": 0.4, "level": "moderate"}),
        stored_at="2026-08-30T21:22:44+00:00")
    base.update(overrides)
    return base


class MigrationCase(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(LEGACY_DDL)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS news_sources (
            source_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            source_type TEXT NOT NULL, url TEXT, active INTEGER NOT NULL DEFAULT 1)""")
        self.conn.execute(
            "INSERT INTO news_sources VALUES ('cnbc-top-news','CNBC Top News','wire_or_major_press',NULL,1)")
        initialize_news_schema(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def insert(self, *rows):
        for row in rows:
            self.conn.execute(
                "INSERT INTO articles VALUES (:article_id,:url,:title,:summary,"
                ":source,:category,:published_at,:collected_at,"
                ":duplicate_group_id,:duplicate_group_size,:companies_mentioned,"
                ":tickers_mentioned,:sectors,:sentiment,:impact,:stored_at)", row)
        self.conn.commit()

    def run_migration(self, **kwargs):
        return migrate_mod.migrate(self.conn, kwargs.pop("limit", None),
                                   kwargs.pop("dry_run", False), quiet=True)

    def canonical(self, article_id="a-1"):
        return self.conn.execute(
            "SELECT * FROM news_articles WHERE article_id = ?",
            (article_id,)).fetchone()


class TestFieldsCarriedAcross(MigrationCase):

    def test_the_article_id_is_preserved(self):
        """
        The whole reason 26,400 Phase 5 entity links survive: they key
        on article_id, and it does not change.
        """
        self.insert(legacy_row())
        self.run_migration()
        self.assertEqual(self.canonical()["article_id"], "a-1")

    def test_sentiment_and_impact_survive(self):
        """
        The Phase 2 domain model carries these fields with a comment
        saying they exist so a migration keeps what the legacy engines
        already produced. This is that migration.
        """
        self.insert(legacy_row())
        self.run_migration()
        row = self.canonical()
        self.assertEqual(row["sentiment_label"], "positive")
        self.assertEqual(row["sentiment_score"], "1.0")
        self.assertEqual(row["impact_score"], "0.4")

    def test_the_category_becomes_a_list(self):
        self.insert(legacy_row())
        self.run_migration()
        self.assertEqual(json.loads(self.canonical()["categories_json"]),
                         ["stocks"])

    def test_a_known_source_resolves_to_its_canonical_id(self):
        self.insert(legacy_row())
        self.run_migration()
        row = self.canonical()
        self.assertEqual(row["source_id"], "cnbc-top-news")
        self.assertEqual(row["source_name"], "CNBC Top News")

    def test_an_unknown_source_keeps_its_name_and_has_no_id(self):
        """
        385 of 398 legacy source names have no canonical row. The name
        is what existing code joins on, so it is kept; the id is left
        NULL rather than invented.
        """
        self.insert(legacy_row(source="Some Blog"))
        self.run_migration()
        row = self.canonical()
        self.assertIsNone(row["source_id"])
        self.assertEqual(row["source_name"], "Some Blog")

    def test_migrated_rows_are_marked_ready_not_ingested(self):
        """
        These rows went through cleaning, dedup, entity detection and
        scoring. Calling them freshly INGESTED would invite a pipeline
        to reprocess work already done.
        """
        self.insert(legacy_row())
        self.run_migration()
        self.assertEqual(self.canonical()["processing_status"],
                         ProcessingStatus.READY.value)

    def test_the_provider_is_recorded_as_legacy_not_guessed(self):
        """
        The legacy schema never recorded which collector produced a
        row. Inferring it from the URL would be a guess presented as a
        fact.
        """
        self.insert(legacy_row())
        self.run_migration()
        self.assertEqual(self.canonical()["provider"], "legacy")


class TestWhatIsNotInvented(MigrationCase):

    def test_absent_fields_stay_null(self):
        self.insert(legacy_row())
        self.run_migration()
        row = self.canonical()
        for column in ("language", "country", "author",
                       "provider_article_id", "raw_id", "updated_at"):
            self.assertIsNone(row[column],
                              f"{column} was invented; the legacy schema "
                              f"has no equivalent")

    def test_a_titleless_row_is_skipped_not_written_empty(self):
        """
        `news_articles.title` is NOT NULL. Writing "" would produce a
        row that looks like an article and is not.
        """
        self.insert(legacy_row(article_id="a-2", title="   "))
        stats = self.run_migration()
        self.assertEqual(stats["skipped_no_title"], 1)
        self.assertIsNone(self.canonical("a-2"))

    def test_unparseable_sentiment_json_does_not_raise(self):
        self.insert(legacy_row(sentiment="{not json"))
        self.run_migration()
        self.assertIsNone(self.canonical()["sentiment_label"])


class TestDuplicates(MigrationCase):

    def test_a_group_of_one_is_not_a_duplicate(self):
        self.insert(legacy_row())
        self.run_migration()
        row = self.canonical()
        self.assertIsNone(row["duplicate_of"])
        self.assertEqual(row["duplicate_match_level"],
                         DuplicateMatchLevel.NONE.value)

    def test_the_earliest_member_of_a_group_becomes_canonical(self):
        self.insert(
            legacy_row(article_id="a-late", url="https://ex.com/l",
                       duplicate_group_id="g-9", duplicate_group_size=2,
                       published_at="2026-08-30T12:00:00+00:00"),
            legacy_row(article_id="a-early", url="https://ex.com/e",
                       duplicate_group_id="g-9", duplicate_group_size=2,
                       published_at="2026-08-30T09:00:00+00:00"))
        self.run_migration()
        self.assertIsNone(self.canonical("a-early")["duplicate_of"])
        self.assertEqual(self.canonical("a-late")["duplicate_of"], "a-early")

    def test_a_duplicate_records_which_level_matched(self):
        """Spec: a duplicate decision must be auditable, never a black box."""
        self.insert(
            legacy_row(article_id="a-1", duplicate_group_id="g-9",
                       duplicate_group_size=2,
                       published_at="2026-08-30T09:00:00+00:00"),
            legacy_row(article_id="a-2", url="https://ex.com/2",
                       duplicate_group_id="g-9", duplicate_group_size=2,
                       published_at="2026-08-30T12:00:00+00:00"))
        self.run_migration()
        self.assertEqual(self.canonical("a-2")["duplicate_match_level"],
                         DuplicateMatchLevel.TITLE_SOURCE_TIME.value)

    def test_no_duplicate_points_at_a_row_that_does_not_exist(self):
        self.insert(
            legacy_row(article_id="a-1", duplicate_group_id="g-9",
                       duplicate_group_size=2,
                       published_at="2026-08-30T09:00:00+00:00"),
            legacy_row(article_id="a-2", url="https://ex.com/2",
                       duplicate_group_id="g-9", duplicate_group_size=2,
                       published_at="2026-08-30T12:00:00+00:00"))
        self.run_migration()
        orphans = self.conn.execute("""
            SELECT COUNT(*) FROM news_articles n
            WHERE n.duplicate_of IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM news_articles o
                              WHERE o.article_id = n.duplicate_of)
        """).fetchone()[0]
        self.assertEqual(orphans, 0)


class TestSafety(MigrationCase):
    """The properties that make this safe to run against 48,392 rows."""

    def test_the_source_table_is_never_modified(self):
        self.insert(legacy_row(), legacy_row(article_id="a-2",
                                             url="https://ex.com/2"))
        before = self.conn.execute(
            "SELECT * FROM articles ORDER BY article_id").fetchall()
        self.run_migration()
        after = self.conn.execute(
            "SELECT * FROM articles ORDER BY article_id").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after],
                         "the migration modified the articles table")

    def test_running_it_twice_changes_nothing(self):
        self.insert(legacy_row())
        self.run_migration()
        first = tuple(self.canonical())
        self.run_migration()
        self.assertEqual(tuple(self.canonical()), first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0], 1)

    def test_a_dry_run_writes_nothing(self):
        self.insert(legacy_row())
        stats = self.run_migration(dry_run=True)
        self.assertEqual(stats["written"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0], 0)

    def test_verification_fails_when_a_row_is_missing(self):
        """
        The check has to be capable of failing, or it proves nothing.
        """
        self.insert(legacy_row())
        self.assertFalse(migrate_mod.verify(self.conn, quiet=True))
        self.run_migration()
        self.assertTrue(migrate_mod.verify(self.conn, quiet=True))


class TestRecoveryFromArchives(MigrationCase):
    """
    An entity link whose article left `articles` before it ever reached
    `news_articles`. Found in production on 2026-09-17: 503 links, 427
    articles, every one preserved in the July archive. The daily run
    reported FAILED on every pass because of them.
    """

    def setUp(self):
        super().setUp()
        self.archive_dir = tempfile.mkdtemp()

    def tearDown(self):
        super().tearDown()
        import shutil
        shutil.rmtree(self.archive_dir, ignore_errors=True)

    def archive(self, *rows, name="articles_2026-07.jsonl.gz"):
        import gzip
        with gzip.open(os.path.join(self.archive_dir, name), "wt",
                       encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def link(self, article_id):
        self.conn.execute("INSERT INTO article_entities VALUES (?, 'company', 'co-1')",
                          (article_id,))
        self.conn.commit()

    def test_an_archived_orphan_is_recovered_and_verification_passes(self):
        self.archive(legacy_row(article_id="gone-1", url="https://ex.com/g"))
        self.link("gone-1")
        self.assertFalse(migrate_mod.verify(self.conn, quiet=True))

        stats = migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(stats["not_in_archive"], 0)
        self.assertEqual(self.canonical("gone-1")["title"], "A headline")
        self.assertTrue(migrate_mod.verify(self.conn, quiet=True))

    def test_a_link_to_an_article_in_no_archive_stays_a_failure(self):
        """NEGATIVE CONTROL: real loss must not be papered over."""
        self.link("never-archived")
        stats = migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        self.assertEqual(stats["recovered"], 0)
        self.assertEqual(stats["not_in_archive"], 1)
        self.assertFalse(migrate_mod.verify(self.conn, quiet=True))

    def test_recovery_never_writes_the_articles_table(self):
        self.archive(legacy_row(article_id="gone-1", url="https://ex.com/g"))
        self.link("gone-1")
        migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 0,
            "the archive was un-archived into the live table")

    def test_only_orphans_are_recovered_not_the_whole_archive(self):
        self.archive(legacy_row(article_id="gone-1", url="https://ex.com/g"),
                     legacy_row(article_id="unlinked", url="https://ex.com/u"))
        self.link("gone-1")
        migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        self.assertIsNotNone(self.canonical("gone-1"))
        self.assertIsNone(self.canonical("unlinked"))

    def test_recovery_is_idempotent(self):
        self.archive(legacy_row(article_id="gone-1", url="https://ex.com/g"))
        self.link("gone-1")
        migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        first = tuple(self.canonical("gone-1"))
        again = migrate_mod.recover_from_archives(self.conn, self.archive_dir)
        self.assertEqual(again["orphaned"], 0)
        self.assertEqual(tuple(self.canonical("gone-1")), first)

    def test_a_dry_run_recovers_nothing(self):
        self.archive(legacy_row(article_id="gone-1", url="https://ex.com/g"))
        self.link("gone-1")
        stats = migrate_mod.recover_from_archives(self.conn, self.archive_dir,
                                                  dry_run=True)
        self.assertEqual(stats["recovered"], 1)
        self.assertIsNone(self.canonical("gone-1"))


if __name__ == "__main__":
    unittest.main()
