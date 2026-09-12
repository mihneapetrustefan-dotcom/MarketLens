"""
tests/marketdata/test_bars.py
-----------------------------------------------------------
One-minute bar construction (Phase 25.7, §36 D).

The property that matters most here is that a bar is never invented.
A gap is a gap, a partial minute is labelled partial, and a late quote
never rewrites a minute a consumer may already have acted on.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.market_data_models import (
    MarketDataAvailability, OperationalQuote,
)
from src.marketdata.bars import MinuteBarBuilder, minute_floor

T0 = datetime(2026, 9, 12, 15, 0, 0, tzinfo=timezone.utc)


def q(seconds, price, instrument_id="aapl", volume=None):
    moment = T0 + timedelta(seconds=seconds)
    return OperationalQuote(
        instrument_id=instrument_id, conid="1", last=price, mid=price,
        volume=volume, availability=MarketDataAvailability.AVAILABLE,
        broker_at=moment, received_at=moment, evaluated_at=moment)


class TestMinuteBoundaries(unittest.TestCase):

    def test_minute_floor_truncates_seconds(self):
        self.assertEqual(
            minute_floor(T0 + timedelta(seconds=59, microseconds=999)),
            T0)

    def test_a_minute_in_progress_is_not_a_bar(self):
        """The core rule: completeness is decided by the clock."""
        builder = MinuteBarBuilder()
        self.assertEqual(builder.observe(q(5, 100.0)), [])
        # Still inside the same minute.
        self.assertEqual(builder.flush(T0 + timedelta(seconds=30)), [])
        self.assertIn("aapl", builder.open_minutes)


class TestNormalConstruction(unittest.TestCase):

    def test_ohlc_reflects_what_was_observed(self):
        builder = MinuteBarBuilder()
        for seconds, price in ((0, 100.0), (10, 103.0), (20, 98.0), (30, 101.0)):
            builder.observe(q(seconds, price))
        bars = builder.flush(T0 + timedelta(minutes=1, seconds=1))
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close),
                         (100.0, 103.0, 98.0, 101.0))
        self.assertEqual(bar.observation_count, 4)
        self.assertTrue(bar.is_complete)
        self.assertFalse(bar.is_gap)

    def test_a_single_observation_still_records_its_count(self):
        """
        A bar built from one sample is one price wearing OHLC clothing.
        The count is what lets a consumer tell.
        """
        builder = MinuteBarBuilder()
        builder.observe(q(5, 100.0))
        bar = builder.flush(T0 + timedelta(minutes=1, seconds=1))[0]
        self.assertEqual(bar.observation_count, 1)
        self.assertEqual(bar.open, bar.close)

    def test_crossing_a_minute_seals_the_previous_one(self):
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        completed = builder.observe(q(70, 105.0))
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].close, 100.0)
        self.assertTrue(completed[0].is_complete)


class TestGaps(unittest.TestCase):

    def test_a_missing_minute_is_recorded_not_interpolated(self):
        """
        An invented bar is indistinguishable from a real one once
        stored, so the hole is made explicit instead.
        """
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))               # 15:00
        completed = builder.observe(q(130, 110.0))  # 15:02, skipping 15:01
        gaps = [b for b in completed if b.is_gap]
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap.bar_start, T0 + timedelta(minutes=1))
        self.assertIsNone(gap.close)
        self.assertEqual(gap.observation_count, 0)

    def test_every_skipped_minute_gets_its_own_gap(self):
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))               # 15:00
        completed = builder.observe(q(250, 110.0))  # 15:04
        gaps = sorted(b.bar_start for b in completed if b.is_gap)
        self.assertEqual(gaps, [T0 + timedelta(minutes=1),
                                T0 + timedelta(minutes=2),
                                T0 + timedelta(minutes=3)])

    def test_a_gap_carries_no_prices_at_all(self):
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        completed = builder.observe(q(250, 110.0))
        for gap in [b for b in completed if b.is_gap]:
            self.assertIsNone(gap.open)
            self.assertIsNone(gap.high)
            self.assertIsNone(gap.low)
            self.assertIsNone(gap.close)


class TestOutOfOrderAndDuplicates(unittest.TestCase):

    def test_a_quote_for_a_sealed_minute_is_refused(self):
        """A late arrival must not move a bar already published."""
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        builder.observe(q(70, 105.0))               # seals 15:00
        before = builder.rejected[:]
        completed = builder.observe(q(20, 999.0))   # late, for 15:00
        self.assertEqual(completed, [])
        self.assertGreater(len(builder.rejected), len(before))
        self.assertIn("sealed", builder.rejected[-1])

    def test_an_older_quote_within_the_open_minute_does_not_set_close(self):
        builder = MinuteBarBuilder()
        builder.observe(q(30, 100.0))
        builder.observe(q(10, 90.0))                # older, same minute
        bar = builder.flush(T0 + timedelta(minutes=1, seconds=1))[0]
        self.assertEqual(bar.close, 100.0)
        self.assertIn("out-of-order", builder.rejected[-1])

    def test_a_duplicate_quote_is_folded_not_rejected(self):
        """
        Same timestamp twice is not out of order; it is the poller
        seeing an unchanged quote. It counts as an observation.
        """
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        builder.observe(q(10, 100.0))
        bar = builder.flush(T0 + timedelta(minutes=1, seconds=1))[0]
        self.assertEqual(bar.observation_count, 2)
        self.assertEqual(bar.close, 100.0)

    def test_a_quote_with_no_price_is_rejected_with_a_reason(self):
        builder = MinuteBarBuilder()
        empty = OperationalQuote(
            instrument_id="aapl", availability=MarketDataAvailability.UNAVAILABLE,
            received_at=T0, evaluated_at=T0)
        self.assertEqual(builder.observe(empty), [])
        self.assertIn("no price", builder.rejected[-1])


class TestSessionClose(unittest.TestCase):

    def test_the_final_partial_minute_is_labelled_incomplete(self):
        """
        Real data, honestly labelled. It must not be mistaken for a
        closed minute by a strategy expecting one.
        """
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        bars = builder.flush(T0 + timedelta(seconds=40),
                             force_incomplete=True)
        self.assertEqual(len(bars), 1)
        self.assertFalse(bars[0].is_complete)
        self.assertEqual(bars[0].close, 100.0)

    def test_flush_without_force_emits_nothing_mid_minute(self):
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0))
        self.assertEqual(builder.flush(T0 + timedelta(seconds=40)), [])


class TestMultipleInstruments(unittest.TestCase):

    def test_instruments_do_not_share_state(self):
        builder = MinuteBarBuilder()
        builder.observe(q(10, 100.0, instrument_id="aapl"))
        builder.observe(q(10, 200.0, instrument_id="msft"))
        bars = builder.flush(T0 + timedelta(minutes=1, seconds=1))
        closes = {b.instrument_id: b.close for b in bars}
        self.assertEqual(closes, {"aapl": 100.0, "msft": 200.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
