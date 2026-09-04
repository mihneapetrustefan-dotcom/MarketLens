"""
tests/scripts/test_populate_events.py
-----------------------------------------------------------
Tests for scripts/populate_events.py: timestamp parsing, the
articles+article_entities join, and end-to-end idempotency against
a disposable temp database.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.populate_events import (
    parse_ts, load_articles_with_entities, ESTIMATED_BYTES_PER_EVENT, main,
)
from src.news_database import NewsDatabase
from src.data_access.news_schema import initialize_news_schema
from src.data_access.event_repository import initialize_event_schema


class TestParseTs(unittest.TestCase):
    def test_parses_iso_with_offset(self):
        result = parse_ts("2026-08-27T21:24:24+00:00")
        self.assertEqual(result, datetime(2026, 8, 27, 21, 24, 24, tzinfo=timezone.utc))

    def test_none_input_returns_none(self):
        self.assertIsNone(parse_ts(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_ts(""))

    def test_malformed_string_returns_none_not_raises(self):
        # articles.published_at is legacy data — a bad row must not
        # abort the whole run.
        self.assertIsNone(parse_ts("not a date"))


class TestByteEstimate(unittest.TestCase):
    def test_estimate_is_at_or_above_measured_value(self):
        # Measured 1.21 MB / 521 new events =~ 2320 bytes/event on the
        # full corpus. The constant must not under-project, or the
        # size guard could let a breaching write through.
        self.assertGreaterEqual(ESTIMATED_BYTES_PER_EVENT, 2320)


class TestLoadArticlesWithEntities(unittest.TestCase):
    """Verifies the join that makes this script read-only against
    Phase 1/3 tables: articles without a Phase 3 entity link must
    never be returned."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # Closed, not discarded: a live handle keeps the temp file
        # undeletable on Windows and tearDown then fails.
        NewsDatabase(self.db_path).close()  # creates the legacy `articles` table
        self.conn = sqlite3.connect(self.db_path)
        initialize_news_schema(self.conn)
        self.conn.execute("""
            INSERT INTO articles (article_id, url, title, summary, published_at, stored_at)
            VALUES ('a1', 'http://x/1', 'Company X acquires Company Y', 's', '2026-01-01T00:00:00+00:00', 'now')
        """)
        self.conn.execute("""
            INSERT INTO articles (article_id, url, title, summary, published_at, stored_at)
            VALUES ('a2', 'http://x/2', 'No entities here', 's', '2026-01-02T00:00:00+00:00', 'now')
        """)
        self.conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, entity_id) VALUES ('a1', 'company', 'company-x')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_only_articles_with_entities_are_returned(self):
        result = load_articles_with_entities(self.conn, limit=None, since=None)
        ids = [a["article_id"] for a in result]
        self.assertIn("a1", ids)
        self.assertNotIn("a2", ids)

    def test_entity_ids_attached_to_article(self):
        result = load_articles_with_entities(self.conn, limit=None, since=None)
        self.assertEqual(result[0]["_entity_ids"], ["company-x"])

    def test_since_filters_by_published_at(self):
        result = load_articles_with_entities(self.conn, limit=None, since="2026-06-01")
        self.assertEqual(result, [])


class TestEndToEndIdempotency(unittest.TestCase):
    """Runs the actual CLI entry point twice against a disposable
    database with one realistic acquisition article, and asserts the
    second run creates no new event rows."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        NewsDatabase(self.db_path).close()  # creates the legacy `articles` table
        conn = sqlite3.connect(self.db_path)
        initialize_news_schema(conn)
        initialize_event_schema(conn)
        conn.execute("""
            INSERT INTO articles (article_id, url, title, summary, published_at, stored_at)
            VALUES ('a1', 'http://x/1',
                    'NVIDIA agrees to acquire Company Y for $2 billion',
                    'The deal is expected to close next quarter.',
                    '2026-01-01T00:00:00+00:00', 'now')
        """)
        conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, entity_id) VALUES ('a1', 'company', 'nvidia')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, extra_args):
        argv = sys.argv
        sys.argv = ["populate_events.py", "--db", self.db_path,
                    "--max-db-mb", "500"] + extra_args
        try:
            return main()
        finally:
            sys.argv = argv

    def test_the_source_tier_reaches_the_confidence_components(self):
        """
        The bug this guards: populate_events called the extractor
        WITHOUT source_tier, so source_quality was the 0.4
        `unclassified` default on every event ever stored -- one of
        four constant components in a five-component confidence.

        A test of get_source_tier alone would not have caught it. This
        one reads the stored breakdown and asserts the tier arrived.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE articles SET source = 'Reuters' WHERE article_id = 'a1'")
        conn.commit()
        conn.close()

        self.assertEqual(self._run(["--apply"]), 0)

        conn = sqlite3.connect(self.db_path)
        raw = conn.execute(
            "SELECT confidence_json FROM events LIMIT 1").fetchone()[0]
        conn.close()

        components = json.loads(raw).get("components", {})
        quality = components.get("source_quality", {})
        value = quality.get("value") if isinstance(quality, dict) else quality
        self.assertEqual(value, 0.8,
                         "Reuters is wire_and_major_press (0.8); 0.4 means "
                         "the tier never reached the extractor")

    def test_a_google_news_article_is_tiered_as_an_aggregator(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE articles SET source = 'Google News: NVIDIA' "
                     "WHERE article_id = 'a1'")
        conn.commit()
        conn.close()

        self.assertEqual(self._run(["--apply"]), 0)

        conn = sqlite3.connect(self.db_path)
        raw = conn.execute(
            "SELECT confidence_json FROM events LIMIT 1").fetchone()[0]
        conn.close()

        components = json.loads(raw).get("components", {})
        quality = components.get("source_quality", {})
        value = quality.get("value") if isinstance(quality, dict) else quality
        self.assertEqual(value, 0.6,
                         "a Google News feed is specialized_or_aggregator (0.6)")

    def test_second_run_creates_no_duplicate_events(self):
        self.assertEqual(self._run(["--apply"]), 0)
        conn = sqlite3.connect(self.db_path)
        first_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(first_count, 1)

        self.assertEqual(self._run(["--apply"]), 0)
        conn = sqlite3.connect(self.db_path)
        second_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(second_count, 1)

    def test_dry_run_writes_nothing(self):
        self._run([])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_low_size_threshold_refuses_write(self):
        result = self._run(["--max-db-mb", "0", "--apply"])
        self.assertEqual(result, 2)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
