"""
tests/data_access/test_schema.py
-------------------------------------
Tests for schema.py, proving the new canonical tables coexist safely
alongside the EXISTING application's tables — created here using the
real, unchanged RecommendationLog class, not a synthetic stand-in.
"""

import sys
import os
import sqlite3
import contextlib
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.schema import initialize_schema, get_all_table_names
from recommendation_log import RecommendationLog


@contextlib.contextmanager
def sqlite_tempfile():
    """A real, on-disk temporary SQLite file (RecommendationLog opens its own connection, so ':memory:' won't work across two separate connections)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        os.remove(path)


class TestSchemaIsAdditiveOnly(unittest.TestCase):
    def test_canonical_tables_created_on_fresh_database(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        tables = get_all_table_names(conn)
        for expected in ("exchanges", "sectors", "companies", "securities", "instruments", "news_sources"):
            self.assertIn(expected, tables)

    def test_running_twice_is_a_safe_no_op(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        initialize_schema(conn)  # must not raise
        tables = get_all_table_names(conn)
        self.assertIn("companies", tables)

    def test_coexists_with_real_existing_recommendation_log_tables(self):
        """
        The critical compatibility proof: create the database using the
        REAL, unmodified RecommendationLog first (exactly what
        run_daily.py does today), THEN layer the canonical schema on
        top, and confirm BOTH the old `recommendations` table and the
        new canonical tables are present, untouched, side by side.
        """
        with sqlite_tempfile() as db_path:
            rec_log = RecommendationLog(db_path)
            rec_log.log_recommendations([
                {"entity": "Nvidia", "recommendation": "BUY", "confidence_score": 0.8, "time_horizon": "short-term"}
            ])
            existing_rows_before = rec_log.load_all()
            rec_log.close()

            conn = sqlite3.connect(db_path)
            initialize_schema(conn)
            tables = get_all_table_names(conn)
            conn.close()

            # Old table still present and untouched.
            self.assertIn("recommendations", tables)
            # New canonical tables now also present.
            for expected in ("exchanges", "sectors", "companies", "securities", "instruments", "news_sources"):
                self.assertIn(expected, tables)

            # The pre-existing data is completely intact.
            rec_log_after = RecommendationLog(db_path)
            existing_rows_after = rec_log_after.load_all()
            rec_log_after.close()
            self.assertEqual(len(existing_rows_before), len(existing_rows_after))
            self.assertEqual(existing_rows_after[0]["entity"], "Nvidia")

    def test_instrument_uniqueness_constraint_enforced(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        conn.execute("INSERT INTO exchanges (exchange_id, name, country) VALUES ('BVB', 'Bursa', 'RO')")
        conn.execute("INSERT INTO securities (security_id, company_id, instrument_type) VALUES ('s1', NULL, 'common_stock')")
        conn.execute("INSERT INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES ('i1', 's1', 'BVB', 'EL', 'bvb')")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES ('i2', 's1', 'BVB', 'EL', 'bvb')")
            conn.commit()

    def test_same_ticker_different_exchange_is_allowed(self):
        # The "EL" case again, at the schema level this time.
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        conn.execute("INSERT INTO exchanges (exchange_id, name, country) VALUES ('BVB', 'Bursa', 'RO')")
        conn.execute("INSERT INTO exchanges (exchange_id, name, country) VALUES ('NYSE', 'NYSE', 'US')")
        conn.execute("INSERT INTO securities (security_id, company_id, instrument_type) VALUES ('s1', NULL, 'common_stock')")
        conn.execute("INSERT INTO securities (security_id, company_id, instrument_type) VALUES ('s2', NULL, 'common_stock')")
        conn.execute("INSERT INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES ('i1', 's1', 'BVB', 'EL', 'bvb')")
        conn.execute("INSERT INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES ('i2', 's2', 'NYSE', 'EL', 'stock')")
        conn.commit()  # must not raise
        rows = conn.execute("SELECT COUNT(*) FROM instruments WHERE ticker = 'EL'").fetchone()
        self.assertEqual(rows[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
