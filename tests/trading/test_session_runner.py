"""
tests/trading/test_session_runner.py
-----------------------------------------------------------
The Phase 25.8 session runner (§45, §46).

THE CASE THAT MATTERS MOST

`test_a_replay_clock_cannot_drive_a_live_session`. The defect this
phase exists to close is that the loop ran four cycles instantly while
stamping three of them in the future. Everything else here is
scheduling detail; that one is the safety property.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.market_data_schema import initialize_market_data_schema
from src.domain.paper_models import HealthState
from src.trading.clock import (
    Clock, ClockModeViolation, ReplayClock, RunMode, WallClock, clock_for,
)
from src.trading.schedule import (
    Cadence, DEFAULT_CADENCES, Schedule, align, next_boundary,
)
from src.trading.session_runner import (
    SessionRefused, SessionRunner, session_id_for,
)

OPEN = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)   # Monday


# ---------------- doubles ----------------

class _Entry:
    def __init__(self, instrument_id):
        self.instrument_id = instrument_id
        self.is_active = True


class _Session:
    """
    Session double.

    `is_open=False` closes it outright; `open_until` closes it at a
    moment on the CALLER's clock, which for replay tests is the replay
    clock rather than the wall clock.
    """

    def __init__(self, open_until=None, is_open=True):
        self.open_until = open_until
        self._is_open = is_open

    def any_open(self, entries, now):
        if not self._is_open:
            return False
        if self.open_until is None:
            return True
        return now < self.open_until


class _Cycle:
    def __init__(self):
        self.requested = 2
        self.tradeable = 2
        self.bars_written = 1


class _MarketData:
    """Stands in for MarketDataService."""

    def __init__(self, open_until=None, connected=True, fail=False,
                 is_open=True):
        self.session = _Session(open_until, is_open=is_open)
        self.connected = connected
        self.fail = fail
        self.cycles = 0

    def run_cycle(self, *args, **kwargs):
        self.cycles += 1
        if self.fail:
            raise RuntimeError("snapshot failed")
        return _Cycle()

    def health(self, now=None, limit=None):
        class _H:
            pass
        report = _H()
        report.connected = self.connected
        return report


class _LoopResult:
    cycle_id = "cyc-1"
    orders_submitted = 0


class _Loop:
    def __init__(self):
        self.calls = []

    def run_cycle(self, now=None, worker=""):
        self.calls.append(now)
        return _LoopResult()

    def deployable_models(self):
        return {}


def build_runner(clock, market_data=None, schedule=None, mode=None,
                 **kwargs):
    conn = sqlite3.connect(":memory:")
    # The features stage reads completed bars, so the operational
    # schema must exist or the stage legitimately fails.
    initialize_market_data_schema(conn)
    runner = SessionRunner(
        conn, _Loop(), market_data or _MarketData(),
        mode=mode or RunMode.PAPER_SESSION,
        clock=clock, schedule=schedule, **kwargs)
    runner._universe = lambda: [_Entry("us_and_intl-aapl")]
    runner._usable_prices = lambda now: {"us_and_intl-aapl": 100.0}
    return runner


# ---------------- clock ----------------

class TestClockModes(unittest.TestCase):

    def test_a_replay_clock_cannot_drive_a_live_session(self):
        """
        The safety property of this phase. A simulated clock reaching
        a real session is how decisions get stamped at moments that
        never happened.
        """
        for mode in (RunMode.REAL_SESSION, RunMode.PAPER_SESSION):
            with self.assertRaises(ClockModeViolation):
                clock_for(mode, ReplayClock(OPEN))

    def test_a_live_session_defaults_to_the_wall_clock(self):
        clock = clock_for(RunMode.PAPER_SESSION)
        self.assertTrue(clock.is_wall_clock)
        self.assertIsInstance(clock, WallClock)

    def test_replay_mode_refuses_to_invent_a_clock(self):
        with self.assertRaises(ClockModeViolation):
            clock_for(RunMode.TEST_REPLAY, None)

    def test_the_runner_refuses_the_wrong_clock_at_construction(self):
        with self.assertRaises(ClockModeViolation):
            build_runner(ReplayClock(OPEN), mode=RunMode.PAPER_SESSION)

    def test_a_wall_clock_never_waits_for_a_past_moment(self):
        """A late cycle catches up rather than sleeping an interval."""
        slept = []
        clock = WallClock(sleep_fn=slept.append)
        waited = clock.sleep_until(clock.now() - timedelta(minutes=5))
        self.assertEqual(waited, 0.0)
        self.assertEqual(slept, [])

    def test_a_single_wait_is_bounded(self):
        slept = []
        clock = WallClock(max_sleep_seconds=10.0, sleep_fn=slept.append)
        clock.sleep_until(clock.now() + timedelta(hours=3))
        self.assertEqual(slept, [10.0])

    def test_the_replay_clock_records_every_moment_it_jumped_to(self):
        clock = ReplayClock(OPEN)
        clock.sleep_until(OPEN + timedelta(minutes=5))
        self.assertEqual(clock.now(), OPEN + timedelta(minutes=5))
        self.assertEqual(clock.waits, [OPEN + timedelta(minutes=5)])


# ---------------- scheduler ----------------

class TestScheduleBoundaries(unittest.TestCase):

    def test_alignment_is_stable_across_restarts(self):
        """
        Aligned to the UTC day, so a runner restarting at 10:17 lands
        on the same grid as one that started at 09:30.
        """
        a = align(datetime(2026, 9, 14, 10, 17, 43, tzinfo=timezone.utc), 300)
        self.assertEqual(a, datetime(2026, 9, 14, 10, 15, tzinfo=timezone.utc))

    def test_the_next_boundary_is_strictly_in_the_future(self):
        exact = datetime(2026, 9, 14, 10, 15, tzinfo=timezone.utc)
        self.assertEqual(next_boundary(exact, 300),
                         datetime(2026, 9, 14, 10, 20, tzinfo=timezone.utc))

    def test_work_duration_is_absorbed_by_the_wait_not_added_to_it(self):
        """
        boundary 10:15:00, work ends 10:15:37, next 10:20:00 -- not
        10:20:37. Sleeping a fixed interval after the work drifts the
        loop off its own grid.
        """
        schedule = Schedule.default(tick_seconds=300.0)
        started = datetime(2026, 9, 14, 10, 15, tzinfo=timezone.utc)
        finished = started + timedelta(seconds=37)
        self.assertEqual(schedule.next_tick_after_work(started, finished),
                         datetime(2026, 9, 14, 10, 20, tzinfo=timezone.utc))
        self.assertEqual(schedule.missed_boundaries, 0)

    def test_an_overrun_skips_to_the_future_and_is_counted(self):
        """Never a backlog: the loop acts on now, not on moments gone."""
        schedule = Schedule.default(tick_seconds=60.0)
        started = datetime(2026, 9, 14, 10, 15, tzinfo=timezone.utc)
        finished = started + timedelta(seconds=190)
        following = schedule.next_tick_after_work(started, finished)
        self.assertGreater(following, finished)
        self.assertEqual(following,
                         datetime(2026, 9, 14, 10, 19, tzinfo=timezone.utc))
        self.assertGreater(schedule.missed_boundaries, 0)

    def test_a_cadence_is_due_once_per_grid_boundary(self):
        cadence = Cadence("signals", 300.0)
        self.assertTrue(cadence.is_due(OPEN))
        cadence.mark(OPEN)
        self.assertFalse(cadence.is_due(OPEN + timedelta(seconds=60)))
        self.assertTrue(cadence.is_due(OPEN + timedelta(seconds=301)))

    def test_the_default_cadences_are_ordered_sensibly(self):
        self.assertLessEqual(DEFAULT_CADENCES["market_data"],
                             DEFAULT_CADENCES["signals"])
        self.assertLessEqual(DEFAULT_CADENCES["signals"],
                             DEFAULT_CADENCES["reconciliation"])


# ---------------- session ----------------

class TestSessionLifecycle(unittest.TestCase):

    def test_a_closed_market_refuses_to_start(self):
        runner = build_runner(
            WallClock(sleep_fn=lambda _s: None),
            market_data=_MarketData(is_open=False))
        with self.assertRaises(SessionRefused) as refusal:
            runner.start()
        self.assertIn("nothing to run", str(refusal.exception))

    def test_a_disconnected_broker_refuses_to_start(self):
        """
        A connected gateway is not sufficient, and a disconnected one
        is disqualifying. The message names the human action.
        """
        runner = build_runner(WallClock(sleep_fn=lambda _s: None),
                              market_data=_MarketData(connected=False))
        with self.assertRaises(SessionRefused) as refusal:
            runner.start()
        self.assertIn("browser", str(refusal.exception))

    def test_the_session_id_is_stable_so_a_restart_rejoins(self):
        first = session_id_for(RunMode.PAPER_SESSION, OPEN, "DU1")
        later = session_id_for(RunMode.PAPER_SESSION,
                               OPEN + timedelta(hours=3), "DU1")
        self.assertEqual(first, later)

    def test_a_different_account_gets_a_different_session(self):
        self.assertNotEqual(
            session_id_for(RunMode.PAPER_SESSION, OPEN, "DU1"),
            session_id_for(RunMode.PAPER_SESSION, OPEN, "DU2"))

    def test_the_fingerprint_records_the_operating_configuration(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        fingerprint = runner.fingerprint()
        for key in ("mode", "tick_seconds", "cadences", "price_freshness",
                    "orders_enabled", "clock", "loop_method_version"):
            self.assertIn(key, fingerprint)
        self.assertEqual(fingerprint["clock"], "WallClock")

    def test_closing_twice_is_harmless(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        runner.start()
        first = runner.close()
        self.assertIsNotNone(first.closed_at)
        self.assertEqual(runner.close().closed_at, first.closed_at)


class TestTickBehaviour(unittest.TestCase):

    def test_orders_are_not_enabled_merely_because_the_runner_runs(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        self.assertFalse(runner.orders_enabled)

    def test_market_data_failure_blocks_trading_but_not_the_session(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None),
                              market_data=_MarketData(fail=True))
        tick = runner.run_tick(OPEN)
        self.assertIn("market_data", tick.failures)
        self.assertIs(tick.health, HealthState.FAILED)

    def test_no_fresh_price_degrades_rather_than_ending_the_session(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        runner._usable_prices = lambda now: {}
        tick = runner.run_tick(OPEN)
        self.assertIs(tick.health, HealthState.DEGRADED)
        self.assertTrue(any("fresh operational price" in b
                            for b in tick.blocks))

    def test_a_long_blind_stretch_becomes_failed_not_endlessly_degraded(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        runner._usable_prices = lambda now: {}
        runner.run_tick(OPEN)
        later = runner.run_tick(OPEN + timedelta(minutes=30))
        self.assertIs(later.health, HealthState.FAILED)

    def test_no_deployable_model_is_reported_not_crashed(self):
        """§19: no signal is not loop failure."""
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        tick = runner.run_tick(OPEN)
        signals = tick.stage("signals")
        self.assertTrue(signals.ok)
        self.assertIn("no deployable model", signals.detail)

    def test_the_loop_cycle_receives_the_runner_wall_clock_moment(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        runner.run_tick(OPEN)
        self.assertEqual(runner.loop.calls, [OPEN])

    def test_describe_exposes_state_for_an_observer(self):
        runner = build_runner(WallClock(sleep_fn=lambda _s: None))
        runner.start()
        described = runner.describe(OPEN)
        self.assertTrue(described["clock"]["wall_clock"])
        self.assertIn("cadences", described["schedule"])
        self.assertFalse(described["orders_enabled"])


class TestEndToEndSimulatedSession(unittest.TestCase):
    """
    §46: one deterministic session, no broker required.

    Runs on a ReplayClock in TEST_REPLAY mode -- the only mode that
    may simulate time -- and asserts the properties the production
    runner must hold.
    """

    def _run(self, ticks=8, tick_seconds=300.0):
        clock = ReplayClock(OPEN)
        close = OPEN + timedelta(hours=6, minutes=30)   # 20:00 UTC
        schedule = Schedule.default(tick_seconds=tick_seconds)
        runner = build_runner(
            clock, market_data=_MarketData(open_until=close),
            schedule=schedule, mode=RunMode.TEST_REPLAY, max_ticks=ticks)
        state = runner.run_until_close()
        return runner, state, clock

    def test_the_session_runs_and_closes_cleanly(self):
        _runner, state, _clock = self._run()
        self.assertEqual(state.ticks, 8)
        self.assertIsNotNone(state.closed_at)
        self.assertFalse(state.is_open)

    def test_no_tick_ever_runs_at_a_moment_that_has_not_arrived(self):
        """
        The regression this phase exists for. Every moment handed to
        the loop must be at or before the clock, never ahead of it.
        """
        runner, _state, clock = self._run()
        for moment in runner.loop.calls:
            self.assertLessEqual(moment, clock.now())

    def test_moments_advance_monotonically(self):
        runner, _state, _clock = self._run()
        calls = runner.loop.calls
        self.assertEqual(calls, sorted(calls))

    def test_the_runner_waits_between_ticks_rather_than_bursting(self):
        _runner, _state, clock = self._run()
        self.assertGreater(len(clock.waits), 1)
        self.assertEqual(clock.waits, sorted(clock.waits))

    def test_ticks_land_on_the_grid(self):
        _runner, _state, clock = self._run(tick_seconds=300.0)
        for moment in clock.waits:
            self.assertEqual(moment.second, 0)
            self.assertEqual(moment.minute % 5, 0)

    def test_the_session_stops_when_the_market_closes(self):
        clock = ReplayClock(OPEN)
        close = OPEN + timedelta(minutes=20)
        runner = build_runner(
            clock, market_data=_MarketData(open_until=close),
            schedule=Schedule.default(tick_seconds=300.0),
            mode=RunMode.TEST_REPLAY, max_ticks=100)
        state = runner.run_until_close()
        self.assertLess(state.ticks, 100)
        self.assertIsNotNone(state.closed_at)

    def test_no_orders_are_submitted_without_permission(self):
        _runner, state, _clock = self._run()
        self.assertEqual(state.orders_submitted, 0)

    def test_stopping_ends_the_session_after_the_current_tick(self):
        clock = ReplayClock(OPEN)
        runner = build_runner(
            clock, market_data=_MarketData(
                open_until=OPEN + timedelta(hours=6)),
            schedule=Schedule.default(tick_seconds=300.0),
            mode=RunMode.TEST_REPLAY, max_ticks=50)
        runner.start()
        runner.stop()
        state = runner.run_until_close()
        self.assertEqual(state.ticks, 0)
        self.assertIsNotNone(state.closed_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
