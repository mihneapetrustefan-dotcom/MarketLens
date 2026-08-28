"""
tests/scripts/test_cache_price_candles.py
-----------------------------------------------------------
Tests for scripts/cache_price_candles.py.

Network calls are always mocked — PolygonConnector.get_daily_candles /
get_minute_candles are patched, never the raw urlopen, matching the
project's own test pattern in tests/impact/test_polygon_connector.py.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.cache_price_candles import (
    resolve_event_instruments, anchor_for_event, is_range_cached,
    record_request, store_candles, main,
)
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.schema import initialize_schema
from src.impact.engine import Candle

PUB = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)


def seed_one_event(conn, canonical_event_id="ce-1", entity_id="nvidia",
                   ticker="NVDA", asset_class="stock", exchange_id="US_AND_INTL"):
    conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id, name, country) VALUES (?, 'X', 'US')",
                 (exchange_id,))
    conn.execute("INSERT OR IGNORE INTO companies (company_id, canonical_name) VALUES (?, ?)",
                 (entity_id, entity_id))
    sec_id = f"{entity_id}-common"
    conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, instrument_type) VALUES (?, ?, 'common_stock')",
                 (sec_id, entity_id))
    instrument_id = f"inst-{ticker.lower()}"
    conn.execute(
        "INSERT OR IGNORE INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) VALUES (?,?,?,?,?)",
        (instrument_id, sec_id, exchange_id, ticker, asset_class))
    conn.execute("""
        INSERT INTO canonical_events
        (canonical_event_id, event_type, category, lifecycle_state, corroboration_state, first_reported_at)
        VALUES (?, 'acquisition', 'corporate_action', 'reported', 'single_source', ?)
    """, (canonical_event_id, PUB.isoformat()))
    conn.execute(
        "INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) VALUES (?, ?, 'primary')",
        (canonical_event_id, entity_id))
    conn.commit()
    return instrument_id


class TestResolveEventInstruments(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_schema(self.conn)
        initialize_fusion_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_resolves_instrument_for_primary_participant(self):
        seed_one_event(self.conn)
        rows = resolve_event_instruments(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "NVDA")

    def test_secondary_participant_is_not_used_for_resolution(self):
        instrument_id = seed_one_event(self.conn)
        self.conn.execute(
            "INSERT INTO companies (company_id, canonical_name) VALUES ('other', 'Other Co')")
        self.conn.execute(
            "INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) VALUES ('ce-1', 'other', 'secondary')")
        self.conn.commit()
        rows = resolve_event_instruments(self.conn)
        # Still exactly one row: the secondary participant has no
        # instrument chain and must not produce a second row or
        # override the primary's.
        self.assertEqual(len(rows), 1)

    def test_event_without_resolvable_instrument_is_excluded(self):
        self.conn.execute("""
            INSERT INTO canonical_events
            (canonical_event_id, event_type, category, lifecycle_state, corroboration_state)
            VALUES ('ce-orphan', 'acquisition', 'corporate_action', 'reported', 'single_source')
        """)
        self.conn.execute(
            "INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) VALUES ('ce-orphan', 'ghost', 'primary')")
        self.conn.commit()
        rows = resolve_event_instruments(self.conn)
        self.assertEqual(rows, [])


class TestAnchorForEvent(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_schema(self.conn)
        initialize_fusion_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_anchor_falls_back_to_first_reported_at(self):
        seed_one_event(self.conn)
        anchor = anchor_for_event(self.conn, "ce-1")
        self.assertEqual(anchor, PUB)

    def test_missing_event_returns_none(self):
        self.assertIsNone(anchor_for_event(self.conn, "does-not-exist"))


class TestRangeCaching(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_price_cache_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_uncached_range_is_not_cached(self):
        self.assertFalse(is_range_cached(self.conn, "inst-1", "1d", PUB, PUB))

    def test_recorded_range_is_recognized_as_cached(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        record_request(self.conn, "inst-1", "1d", start, end, 150)
        # A narrower request fully inside the recorded range is covered.
        inner_start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        inner_end = datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.assertTrue(is_range_cached(self.conn, "inst-1", "1d", inner_start, inner_end))

    def test_partially_overlapping_range_is_not_considered_cached(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 1, tzinfo=timezone.utc)
        record_request(self.conn, "inst-1", "1d", start, end, 40)
        later_start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        later_end = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self.assertFalse(is_range_cached(self.conn, "inst-1", "1d", later_start, later_end))

    def test_empty_result_is_still_recorded_as_cached(self):
        # A range that legitimately came back with zero candles (e.g.
        # market closed the whole window) must not be re-requested
        # forever.
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        record_request(self.conn, "inst-1", "1d", start, end, 0)
        self.assertTrue(is_range_cached(self.conn, "inst-1", "1d", start, end))

    def test_store_candles_is_idempotent(self):
        candles = [Candle(timestamp=PUB, open_=1, high=2, low=0.5, close=1.5,
                          volume=100, adjusted_close=1.5)]
        n1 = store_candles(self.conn, "inst-1", "1d", candles)
        n2 = store_candles(self.conn, "inst-1", "1d", candles)
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM price_candle_cache").fetchone()[0]
        self.assertEqual((n1, n2, count), (1, 1, 1))


class TestMainDryRun(unittest.TestCase):
    """--dry-run must never call the network and never write."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        initialize_schema(conn)
        initialize_fusion_schema(conn)
        seed_one_event(conn)
        conn.close()

    def tearDown(self):
        os.remove(self.db_path)

    def test_dry_run_makes_no_network_call_and_writes_nothing(self):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path, "--dry-run"]
        try:
            with patch("src.impact.polygon_connector.PolygonConnector.get_daily_candles") as daily, \
                 patch("src.impact.polygon_connector.PolygonConnector.get_minute_candles") as minute:
                result = main()
            daily.assert_not_called()
            minute.assert_not_called()
        finally:
            sys.argv = argv
        self.assertEqual(result, 0)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM price_candle_cache").fetchone()[0], 0)
        conn.close()

    def test_missing_api_key_without_dry_run_refuses(self):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path]
        env_backup = os.environ.pop("POLYGON_API_KEY", None)
        try:
            result = main()
        finally:
            sys.argv = argv
            if env_backup is not None:
                os.environ["POLYGON_API_KEY"] = env_backup
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()


