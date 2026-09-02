"""
tests/paper/test_clock_and_freshness.py
--------------------------------------------
The time source and the data-freshness monitor (spec §8, §9, §47, §48).

Freshness gets the most attention because it is what keeps this phase
honest. A paper system running on days-old bars is a legitimate thing;
one that PRESENTS days-old bars as live is not, and these tests are what
separate the two.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.calendar import MarketCalendar
from src.domain.paper_models import (
    DEFAULT_FRESHNESS_POLICIES, DataFreshness, FreshnessPolicy,
)
from src.paper.clock import (
    Clock, FixedClock, ReplayClock, SystemClock, clock_from_kind, require_utc,
)
from src.paper.freshness import FreshnessMonitor
from tests.paper.helpers import END, flat_universe, make_connection, standard_universe

T = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class TestSystemClock(unittest.TestCase):
    def test_returns_utc_aware_time(self):
        now = SystemClock().now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(0))

    def test_kind_is_recorded(self):
        self.assertEqual(SystemClock().kind, "system")


class TestFixedClock(unittest.TestCase):
    def test_stays_where_it_was_put(self):
        clock = FixedClock(T)
        self.assertEqual(clock.now(), T)
        self.assertEqual(clock.now(), T)

    def test_advances_forward(self):
        clock = FixedClock(T)
        clock.advance(timedelta(hours=2))
        self.assertEqual(clock.now(), T + timedelta(hours=2))

    def test_refuses_to_advance_backwards(self):
        """A decision loop that could see time reverse produces orderings no live system could reproduce."""
        clock = FixedClock(T)
        with self.assertRaises(ValueError):
            clock.advance(timedelta(hours=-1))

    def test_refuses_to_be_set_backwards(self):
        clock = FixedClock(T)
        with self.assertRaises(ValueError):
            clock.set(T - timedelta(minutes=1))

    def test_set_forward_is_allowed(self):
        clock = FixedClock(T)
        clock.set(T + timedelta(days=1))
        self.assertEqual(clock.now(), T + timedelta(days=1))

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            FixedClock(datetime(2026, 6, 1))


class TestReplayClock(unittest.TestCase):
    def setUp(self):
        self.moments = [T + timedelta(days=i) for i in range(5)]

    def test_starts_at_the_first_moment(self):
        self.assertEqual(ReplayClock(self.moments).now(), self.moments[0])

    def test_steps_through_the_sequence(self):
        clock = ReplayClock(self.moments)
        for expected in self.moments[1:]:
            self.assertEqual(clock.step(), expected)

    def test_exhaustion_pins_rather_than_raising(self):
        """A session that processed every tick is finished, not broken."""
        clock = ReplayClock(self.moments)
        while clock.step() is not None:
            pass
        self.assertTrue(clock.exhausted)
        self.assertEqual(clock.now(), self.moments[-1])
        self.assertIsNone(clock.step())

    def test_out_of_order_moments_are_rejected(self):
        with self.assertRaises(ValueError):
            ReplayClock([self.moments[2], self.moments[0]])

    def test_reset_returns_to_the_start(self):
        clock = ReplayClock(self.moments)
        clock.step()
        clock.reset()
        self.assertEqual(clock.now(), self.moments[0])

    def test_remaining_is_reported(self):
        clock = ReplayClock(self.moments)
        self.assertEqual(clock.remaining, 4)
        clock.step()
        self.assertEqual(clock.remaining, 3)


class TestClockRestoration(unittest.TestCase):
    """Recovery must restore the same KIND of clock, not merely a clock."""

    def test_system_clock_restores(self):
        self.assertIsInstance(clock_from_kind("system"), SystemClock)

    def test_fixed_clock_restores_to_its_moment(self):
        clock = clock_from_kind("fixed", moment=T)
        self.assertEqual(clock.now(), T)

    def test_replay_clock_restores_its_sequence(self):
        moments = [T, T + timedelta(days=1)]
        clock = clock_from_kind("replay", moments=moments)
        self.assertEqual(clock.now(), moments[0])

    def test_a_fixed_clock_without_a_moment_is_refused(self):
        with self.assertRaises(ValueError):
            clock_from_kind("fixed")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            clock_from_kind("wall-of-clocks")


class TestFreshnessPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = FreshnessPolicy(fresh_seconds=100, aging_seconds=1000,
                                      stale_seconds=10_000)

    def test_bands(self):
        self.assertEqual(self.policy.classify(50), DataFreshness.FRESH)
        self.assertEqual(self.policy.classify(500), DataFreshness.AGING)
        self.assertEqual(self.policy.classify(5_000), DataFreshness.STALE)
        self.assertEqual(self.policy.classify(50_000), DataFreshness.INVALID)

    def test_boundaries_are_inclusive_of_the_better_band(self):
        self.assertEqual(self.policy.classify(100), DataFreshness.FRESH)
        self.assertEqual(self.policy.classify(1000), DataFreshness.AGING)

    def test_missing_age_is_unavailable(self):
        self.assertEqual(self.policy.classify(None), DataFreshness.UNAVAILABLE)

    def test_future_dated_data_is_invalid_not_very_fresh(self):
        """A negative age is a clock or feed fault, not freshness."""
        self.assertEqual(self.policy.classify(-60), DataFreshness.INVALID)

    def test_only_fresh_and_aging_may_trade(self):
        self.assertTrue(DataFreshness.FRESH.is_tradeable)
        self.assertTrue(DataFreshness.AGING.is_tradeable)
        for blocked in (DataFreshness.STALE, DataFreshness.INVALID,
                        DataFreshness.UNAVAILABLE):
            self.assertFalse(blocked.is_tradeable, blocked)


class TestPerAssetClassPolicies(unittest.TestCase):
    """Spec §9 — one universal threshold cannot serve equities and crypto."""

    def test_crypto_is_stricter_than_equities(self):
        crypto = DEFAULT_FRESHNESS_POLICIES["crypto"]
        stock = DEFAULT_FRESHNESS_POLICIES["stock"]
        self.assertLess(crypto.fresh_seconds, stock.fresh_seconds)

    def test_the_same_age_classifies_differently_by_asset_class(self):
        age = 4 * 3600.0        # four hours
        self.assertEqual(DEFAULT_FRESHNESS_POLICIES["stock"].classify(age),
                         DataFreshness.FRESH)
        self.assertEqual(DEFAULT_FRESHNESS_POLICIES["crypto"].classify(age),
                         DataFreshness.AGING)


class TestFreshnessMonitor(unittest.TestCase):
    def setUp(self):
        self.conn = make_connection()
        flat_universe(self.conn)
        self.calendar = MarketCalendar(self.conn)
        self.calendar.load(["i-flat"])
        self.bars = self.calendar.bars("i-flat")
        self.monitor = FreshnessMonitor(
            self.calendar, asset_class_by_instrument={"i-flat": "stock"})

    def tearDown(self):
        self.conn.close()

    def test_a_recent_bar_is_fresh(self):
        report = self.monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        self.assertEqual(report.worst, DataFreshness.FRESH)

    def test_an_old_bar_is_stale(self):
        # Past the LAST bar, so the newest observation really is old.
        # Ten days after an early bar is not stale — later bars exist.
        far = self.bars[-1].timestamp + timedelta(days=10)
        report = self.monitor.evaluate(["i-flat"], far)
        self.assertIn(report.worst, (DataFreshness.STALE, DataFreshness.INVALID))

    def test_an_unknown_instrument_is_unavailable(self):
        report = self.monitor.evaluate(["i-nowhere"], self.bars[10].timestamp)
        self.assertEqual(report.worst, DataFreshness.UNAVAILABLE)

    def test_worst_is_the_aggregate_not_an_average(self):
        """One dead feed must not hide behind healthy ones."""
        report = self.monitor.evaluate(
            ["i-flat", "i-nowhere"], self.bars[10].timestamp)
        self.assertEqual(report.worst, DataFreshness.UNAVAILABLE)

    def test_age_is_measured(self):
        report = self.monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        status = report.status_for("i-flat")
        self.assertIsNotNone(status.age_seconds)
        self.assertGreaterEqual(status.age_seconds, 0)

    def test_prices_are_marked_cached_not_live(self):
        """Every price in this system is a stored bar, and says so."""
        report = self.monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        self.assertTrue(report.status_for("i-flat").is_cached)

    def test_tradeable_and_blocked_are_separated(self):
        report = self.monitor.evaluate(
            ["i-flat", "i-nowhere"], self.bars[10].timestamp)
        self.assertIn("i-flat", report.tradeable_instruments)
        self.assertIn("i-nowhere", report.blocked_instruments)

    def test_valuation_prices_include_stale_ones(self):
        """Blocking new orders is not the same as refusing to value a holding."""
        far = self.bars[-1].timestamp + timedelta(days=10)
        report = self.monitor.evaluate(["i-flat"], far)
        self.assertIsNotNone(report.prices().get("i-flat"))
        self.assertEqual(report.tradeable_prices(), {})

    def test_the_monitor_states_its_limitation(self):
        described = self.monitor.describe()
        self.assertIn("no streaming market feed", described["limitation"])

    def test_never_reads_a_bar_after_the_moment(self):
        report = self.monitor.evaluate(["i-flat"], self.bars[10].timestamp)
        status = report.status_for("i-flat")
        self.assertLessEqual(status.observed_at, self.bars[10].timestamp)


if __name__ == "__main__":
    unittest.main()
