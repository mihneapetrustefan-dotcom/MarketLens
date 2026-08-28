"""
tests/scripts/test_build_event_studies.py
-----------------------------------------------------------
Tests for scripts/build_event_studies.py.

Covers the two correctness points that are easy to get wrong here:
the daily/minute session-timestamp separation (see the script's module
docstring), and idempotency of the deterministic study id.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.build_event_studies import study_id_for, load_candles, main
from src.data_access.schema import initialize_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema

ANCHOR = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def seed_db(path, days_before=40, days_after=10, seed_benchmark=True):
    conn = sqlite3.connect(path)
    initialize_schema(conn)
    initialize_fusion_schema(conn)
    initialize_price_cache_schema(conn)

    conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id,name,country) VALUES ('US_AND_INTL','X','US')")
    conn.execute("INSERT OR IGNORE INTO companies (company_id, canonical_name) VALUES ('nvidia','NVIDIA')")
    conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, instrument_type) "
                 "VALUES ('nvidia-common','nvidia','common_stock')")
    conn.execute("INSERT OR IGNORE INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) "
                 "VALUES ('inst-nvda','nvidia-common','US_AND_INTL','NVDA','stock')")
    conn.execute("""INSERT INTO canonical_events
        (canonical_event_id, event_type, category, lifecycle_state, corroboration_state, first_reported_at)
        VALUES ('ce-1','acquisition','corporate_action','reported','single_source',?)""", (ANCHOR.isoformat(),))
    conn.execute("INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) "
                 "VALUES ('ce-1','nvidia','primary')")

    def seed_daily(instrument_id, base_price):
        for i in range(-days_before, days_after + 1):
            ts = (ANCHOR + timedelta(days=i)).replace(hour=20, minute=0)
            price = base_price + i * 0.1
            conn.execute("""INSERT INTO price_candle_cache
                (instrument_id, interval, timestamp, open, high, low, close, adjusted_close, volume, source, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument_id, "1d", ts.isoformat(), price, price + 1, price - 1, price, price,
                 1_000_000, "test", "now"))

    seed_daily("inst-nvda", 100.0)
    if seed_benchmark:
        seed_daily("benchmark-spy", 500.0)
    conn.commit()
    conn.close()


class TestStudyIdFor(unittest.TestCase):
    def test_stable_across_calls(self):
        self.assertEqual(study_id_for("ce-1", "inst-nvda"), study_id_for("ce-1", "inst-nvda"))

    def test_different_instrument_gives_different_id(self):
        self.assertNotEqual(study_id_for("ce-1", "inst-nvda"), study_id_for("ce-1", "inst-msft"))


class TestLoadCandles(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed_db(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def test_daily_timestamps_exclude_minute_candles(self):
        conn = sqlite3.connect(self.db_path)
        # add one minute candle alongside the daily series
        conn.execute("""INSERT INTO price_candle_cache
            (instrument_id, interval, timestamp, open, high, low, close, adjusted_close, volume, source, fetched_at)
            VALUES ('inst-nvda','1m',?,100,101,99,100,100,1000,'test','now')""",
            (ANCHOR.isoformat(),))
        conn.commit()
        merged, daily_only = load_candles(conn, "inst-nvda")
        conn.close()
        # merged includes the minute candle, daily_only must not
        self.assertEqual(len(merged), len(daily_only) + 1)
        self.assertTrue(all(ts.isoformat()[11:13] == "20" for ts in daily_only))

    def test_merged_is_sorted_by_time(self):
        conn = sqlite3.connect(self.db_path)
        merged, _ = load_candles(conn, "inst-nvda")
        conn.close()
        timestamps = [c.timestamp for c in merged]
        self.assertEqual(timestamps, sorted(timestamps))


class TestBuildEventStudiesEndToEnd(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed_db(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, extra):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path] + extra
        try:
            return main()
        finally:
            sys.argv = argv

    def test_dry_run_writes_nothing(self):
        self._run([])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM event_studies").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_apply_produces_a_high_quality_study_with_enough_history(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        level = conn.execute("SELECT quality_level FROM event_studies").fetchone()[0]
        conn.close()
        self.assertEqual(level, "high")

    def test_abnormal_return_is_populated_when_benchmark_present(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT abnormal_return FROM event_study_returns WHERE window_name = 'd1'").fetchone()
        conn.close()
        self.assertIsNotNone(row[0])

    def test_rerunning_does_not_duplicate_studies(self):
        self._run(["--apply"])
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM event_studies").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_missing_benchmark_still_produces_a_study_without_abnormal_return(self):
        os.remove(self.db_path)
        seed_db(self.db_path, seed_benchmark=False)
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM event_studies").fetchone()[0]
        row = conn.execute(
            "SELECT abnormal_return FROM event_study_returns WHERE window_name = 'd1'").fetchone()
        conn.close()
        self.assertEqual(count, 1)
        self.assertIsNone(row[0])

    def test_insufficient_history_yields_unusable_quality(self):
        os.remove(self.db_path)
        seed_db(self.db_path, days_before=5)  # below min_baseline
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        level = conn.execute("SELECT quality_level FROM event_studies").fetchone()[0]
        conn.close()
        self.assertEqual(level, "unusable")


if __name__ == "__main__":
    unittest.main()
