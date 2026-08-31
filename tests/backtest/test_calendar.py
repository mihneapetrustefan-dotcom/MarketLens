"""
tests/backtest/test_calendar.py
------------------------------------
The derived market calendar (spec §10, §71).

The load-bearing behaviour is `next_bar_after` being STRICTLY after.
An off-by-one there turns every backtest into a same-bar-execution
backtest, which is the single most effective way to make a losing
strategy look profitable — so it is tested from several directions.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.calendar import CALENDAR_VERSION, MarketCalendar
from tests.backtest.helpers import (
    END, START, add_bars, add_instrument, make_connection,
)


class CalendarTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        add_instrument(self.conn, "eq", "EQ", "technology")
        add_instrument(self.conn, "cx", "CX", "cryptocurrency", asset_class="crypto")
        add_bars(self.conn, "eq", days=60, start_price=100.0, weekdays_only=True)
        add_bars(self.conn, "cx", days=60, start_price=200.0, weekdays_only=False)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["eq", "cx"])

    def tearDown(self):
        self.conn.close()


class TestDerivation(CalendarTestCase):
    def test_equity_sessions_exclude_weekends(self):
        for bar in self.calendar.bars("eq"):
            self.assertLess(bar.timestamp.weekday(), 5)

    def test_crypto_sessions_include_weekends(self):
        weekdays = {b.timestamp.weekday() for b in self.calendar.bars("cx")}
        self.assertTrue(weekdays & {5, 6})

    def test_crypto_has_more_sessions_than_equity_over_the_same_span(self):
        self.assertGreater(len(self.calendar.bars("cx")),
                           len(self.calendar.bars("eq")))

    def test_unknown_instrument_has_no_data(self):
        self.calendar.load(["nowhere"])
        self.assertFalse(self.calendar.has_data("nowhere"))

    def test_missing_price_table_degrades_without_raising(self):
        conn = make_connection()
        conn.execute("DROP TABLE price_candle_cache")
        calendar = MarketCalendar(conn)
        calendar.load(["eq"])
        self.assertFalse(calendar.has_data("eq"))
        conn.close()

    def test_version_is_recorded(self):
        self.assertEqual(self.calendar.version, CALENDAR_VERSION)
        self.assertIn("limitations", self.calendar.describe())


class TestPointInTimeAccessors(CalendarTestCase):
    def test_next_bar_is_strictly_after_the_moment(self):
        """The inequality that separates a fill from look-ahead."""
        bars = self.calendar.bars("eq")
        target = bars[10]
        following = self.calendar.next_bar_after("eq", target.timestamp)
        self.assertIsNotNone(following)
        self.assertGreater(following.timestamp, target.timestamp)
        self.assertNotEqual(following.timestamp, target.timestamp)

    def test_next_bar_after_the_last_session_is_none(self):
        last = self.calendar.last_session("eq")
        self.assertIsNone(self.calendar.next_bar_after("eq", last))

    def test_bar_at_or_before_includes_the_moment_itself(self):
        bars = self.calendar.bars("eq")
        target = bars[5]
        found = self.calendar.bar_at_or_before("eq", target.timestamp)
        self.assertEqual(found.timestamp, target.timestamp)

    def test_bar_at_or_before_never_returns_a_later_bar(self):
        bars = self.calendar.bars("eq")
        moment = bars[5].timestamp + timedelta(hours=1)
        found = self.calendar.bar_at_or_before("eq", moment)
        self.assertLessEqual(found.timestamp, moment)

    def test_bar_before_all_history_is_none(self):
        first = self.calendar.first_session("eq")
        self.assertIsNone(
            self.calendar.bar_at_or_before("eq", first - timedelta(days=1)))

    def test_next_bars_after_respects_the_limit(self):
        bars = self.calendar.bars("eq")
        found = self.calendar.next_bars_after("eq", bars[0].timestamp, 3)
        self.assertEqual(len(found), 3)
        for bar in found:
            self.assertGreater(bar.timestamp, bars[0].timestamp)

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            self.calendar.next_bar_after("eq", datetime(2026, 6, 1))

    def test_bar_on_matches_only_an_exact_stamp(self):
        bars = self.calendar.bars("eq")
        self.assertIsNotNone(self.calendar.bar_on("eq", bars[3].timestamp))
        self.assertIsNone(
            self.calendar.bar_on("eq", bars[3].timestamp + timedelta(hours=3)))


class TestEvaluationClock(CalendarTestCase):
    def test_evaluation_dates_union_every_instrument(self):
        equity_only = self.calendar.evaluation_dates(["eq"], START, END)
        both = self.calendar.evaluation_dates(["eq", "cx"], START, END)
        self.assertGreater(len(both), len(equity_only))

    def test_evaluation_dates_are_bounded_by_the_period(self):
        bars = self.calendar.bars("eq")
        start = bars[10].timestamp
        end = bars[20].timestamp
        for moment in self.calendar.evaluation_dates(["eq"], start, end):
            self.assertGreaterEqual(moment, start)
            self.assertLessEqual(moment, end)

    def test_evaluation_dates_are_ascending_and_unique(self):
        dates = self.calendar.evaluation_dates(["eq", "cx"], START, END)
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), len(set(dates)))

    def test_one_evaluation_per_calendar_day(self):
        dates = self.calendar.evaluation_dates(["eq", "cx"], START, END)
        days = [d.date() for d in dates]
        self.assertEqual(len(days), len(set(days)))

    def test_empty_universe_yields_no_clock(self):
        self.assertEqual(self.calendar.evaluation_dates([], START, END), [])


class TestCoverage(CalendarTestCase):
    def test_coverage_counts_sessions(self):
        coverage = self.calendar.coverage(["eq", "cx"])
        self.assertGreater(coverage["eq"], 0)
        self.assertGreater(coverage["cx"], coverage["eq"])

    def test_instrument_without_bars_reports_zero(self):
        self.calendar.load(["nothing"])
        self.assertEqual(self.calendar.coverage(["nothing"])["nothing"], 0)

    def test_no_synthetic_bars_are_produced_for_gaps(self):
        """A missing session stays missing — nothing is interpolated."""
        bars = self.calendar.bars("eq")
        stamps = {b.timestamp.date() for b in bars}
        span = (bars[-1].timestamp.date() - bars[0].timestamp.date()).days + 1
        self.assertLess(len(stamps), span)


if __name__ == "__main__":
    unittest.main()
