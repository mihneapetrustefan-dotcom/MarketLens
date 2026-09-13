"""
tests/impact/test_anchoring.py
-----------------------------------------------------------
Post-event window anchoring, v1 and v2 (Phase 25.9B, §29).

Built from synthetic candles that mirror the traced production study
es-86b69ecaf738ef05, so these run without a database and stay
meaningful if that study is ever rebuilt.

The production values the v1 cases reproduce were verified against
`event_study_returns` directly: resolve_v1 matched all five stored
windows exactly.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.impact_models import DEFAULT_WINDOWS
from src.impact.anchoring import (
    ANCHOR_METHOD_V1, ANCHOR_METHOD_V2, resolve_v1, resolve_v2,
)
from src.impact.engine import Candle

W = {w.name: w for w in DEFAULT_WINDOWS}
UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def daily(date_text, close):
    return Candle(timestamp=at(f"{date_text}T04:00:00"), close=close)


def minute(ts_text, close):
    return Candle(timestamp=at(ts_text), close=close)


def trading_days(start, count):
    """Weekday session dates, as daily-candle timestamps at 04:00 UTC."""
    days, day = [], at(f"{start}T04:00:00")
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


class TheTracedStudy(unittest.TestCase):
    """
    A Saturday event. Friday has a daily close of 360.07 and a
    pre-market minute print of 355.94 at 08:03 UTC; Monday has minute
    candles; the market is shut in between.
    """

    def setUp(self):
        self.anchor = at("2026-08-01T10:02:40")          # Saturday
        sessions = trading_days("2026-07-27", 30)
        self.sessions = sessions
        self.daily = [Candle(timestamp=s, close=300.0 + i)
                      for i, s in enumerate(sessions)]
        for candle in self.daily:
            if candle.timestamp.date().isoformat() == "2026-07-31":
                candle.close = 360.07                       # Friday close
        self.minute = [minute("2026-07-31T08:03:00", 355.94),   # Fri pre-market
                       minute("2026-08-03T13:30:00", 368.93)]   # Mon open
        self.merged = sorted(self.daily + self.minute, key=lambda c: c.timestamp)


class TestV1ReproducesTheDefects(TheTracedStudy):
    """v1 must keep doing exactly what it did, so it stays reproducible."""

    def test_v1_uses_the_stale_premarket_print_as_the_base(self):
        """Defect 1: a 4 a.m. ET minute trade, not Friday's 360.07 close."""
        result = resolve_v1(self.anchor, W["d20"], self.merged, self.sessions)
        self.assertEqual(result.method, ANCHOR_METHOD_V1)
        self.assertAlmostEqual(result.before.price, 355.94)

    def test_v1_gives_every_window_the_same_base(self):
        bases = {resolve_v1(self.anchor, W[n], self.merged, self.sessions).before.price
                 for n in ("intraday_5m", "intraday_60m", "d1", "d5", "d20")}
        self.assertEqual(bases, {355.94})

    def test_v1_snaps_intraday_windows_across_the_weekend(self):
        """
        Defect 2: both windows jump to the same post-weekend candle.

        Asserted by property rather than by price. Which Monday candle
        wins depends on what is cached; in production it was a 368.93
        minute candle (verified directly against event_study_returns).
        The defect is the snap and the collapse, whatever the price.
        """
        five = resolve_v1(self.anchor, W["intraday_5m"], self.merged, self.sessions)
        sixty = resolve_v1(self.anchor, W["intraday_60m"], self.merged, self.sessions)
        self.assertEqual(five.after.timestamp, sixty.after.timestamp)
        self.assertAlmostEqual(five.after.price, sixty.after.price)
        self.assertGreater(five.after.timestamp - five.before.timestamp,
                           timedelta(days=2))


