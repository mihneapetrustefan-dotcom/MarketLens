"""
tests/portfolio/test_valuation.py
--------------------------------------
Point-in-time pricing, and the leakage tests that protect it.

THESE ARE THE MOST IMPORTANT TESTS IN THE PHASE
--------------------------------------------------
Every other guarantee rests on the anchor being real. A risk engine
that can see tomorrow's price produces decisions that look excellent
and mean nothing, and the failure is invisible in the output — the
numbers are all plausible.

So the leakage tests here do not merely check that a future candle is
unused. They check that it is INVISIBLE: the same query, run against a
database that contains the future, returns exactly what it returns
against a database that does not.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.portfolio_models import ValuationStatus
from src.portfolio.valuation import (
    DEFAULT_MAX_PRICE_AGE_DAYS, PortfolioValuator, PriceRepository,
)
from tests.portfolio.helpers import (
    AS_OF, add_candles, add_instrument, make_connection, make_position,
)


class TestPriceLookup(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        add_candles(self.conn, "i-a", AS_OF, days=30, start_price=100.0)
        self.prices = PriceRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_the_latest_price_at_or_before_the_anchor(self):
        point = self.prices.price_as_of("i-a", AS_OF)
        self.assertIsNotNone(point)
        self.assertLessEqual(point.timestamp, AS_OF)

    def test_unknown_instrument_returns_none_not_zero(self):
        self.assertIsNone(self.prices.price_as_of("i-missing", AS_OF))

    def test_anchor_before_all_history_returns_none(self):
        self.assertIsNone(self.prices.price_as_of("i-a", AS_OF - timedelta(days=400)))

    def test_naive_anchor_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prices.price_as_of("i-a", datetime(2026, 8, 27))

    def test_batch_lookup_returns_one_entry_per_instrument(self):
        add_instrument(self.conn, "i-b", "BBB", "energy")
        add_candles(self.conn, "i-b", AS_OF, days=30, start_price=50.0, seed=9)
        found = self.prices.prices_as_of(["i-a", "i-b", "i-missing"], AS_OF)
        self.assertEqual(set(found), {"i-a", "i-b"})

    def test_missing_price_table_is_reported_as_no_prices(self):
        bare = make_connection()
        bare.execute("DROP TABLE price_candle_cache")
        self.assertEqual(PriceRepository(bare).prices_as_of(["i-a"], AS_OF), {})
        bare.close()


class TestLookAheadPrevention(unittest.TestCase):
    """Spec §44: information after the anchor must be unreachable, not merely unused."""

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        # History up to the anchor, at a known level.
        add_candles(self.conn, "i-a", AS_OF, days=30,
                    prices=[100.0] * 30)
        self.prices = PriceRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_future_candle(self, price: float, days_ahead: int = 1):
        future = (AS_OF + timedelta(days=days_ahead)).replace(hour=4, minute=0)
        self.conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("i-a", "1d", future.isoformat(), price, price, price, price, price,
             1_000_000.0, "test", AS_OF.isoformat()))
        self.conn.commit()

    def test_a_future_candle_is_invisible_to_a_price_lookup(self):
        before = self.prices.price_as_of("i-a", AS_OF)
        self._insert_future_candle(9_999.0)
        after = self.prices.price_as_of("i-a", AS_OF)
        self.assertEqual(before.price, after.price)
        self.assertEqual(after.price, 100.0)

    def test_a_future_candle_does_not_lengthen_the_return_series(self):
        before = self.prices.return_series_batch(["i-a"], AS_OF, 365).get("i-a", [])
        self._insert_future_candle(9_999.0)
        after = self.prices.return_series_batch(["i-a"], AS_OF, 365).get("i-a", [])
        self.assertEqual(len(before), len(after))

    def test_no_returned_observation_postdates_the_anchor(self):
        self._insert_future_candle(120.0)
        for _, points in self.prices.close_series_batch(["i-a"], AS_OF, 365).items():
            for point in points:
                self.assertLessEqual(point.timestamp, AS_OF)

    def test_an_earlier_anchor_sees_strictly_less(self):
        full = self.prices.close_series_batch(["i-a"], AS_OF, 365)["i-a"]
        earlier = self.prices.close_series_batch(
            ["i-a"], AS_OF - timedelta(days=10), 365)["i-a"]
        self.assertLess(len(earlier), len(full))

    def test_lookback_window_bounds_the_series_on_both_sides(self):
        points = self.prices.close_series_batch(["i-a"], AS_OF, 10)["i-a"]
        for point in points:
            self.assertGreaterEqual(point.timestamp, AS_OF - timedelta(days=10))
            self.assertLessEqual(point.timestamp, AS_OF)


class TestPriceQuality(unittest.TestCase):
    """Corrupt or unusable cache rows must not become plausible prices."""

    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        self.prices = PriceRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert(self, price, days_ago=1):
        timestamp = (AS_OF - timedelta(days=days_ago)).replace(hour=4, minute=0)
        self.conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("i-a", "1d", timestamp.isoformat(), price, price, price, price, price,
             1000.0, "test", AS_OF.isoformat()))
        self.conn.commit()

    def test_zero_price_is_not_used(self):
        self._insert(0.0)
        self.assertIsNone(self.prices.price_as_of("i-a", AS_OF))

    def test_negative_price_is_not_used(self):
        self._insert(-5.0)
        self.assertIsNone(self.prices.price_as_of("i-a", AS_OF))

    def test_null_price_is_not_used(self):
        self._insert(None)
        self.assertIsNone(self.prices.price_as_of("i-a", AS_OF))

    def test_adjusted_close_is_preferred_over_close(self):
        timestamp = (AS_OF - timedelta(days=1)).replace(hour=4, minute=0)
        self.conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("i-a", "1d", timestamp.isoformat(), 10.0, 10.0, 10.0,
             200.0, 100.0, 1000.0, "test", AS_OF.isoformat()))
        self.conn.commit()
        self.assertEqual(self.prices.price_as_of("i-a", AS_OF).price, 100.0)

    def test_iso_timestamps_sort_lexicographically_as_they_do_chronologically(self):
        """
        The SQL anchor relies on string comparison. This asserts the
        property the whole point-in-time barrier depends on, rather
        than leaving it as an unstated assumption.
        """
        earlier = (AS_OF - timedelta(days=40)).replace(hour=4, minute=0).isoformat()
        later = (AS_OF - timedelta(days=1)).replace(hour=4, minute=0).isoformat()
        self.assertLess(earlier, later)
        self.assertEqual(len(earlier), len(later))


class TestValuationStaleness(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "i-a", "AAA", "technology")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _value(self, days_ago, quantity=10.0):
        timestamp = (AS_OF - timedelta(days=days_ago)).replace(hour=4, minute=0)
        self.conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("i-a", "1d", timestamp.isoformat(), 50.0, 50.0, 50.0, 50.0, 50.0,
             1000.0, "test", AS_OF.isoformat()))
        self.conn.commit()
        valuator = PortfolioValuator(PriceRepository(self.conn))
        return valuator.value_positions(
            [make_position("i-a", quantity)], AS_OF)[0]

    def test_recent_price_is_valued(self):
        valuation = self._value(days_ago=1)
        self.assertEqual(valuation.status, ValuationStatus.VALUED)
        self.assertTrue(valuation.is_valued)

    def test_old_price_is_marked_stale_but_still_carries_a_value(self):
        valuation = self._value(days_ago=int(DEFAULT_MAX_PRICE_AGE_DAYS) + 10)
        self.assertEqual(valuation.status, ValuationStatus.STALE_PRICE)
        self.assertIsNotNone(valuation.market_value)
        self.assertEqual(valuation.market_value, 500.0)

    def test_missing_price_yields_missing_status_and_no_value(self):
        valuator = PortfolioValuator(PriceRepository(self.conn))
        valuation = valuator.value_positions([make_position("i-none", 10.0)], AS_OF)[0]
        self.assertEqual(valuation.status, ValuationStatus.MISSING_PRICE)
        self.assertIsNone(valuation.market_value)

    def test_every_position_gets_a_valuation_even_when_unpriceable(self):
        """A valuator that silently dropped rows would make a partial book look whole."""
        valuator = PortfolioValuator(PriceRepository(self.conn))
        valuations = valuator.value_positions(
            [make_position("i-a", 1.0), make_position("i-none", 1.0)], AS_OF)
        self.assertEqual(len(valuations), 2)

    def test_price_age_is_reported(self):
        valuation = self._value(days_ago=3)
        self.assertIsNotNone(valuation.price_age_days)
        self.assertGreater(valuation.price_age_days, 2.0)


if __name__ == "__main__":
    unittest.main()
