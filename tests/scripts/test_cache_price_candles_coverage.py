"""
tests/scripts/test_cache_price_candles_coverage.py
--------------------------------------------------------------
`--include-unstudied`: prices for instruments no event has touched.

WHY THIS OPTION EXISTS
--------------------------
The cache fetches prices for instruments that are the PRIMARY
participant of a canonical event, because it serves event studies.
That left 97 of 389 registry instruments with no candles at all, and
the dashboard -- a browsing surface over the WHOLE registry -- showed a
blank chart on a quarter of its company pages.

A requirement mismatch, not a bug: two consumers wanting different
coverage from one table.

WHAT THESE DEFEND
---------------------
That the option adds only what it should: instruments with no candles,
excluding the ones Polygon does not cover, without re-fetching anything
already held. And that display-only instruments skip minute candles,
which halved the cost (126 requests to 64) because nothing renders
intraday bars for a company page.
"""

import importlib.util
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_spec = importlib.util.spec_from_file_location(
    "cache_price_candles",
    os.path.join(os.path.dirname(__file__), "..", "..",
                 "scripts", "cache_price_candles.py"))
cache_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cache_mod)


class UnstudiedResolutionCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY,
                security_id TEXT, exchange_id TEXT, ticker TEXT, asset_class TEXT);
            CREATE TABLE price_candle_cache (instrument_id TEXT, interval TEXT,
                timestamp TEXT, open REAL, high REAL, low REAL, close REAL,
                adjusted_close REAL, volume REAL, source TEXT, fetched_at TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def add_instrument(self, instrument_id, ticker, asset_class="stock"):
        self.conn.execute(
            "INSERT INTO instruments VALUES (?,?,?,?,?)",
            (instrument_id, instrument_id + "-sec", "X", ticker, asset_class))
        self.conn.commit()

    def add_candle(self, instrument_id):
        self.conn.execute(
            "INSERT INTO price_candle_cache (instrument_id, interval, timestamp, close) "
            "VALUES (?, '1d', '2026-01-01T00:00:00+00:00', 10.0)", (instrument_id,))
        self.conn.commit()

    def resolve(self):
        return cache_mod.resolve_unstudied_instruments(self.conn)


class TestWhatGetsAdded(UnstudiedResolutionCase):

    def test_an_instrument_with_no_candles_is_returned(self):
        self.add_instrument("us_and_intl-azo", "AZO")
        self.assertEqual([r[0] for r in self.resolve()], ["us_and_intl-azo"])

    def test_an_instrument_that_already_has_candles_is_not(self):
        """
        Keyed on "has no candles", not "has no event". An instrument
        studied once already holds its history, and re-fetching it
        spends a call to learn nothing.
        """
        self.add_instrument("us_and_intl-aapl", "AAPL")
        self.add_candle("us_and_intl-aapl")
        self.assertEqual(self.resolve(), [])

    def test_the_ticker_and_asset_class_come_back(self):
        """The caller needs both to build a Polygon symbol."""
        self.add_instrument("crypto-link", "LINK", "crypto")
        row = self.resolve()[0]
        self.assertEqual(row[1], "LINK")
        self.assertEqual(row[2], "crypto")

    def test_an_empty_registry_returns_nothing_rather_than_raising(self):
        self.assertEqual(self.resolve(), [])

    def test_results_are_ordered_so_a_limit_is_reproducible(self):
        for suffix in ("zz", "aa", "mm"):
            self.add_instrument(f"us_and_intl-{suffix}", suffix.upper())
        ids = [r[0] for r in self.resolve()]
        self.assertEqual(ids, sorted(ids))


class TestWhatStaysExcluded(UnstudiedResolutionCase):
    """
    BVB is not skipped by this query — it is skipped downstream by
    `normalize_ticker_for_polygon` returning None, which is the one
    place that knows what Polygon covers. Asserted here so the
    exclusion is not quietly duplicated into a second place.
    """

    def test_polygon_does_not_cover_bucharest(self):
        self.assertIsNone(
            cache_mod.normalize_ticker_for_polygon("TLV", "bvb"))

    def test_a_us_equity_normalises(self):
        self.assertIsNotNone(
            cache_mod.normalize_ticker_for_polygon("AAPL", "stock"))

    def test_the_resolver_itself_does_not_filter_by_exchange(self):
        """
        It returns BVB rows too; the caller drops them by symbol. One
        definition of "Polygon covers this", not two.
        """
        self.add_instrument("bvb-tlv", "TLV", "bvb")
        self.assertEqual(len(self.resolve()), 1)


if __name__ == "__main__":
    unittest.main()