class TestCacheBenchmark(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        initialize_price_cache_schema(self.conn)
        self.connector = None  # not used in dry_run=True path

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_empty_anchors_makes_no_calls(self):
        from scripts.cache_price_candles import cache_benchmark
        result = cache_benchmark(self.conn, self.connector, [], dry_run=True)
        self.assertEqual(result, (0, 0, 0, 0))

    def test_dry_run_reports_planned_calls_without_fetching(self):
        from scripts.cache_price_candles import cache_benchmark
        anchors = [PUB, PUB.replace(day=2)]
        daily_calls, daily_rows, minute_calls, minute_rows = cache_benchmark(
            self.conn, self.connector, anchors, dry_run=True)
        self.assertEqual(daily_calls, 1)
        self.assertEqual(minute_calls, 2)  # two distinct days
        self.assertEqual((daily_rows, minute_rows), (0, 0))  # dry run fetches nothing

    def test_second_call_with_same_anchors_is_fully_cached(self):
        from scripts.cache_price_candles import cache_benchmark, BENCHMARK_INSTRUMENT_ID
        from unittest.mock import MagicMock
        connector = MagicMock()
        connector.get_daily_candles.return_value = []
        connector.get_minute_candles.return_value = []
        anchors = [PUB]
        cache_benchmark(self.conn, connector, anchors, dry_run=False)
        daily_calls, _, minute_calls, _ = cache_benchmark(self.conn, connector, anchors, dry_run=False)
        self.assertEqual((daily_calls, minute_calls), (0, 0))

    def test_benchmark_uses_reserved_instrument_id_not_a_real_instrument(self):
        from scripts.cache_price_candles import BENCHMARK_INSTRUMENT_ID
        # Must not collide with any real instrument_id format (inst-<ticker>).
        self.assertFalse(BENCHMARK_INSTRUMENT_ID.startswith("inst-"))
