"""
tests/test_dashboard_prices.py
------------------------------------
Price history read from the database, and the query that made a
rebuild take eight minutes.

WHAT THESE DEFEND
---------------------
1. That a dashboard built from the database ALONE shows prices.

   The instrument page rendered its price block from `market_data` --
   live quotes that exist only in memory during a run_daily.py run --
   and fell back to "price_candle_cache — indisponibil" whenever that
   was absent. `price_history` was likewise passed in and never read
   back. So every rebuild_dashboard run reported no price for any
   instrument while price_candle_cache held 116,719 candles. Honda
   Motor showed "indisponibil" with 196 candles stored.

2. That a stored close is LABELLED as one. Making 292 charts appear is
   only an improvement if none of them claims to be live.

3. That the latest-recommendation lookup does not re-scan the table
   once per row. It took 505 seconds on the real corpus.
"""

import os
import sqlite3
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator


def blank_generator():
    return DashboardGenerator.__new__(DashboardGenerator)


class PriceCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE companies (company_id TEXT PRIMARY KEY,
                canonical_name TEXT, aliases_json TEXT, sector_id TEXT);
            CREATE TABLE securities (security_id TEXT PRIMARY KEY,
                company_id TEXT, instrument_type TEXT, currency TEXT);
            CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY,
                security_id TEXT, exchange_id TEXT, ticker TEXT, asset_class TEXT);
            CREATE TABLE price_candle_cache (instrument_id TEXT, interval TEXT,
                timestamp TEXT, open REAL, high REAL, low REAL, close REAL,
                adjusted_close REAL, volume REAL, source TEXT, fetched_at TEXT);
        """)
        self.conn.execute("INSERT INTO companies VALUES ('honda','Honda Motor','[]','auto')")
        self.conn.execute("INSERT INTO securities VALUES ('honda-common','honda','common','USD')")
        self.conn.execute("INSERT INTO instruments VALUES "
                          "('us_and_intl-hmc','honda-common','US_AND_INTL','HMC','stock')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def add_candles(self, closes, interval="1d", instrument="us_and_intl-hmc",
                    adjusted=None):
        # Real consecutive dates. Formatting the index straight into the
        # day field gives "2026-08-100", which is not a date and sorts
        # BEFORE "2026-08-99" as a string -- a broken fixture that looks
        # like a broken collector.
        start = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
        for i, close in enumerate(closes):
            stamp = (start + timedelta(days=i)).isoformat()
            self.conn.execute(
                "INSERT INTO price_candle_cache "
                "(instrument_id, interval, timestamp, close, adjusted_close) "
                "VALUES (?,?,?,?,?)",
                (instrument, interval, stamp, close,
                 adjusted[i] if adjusted else None))
        self.conn.commit()


class TestHistoryIsReadFromTheDatabase(PriceCase):

    def test_candles_become_a_series(self):
        self.add_candles([10.0, 11.0, 12.0])
        history, summary = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(history["Honda Motor"], [10.0, 11.0, 12.0])

    def test_the_summary_is_keyed_by_ticker_and_history_by_name(self):
        """
        The page looks up `market_data[sel.t]` and
        `price_history[sel.n]`. Keying either the other way produces a
        silent miss that looks exactly like missing data.
        """
        self.add_candles([10.0, 11.0])
        history, summary = blank_generator()._collect_price_history(self.conn)
        self.assertIn("Honda Motor", history)
        self.assertIn("HMC", summary)

    def test_the_last_close_and_change_are_computed(self):
        self.add_candles([100.0, 110.0])
        _, summary = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(summary["HMC"]["current_price"], 110.0)
        self.assertAlmostEqual(summary["HMC"]["daily_change_pct"], 10.0)

    def test_a_single_candle_has_no_change_rather_than_zero(self):
        """
        One point cannot yield a change. Zero would read as "flat",
        which is a claim the data does not support.
        """
        self.add_candles([42.0])
        _, summary = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(summary["HMC"]["current_price"], 42.0)
        self.assertIsNone(summary["HMC"]["daily_change_pct"])

    def test_adjusted_close_wins_when_present(self):
        self.add_candles([10.0, 11.0], adjusted=[9.5, 10.5])
        history, _ = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(history["Honda Motor"], [9.5, 10.5])

    def test_intraday_bars_are_excluded(self):
        """A sparkline shows daily bars; intraday rows would swamp it."""
        self.add_candles([10.0, 11.0])
        self.add_candles([99.0] * 50, interval="5m")
        history, _ = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(history["Honda Motor"], [10.0, 11.0])

    def test_the_series_is_capped(self):
        """
        The first cut kept 120 points as objects and produced 1,835 KB
        of embedded JSON on a 278 KB page. Flat closes, capped.
        """
        self.add_candles([float(i) for i in range(200)])
        history, summary = blank_generator()._collect_price_history(self.conn)
        self.assertEqual(len(history["Honda Motor"]), 90)
        self.assertEqual(summary["HMC"]["points"], 90)
        self.assertEqual(history["Honda Motor"][-1], 199.0)

    def test_the_series_is_numbers_not_objects(self):
        self.add_candles([10.0, 11.0])
        history, _ = blank_generator()._collect_price_history(self.conn)
        for value in history["Honda Motor"]:
            self.assertIsInstance(value, float)

    def test_an_instrument_with_no_candles_is_absent_not_empty(self):
        history, summary = blank_generator()._collect_price_history(self.conn)
        self.assertNotIn("Honda Motor", history)
        self.assertNotIn("HMC", summary)

    def test_a_missing_table_returns_empty_rather_than_raising(self):
        self.conn.execute("DROP TABLE price_candle_cache")
        self.conn.commit()
        history, summary = blank_generator()._collect_price_history(self.conn)
        self.assertEqual((history, summary), ({}, {}))


class TestItDoesNotClaimToBeLive(PriceCase):
    """
    Making 292 charts appear is only an improvement if none of them
    passes a three-day-old close off as today's price.
    """

    def test_every_cached_price_is_flagged(self):
        self.add_candles([10.0, 11.0])
        _, summary = blank_generator()._collect_price_history(self.conn)
        self.assertIs(summary["HMC"]["from_cache"], True)

    def test_the_date_of_the_close_is_carried(self):
        self.add_candles([10.0, 11.0])
        _, summary = blank_generator()._collect_price_history(self.conn)
        self.assertTrue(summary["HMC"]["as_of"].startswith("2026-01-02"))


class TestLatestRecommendationLookup(unittest.TestCase):
    """
    _collect_rec_index took 505 SECONDS on the real corpus: a
    correlated subquery re-scanned all 22,725 rows once per row to find
    each entity's maximum. Roughly half a billion row reads to produce
    389 results.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE recommendations (id INTEGER PRIMARY KEY, entity TEXT,
                ticker TEXT, recommendation TEXT, confidence_score REAL,
                generated_at TEXT, time_horizon TEXT, checked_at TEXT,
                was_correct INTEGER)
        """)

    def tearDown(self):
        self.conn.close()

    def add(self, entity, when, rec="BUY", conf=0.5):
        self.conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, "
            "confidence_score, generated_at, time_horizon) VALUES (?,?,?,?,?,?)",
            (entity, entity[:4].upper(), rec, conf, when, "long-term"))
        self.conn.commit()

    def test_the_newest_recommendation_per_entity_wins(self):
        self.add("Apple", "2026-01-01T00:00:00+00:00", "SELL")
        self.add("Apple", "2026-09-01T00:00:00+00:00", "BUY")
        index = blank_generator()._collect_rec_index(self.conn)
        self.assertEqual(index["Apple"]["recommendation"], "BUY")

    def test_every_entity_appears_once(self):
        for entity in ("Apple", "Honda Motor", "Nvidia"):
            self.add(entity, "2026-01-01T00:00:00+00:00")
            self.add(entity, "2026-09-01T00:00:00+00:00")
        index = blank_generator()._collect_rec_index(self.conn)
        self.assertEqual(len(index), 3)

    def test_it_does_not_rescan_the_table_once_per_row(self):
        """
        A performance assertion, deliberately.

        The correlated form is O(n^2) and produced identical RESULTS,
        so no correctness test would have caught it. 4,000 rows takes
        well under a second with one aggregate pass and many seconds
        with a re-scan per row.
        """
        rows = [("E%03d" % (i % 400), "2026-%02d-01T00:00:00+00:00" % (i % 12 + 1))
                for i in range(4000)]
        self.conn.executemany(
            "INSERT INTO recommendations (entity, ticker, recommendation, "
            "confidence_score, generated_at, time_horizon) "
            "VALUES (?,?,'BUY',0.5,?,'long-term')",
            [(e, e, w) for e, w in rows])
        self.conn.commit()

        started = time.time()
        index = blank_generator()._collect_rec_index(self.conn)
        elapsed = time.time() - started

        self.assertEqual(len(index), 400)
        self.assertLess(elapsed, 3.0,
                        f"took {elapsed:.1f}s for 4,000 rows — the "
                        f"per-row re-scan is back")

    def test_a_missing_table_returns_empty(self):
        self.conn.execute("DROP TABLE recommendations")
        self.conn.commit()
        self.assertEqual(blank_generator()._collect_rec_index(self.conn), {})


if __name__ == "__main__":
    unittest.main()
