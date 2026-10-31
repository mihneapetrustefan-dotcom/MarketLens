"""
tests/scripts/test_cache_refresh_horizon.py
-----------------------------------------------------------
The price-cache refresh fix (Phase 25.9C, §8, §9, §24).

THE DEFECT. A daily request reaches anchor + 35 days, and a recorded
range counted as covered to its `range_end` even when that end was in
the future when requested. On production, every protected instrument
and the SPY benchmark carried such a range, so their forward prices
would never have been fetched and the d20 labels could never resolve.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.cache_price_candles import (
    VINTAGE_OVERLAP_DAYS, check_vintage, covered_through, is_range_cached,
    plan_daily_fetch, record_request,
)
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.impact.engine import Candle

UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_price_cache_schema(self.conn)

    def record(self, start, end, requested_at):
        self.conn.execute("INSERT INTO price_cache_requests VALUES (?,?,?,?,?,?)",
                          ("i", "1d", at(start).isoformat(), at(end).isoformat(), 10,
                           at(requested_at).isoformat()))


class TestFutureEndsAreNotCoverage(CacheCase):

    def test_the_production_shape_is_no_longer_treated_as_complete(self):
        """Requested 09-04 for a range ending 10-01: 09-05..10-01 never existed."""
        self.record("2025-11-01", "2026-10-01", "2026-09-04T12:00:00")
        self.assertFalse(is_range_cached(self.conn, "i", "1d", at("2025-11-01"), at("2026-10-01")))
        self.assertTrue(is_range_cached(self.conn, "i", "1d", at("2025-11-01"), at("2026-09-03")))

    def test_a_past_range_is_still_cached(self):
        """The existing behaviour for genuinely complete ranges is kept."""
        self.record("2026-01-01", "2026-03-01", "2026-09-01T00:00:00")
        self.assertTrue(is_range_cached(self.conn, "i", "1d", at("2026-01-01"), at("2026-03-01")))

    def test_covered_through_stops_at_the_request_time(self):
        self.record("2025-11-01", "2026-10-01", "2026-09-04T12:00:00")
        self.assertEqual(covered_through(self.conn, "i", "1d", at("2025-11-01")),
                         at("2026-09-04T12:00:00"))


class TestPlan(CacheCase):

    def test_nothing_recorded_is_a_full_fetch(self):
        plan = plan_daily_fetch(self.conn, "i", at("2026-01-01"), at("2026-10-01"),
                                at("2026-10-15"))
        self.assertEqual(plan, (at("2026-01-01"), at("2026-10-01"), False))

    def test_a_stale_range_fetches_only_the_tail_with_an_overlap(self):
        self.record("2026-01-01", "2026-10-01", "2026-09-04T12:00:00")
        start, end, incremental = plan_daily_fetch(
            self.conn, "i", at("2026-01-01"), at("2026-10-01"), at("2026-10-15"))
        self.assertTrue(incremental)
        self.assertEqual(start, at("2026-09-04T12:00:00") - timedelta(days=VINTAGE_OVERLAP_DAYS))
        self.assertEqual(end, at("2026-10-01"))

    def test_never_asks_for_the_future(self):
        plan = plan_daily_fetch(self.conn, "i", at("2026-01-01"), at("2026-12-01"),
                                at("2026-10-15"))
        self.assertEqual(plan[1], at("2026-10-15"))

    def test_a_fully_observed_range_is_skipped(self):
        self.record("2026-01-01", "2026-10-01", "2026-10-10T00:00:00")
        self.assertIsNone(plan_daily_fetch(self.conn, "i", at("2026-01-01"), at("2026-10-01"),
                                           at("2026-10-15")))

    def test_recorded_ends_never_lie_about_the_future_again(self):
        """After a real refresh the stored end is the capped one."""
        now = at("2026-09-14")
        _s, end, _i = plan_daily_fetch(self.conn, "i", at("2026-01-01"), at("2026-10-01"), now)
        record_request(self.conn, "i", "1d", at("2026-01-01"), end, 5)
        stored = self.conn.execute("SELECT range_end FROM price_cache_requests").fetchone()[0]
        self.assertLessEqual(datetime.fromisoformat(stored), now)


class TestVintage(CacheCase):

    def store(self, day, close):
        self.conn.execute("INSERT INTO price_candle_cache (instrument_id, interval, timestamp, "
                          "open, high, low, close, adjusted_close, volume, source, fetched_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          ("i", "1d", at(day).isoformat(), close, close, close, close, close,
                           1, "test", "2026-09-01T00:00:00+00:00"))

    def test_an_unchanged_overlap_is_consistent(self):
        self.store("2026-09-01", 100.0)
        fresh = [Candle(timestamp=at("2026-09-01"), close=100.2)]
        self.assertTrue(check_vintage(self.conn, "i", fresh, at("2026-09-10"), at("2026-10-01")))

    def test_a_split_between_fetches_is_detected_and_recorded(self):
        self.store("2026-09-01", 100.0)
        fresh = [Candle(timestamp=at("2026-09-01"), close=50.0)]     # 2:1 split re-adjusted
        self.assertFalse(check_vintage(self.conn, "i", fresh, at("2026-09-10"), at("2026-10-01")))
        row = self.conn.execute("SELECT consistent, max_relative_change "
                                "FROM price_cache_vintage_checks").fetchone()
        self.assertEqual(row[0], 0)
        self.assertAlmostEqual(row[1], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
