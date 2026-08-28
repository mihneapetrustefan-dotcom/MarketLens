"""
tests/scripts/test_backfill_article_entities.py
-----------------------------------------------------------
Tests for the Phase 3 backfill script.

Focus is on the two things that can silently corrupt a backfill:
malformed legacy JSON aborting the run, and the size guard failing
to stop a write that would breach the database size limit.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.backfill_article_entities import extract_mentions, MEASURED_BYTES_PER_ROW
from src.domain.entity_models import EntityType


class TestExtractMentions(unittest.TestCase):
    def test_extracts_plain_string_list(self):
        result = extract_mentions(json.dumps(["Banca Transilvania", "OMV Petrom"]), None)
        self.assertEqual([t for t, _ in result], ["Banca Transilvania", "OMV Petrom"])

    def test_extracts_ticker_from_dict_form(self):
        raw = json.dumps([{"ticker": "RTX", "name": "RTX Corporation", "match_type": "bare"}])
        result = extract_mentions(None, raw)
        self.assertEqual([t for t, _ in result], ["RTX"])

    def test_falls_back_to_name_when_ticker_absent(self):
        raw = json.dumps([{"name": "RTX Corporation"}])
        self.assertEqual([t for t, _ in extract_mentions(None, raw)], ["RTX Corporation"])

    def test_expected_type_is_company(self):
        result = extract_mentions(json.dumps(["Banca Transilvania"]), None)
        self.assertEqual(result[0][1], EntityType.COMPANY)

    def test_malformed_json_is_skipped_not_raised(self):
        # The legacy corpus predates schema validation. A handful of bad
        # rows must never abort a 50k-article backfill.
        self.assertEqual(extract_mentions("{not json", "also bad"), [])

    def test_non_list_json_is_skipped(self):
        self.assertEqual(extract_mentions(json.dumps({"a": 1}), None), [])

    def test_empty_and_none_inputs(self):
        self.assertEqual(extract_mentions(None, None), [])
        self.assertEqual(extract_mentions("", ""), [])
        self.assertEqual(extract_mentions(json.dumps([]), json.dumps([])), [])

    def test_blank_and_unusable_entries_are_dropped(self):
        raw = json.dumps(["", "   ", {"ticker": ""}, {"other": "x"}, 42, None])
        self.assertEqual(extract_mentions(raw, None), [])

    def test_both_columns_are_combined(self):
        result = extract_mentions(
            json.dumps(["Banca Transilvania"]),
            json.dumps([{"ticker": "TLV"}]),
        )
        self.assertEqual([t for t, _ in result], ["Banca Transilvania", "TLV"])


class TestSizeProjection(unittest.TestCase):
    def test_bytes_per_row_is_above_measured_value(self):
        # Measured 219 bytes/row on the full backfill. The constant must
        # stay at or above that, or the guard under-projects and lets a
        # breaching write through.
        self.assertGreaterEqual(MEASURED_BYTES_PER_ROW, 219)

    def test_bytes_per_row_is_not_wildly_pessimistic(self):
        # An over-large constant causes false refusals on writes that
        # would actually fit.
        self.assertLess(MEASURED_BYTES_PER_ROW, 300)


class TestIdempotency(unittest.TestCase):
    """The backfill must be safe to re-run; INSERT OR IGNORE + the
    composite primary key are what guarantee that."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
        from src.data_access.news_schema import initialize_news_schema
        initialize_news_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_reinserting_same_row_does_not_duplicate(self):
        row = ("art-1", "company", "banca-transilvania")
        for _ in range(3):
            self.conn.execute(
                "INSERT OR IGNORE INTO article_entities (article_id, entity_type, entity_id) VALUES (?, ?, ?)",
                row,
            )
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