class TestV2CorrectsThem(TheTracedStudy):

    def test_v2_uses_the_prior_session_close(self):
        result = resolve_v2(self.anchor, W["d20"], self.minute, self.daily,
                            self.sessions)
        self.assertEqual(result.method, ANCHOR_METHOD_V2)
        self.assertTrue(result.resolved)
        self.assertAlmostEqual(result.before.price, 360.07)

    def test_v2_does_not_move_the_window_end(self):
        """An anchor fix, not a target redefinition."""
        for name in ("d1", "d5", "d20"):
            v1 = resolve_v1(self.anchor, W[name], self.merged, self.sessions)
            v2 = resolve_v2(self.anchor, W[name], self.minute, self.daily,
                            self.sessions)
            self.assertAlmostEqual(v1.after.price, v2.after.price, msg=name)

    def test_v2_refuses_an_intraday_window_outside_a_session(self):
        """A Saturday event has no observable five minutes. Say so."""
        for name in ("intraday_5m", "intraday_60m"):
            result = resolve_v2(self.anchor, W[name], self.minute, self.daily,
                                self.sessions)
            self.assertFalse(result.resolved)
            self.assertIn("observable session", result.reason)

    def test_v2_never_fills_a_missing_window(self):
        result = resolve_v2(self.anchor, W["intraday_5m"], self.minute,
                            self.daily, self.sessions)
        self.assertIsNone(result.before)
        self.assertIsNone(result.after)


class TestV2IntradayInsideASession(unittest.TestCase):

    def setUp(self):
        self.anchor = at("2026-08-04T14:00:00")           # Tuesday, in session
        self.sessions = trading_days("2026-07-27", 30)
        self.daily = [Candle(timestamp=s, close=100.0) for s in self.sessions]
        self.minute = [minute("2026-08-04T13:59:00", 100.0),
                       minute("2026-08-04T14:05:00", 101.0),
                       minute("2026-08-04T15:00:00", 103.0)]

    def test_a_real_five_minute_window_resolves(self):
        result = resolve_v2(self.anchor, W["intraday_5m"], self.minute,
                            self.daily, self.sessions)
        self.assertTrue(result.resolved)
        self.assertAlmostEqual(result.before.price, 100.0)
        self.assertAlmostEqual(result.after.price, 101.0)

    def test_five_and_sixty_minutes_are_now_different_prices(self):
        five = resolve_v2(self.anchor, W["intraday_5m"], self.minute,
                          self.daily, self.sessions)
        sixty = resolve_v2(self.anchor, W["intraday_60m"], self.minute,
                           self.daily, self.sessions)
        self.assertNotEqual(five.after.price, sixty.after.price)

    def test_a_price_beyond_the_tolerance_is_refused(self):
        sparse = [minute("2026-08-04T13:40:00", 100.0),
                  minute("2026-08-04T14:05:00", 101.0)]
        result = resolve_v2(self.anchor, W["intraday_5m"], sparse,
                            self.daily, self.sessions)
        self.assertFalse(result.resolved)


class TestTradingDaySemantics(unittest.TestCase):

    def test_the_end_counts_sessions_not_calendar_days(self):
        """
        A Friday event's d1 must skip the weekend. Counting calendar
        days would silently shorten the window.
        """
        sessions = trading_days("2026-07-27", 30)
        daily = [Candle(timestamp=s, close=100.0 + i)
                 for i, s in enumerate(sessions)]
        anchor = at("2026-07-31T21:00:00")                # Friday evening
        result = resolve_v2(anchor, W["d1"], [], daily, sessions)
        self.assertTrue(result.resolved)
        self.assertEqual(result.after.timestamp.weekday(), 1)   # Tuesday
        self.assertNotIn(result.after.timestamp.weekday(), (5, 6))

    def test_an_unresolvable_horizon_is_named_not_guessed(self):
        sessions = trading_days("2026-07-27", 8)
        daily = [Candle(timestamp=s, close=100.0) for s in sessions]
        result = resolve_v2(at("2026-07-30T14:00:00"), W["d20"], [], daily,
                            sessions)
        self.assertFalse(result.resolved)
        self.assertIn("not yet resolvable", result.reason)


class TestPointInTime(unittest.TestCase):

    def test_the_base_is_never_a_close_from_the_event_day(self):
        """
        A same-day close may not have happened yet when the event
        arrives, so v2 only uses a close from a strictly earlier date.
        """
        sessions = trading_days("2026-07-27", 30)
        daily = [Candle(timestamp=s, close=100.0 + i)
                 for i, s in enumerate(sessions)]
        anchor = at("2026-08-04T14:00:00")
        result = resolve_v2(anchor, W["d5"], [], daily, sessions)
        self.assertLess(result.before.timestamp.date(), anchor.date())


if __name__ == "__main__":
    unittest.main(verbosity=2)
