"""
tests/scripts/test_fuse_events.py
-----------------------------------------------------------
Tests for scripts/fuse_events.py and the Phase 5 fusion schema.

The two properties that matter most here, and that broke during
development, are covered end to end: deterministic ids (so re-running
does not duplicate canonical events) and the report->canonical link
being complete and lossless.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.fuse_events import (
    categorize_source, decision_id_for, canonical_id_for, main,
)
from scripts.populate_events import main as populate_main
from src.domain.fusion_models import SourceCategory
from src.news_database import NewsDatabase
from src.data_access.news_schema import initialize_news_schema
from src.data_access.event_repository import initialize_event_schema
from src.data_access.fusion_schema import initialize_fusion_schema


class TestCategorizeSource(unittest.TestCase):
    def test_known_major_press(self):
        self.assertEqual(categorize_source("Reuters"), SourceCategory.MAJOR_FINANCIAL_PRESS)

    def test_match_is_case_insensitive(self):
        self.assertEqual(categorize_source("REUTERS Business"), SourceCategory.MAJOR_FINANCIAL_PRESS)

    def test_unknown_source_is_unknown_not_guessed(self):
        # Never flatter an unrecognized source into a category — the
        # category feeds quality scoring.
        self.assertEqual(categorize_source("Some Random Blog"), SourceCategory.UNKNOWN)

    def test_none_is_unknown(self):
        self.assertEqual(categorize_source(None), SourceCategory.UNKNOWN)


class TestDeterministicIds(unittest.TestCase):
    def test_decision_id_is_stable(self):
        self.assertEqual(decision_id_for("evt-abc"), decision_id_for("evt-abc"))

    def test_different_reports_get_different_decision_ids(self):
        self.assertNotEqual(decision_id_for("evt-a"), decision_id_for("evt-b"))

    def test_canonical_id_is_order_independent(self):
        # The set of reports identifies the occurrence; the order they
        # happened to be processed in must not change the id.
        self.assertEqual(canonical_id_for(["r1", "r2"]), canonical_id_for(["r2", "r1"]))

    def test_canonical_id_changes_with_membership(self):
        self.assertNotEqual(canonical_id_for(["r1"]), canonical_id_for(["r1", "r2"]))


class TestFusionSchema(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_schema_creates_all_tables(self):
        initialize_fusion_schema(self.conn)
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("canonical_events", "canonical_event_participants",
                         "canonical_event_reports", "fusion_decisions",
                         "fusion_contradictions", "fusion_timeline",
                         "fusion_review_cases"):
            self.assertIn(expected, names)

    def test_schema_is_safe_to_run_twice(self):
        initialize_fusion_schema(self.conn)
        initialize_fusion_schema(self.conn)  # must not raise


class TestFusionEndToEnd(unittest.TestCase):
    """Two articles describing the same acquisition must fuse into one
    canonical event, and re-running must not duplicate it."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        NewsDatabase(self.db_path).close()
        conn = sqlite3.connect(self.db_path)
        initialize_news_schema(conn)
        initialize_event_schema(conn)
        initialize_fusion_schema(conn)
        for i, (src, title) in enumerate([
            ("Reuters", "NVIDIA agrees to acquire Company Y for $2 billion"),
            ("CNBC", "NVIDIA to acquire Company Y in $2 billion deal"),
        ], start=1):
            conn.execute(
                "INSERT INTO articles (article_id, url, title, summary, source, published_at, stored_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"a{i}", f"http://x/{i}", title, "Deal expected to close next quarter.",
                 src, "2026-01-01T00:00:00+00:00", "now"))
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, entity_id) VALUES (?,?,?)",
                (f"a{i}", "company", "nvidia"))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, entry, extra):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path, "--max-db-mb", "500"] + extra
        try:
            return entry()
        finally:
            sys.argv = argv

    def _count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_two_reports_fuse_into_one_canonical_event(self):
        self._run(populate_main, ["--apply"])
        self.assertEqual(self._count("events"), 2)  # two reports preserved

        self._run(main, ["--apply"])
        self.assertEqual(self._count("canonical_events"), 1)
        # Both reports linked — fusion never drops a report.
        self.assertEqual(self._count("canonical_event_reports"), 2)

    def test_rerunning_fusion_does_not_duplicate(self):
        self._run(populate_main, ["--apply"])
        self._run(main, ["--apply"])
        first = self._count("canonical_events")
        self._run(main, ["--apply"])
        self.assertEqual(self._count("canonical_events"), first)

    def test_dry_run_writes_nothing(self):
        self._run(populate_main, ["--apply"])
        self._run(main, [])
        self.assertEqual(self._count("canonical_events"), 0)

    def test_reports_are_never_deleted_by_fusion(self):
        self._run(populate_main, ["--apply"])
        before = self._count("events")
        self._run(main, ["--apply"])
        self.assertEqual(self._count("events"), before)


if __name__ == "__main__":
    unittest.main()
