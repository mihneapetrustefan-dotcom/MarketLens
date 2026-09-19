"""
src/trading/clock.py
-------------------------------------------
Time, and which kind of it a runner is allowed to use (Phase 25.8, §3, §8).

THE BUG THIS EXISTS TO MAKE IMPOSSIBLE
------------------------------------------
`scripts/run_trading_loop.py` advanced cycles like this:

    for index in range(args.cycles):
        moment = now + timedelta(seconds=index * args.cycle_seconds)

With the 900-second default, `--cycles 4` ran four cycles in about a
second, stamped now, +15m, +30m and +45m. Three of them were in the
FUTURE. The decisions were real, the signals were real, the database
rows were real -- and three quarters of them claimed to have happened
at moments that had not arrived.

Phase 25.5's anchor-drift guard did not catch it: that guard rejects
anchors more than four hours from the wall clock, and a 45-minute
forward drift sails through.

So time is no longer a number a caller passes in. It is a CLOCK, and
which clock you get is decided by the mode you are running in.

    WallClock      real time, really waits.      REAL/PAPER sessions.
    ReplayClock    controlled, never waits.      TESTS ONLY.

`RunMode.requires_wall_clock` enforces the separation, and
`SessionRunner` refuses to start with the wrong pairing. A replay
clock cannot reach a real session by configuration, by flag, or by
accident.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional


class RunMode(str, Enum):
    """
    What a runner is for, and therefore what time it may use.

    PAPER_SESSION and REAL_SESSION are separate names for the same
    clock discipline: both operate against a live venue in real time.
    They differ in intent and in what the operator has authorised, not
    in how they treat the clock.
    """
    REAL_SESSION = "real_session"
    PAPER_SESSION = "paper_session"
    TEST_REPLAY = "test_replay"

    @property
    def requires_wall_clock(self) -> bool:
        """A session against a live venue may only use real time."""
        return self in (RunMode.REAL_SESSION, RunMode.PAPER_SESSION)

    @property
    def may_simulate_time(self) -> bool:
        return self is RunMode.TEST_REPLAY


class Clock(ABC):
    """A source of now, and a way to wait for later."""

    #: Whether this clock reports real wall-clock time.
    is_wall_clock: bool = False

    @abstractmethod
    def now(self) -> datetime:
        """The current moment, timezone-aware UTC."""

    @abstractmethod
    def sleep_until(self, moment: datetime) -> float:
        """
        Wait until `moment`. Returns the seconds actually waited.

        Never waits for a moment already past: that returns
        immediately with 0.0, which is how a late cycle catches up
        rather than sleeping a full interval.
        """


class WallClock(Clock):
    """
    Real time. The only clock a live session may use.

    `max_sleep_seconds` bounds a single wait so a misconfigured
    boundary cannot park the runner for hours. It is a guard against
    arithmetic mistakes, not a scheduling feature.
    """

    is_wall_clock = True

    def __init__(self, max_sleep_seconds: float = 3600.0,
                 sleep_fn=time.sleep):
        self.max_sleep_seconds = float(max_sleep_seconds)
        self._sleep = sleep_fn

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep_until(self, moment: datetime) -> float:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware (UTC)")
        remaining = (moment - self.now()).total_seconds()
        if remaining <= 0:
            return 0.0
        remaining = min(remaining, self.max_sleep_seconds)
        self._sleep(remaining)
        return remaining


class ReplayClock(Clock):
    """
    A controlled clock for tests. NEVER reaches a live session.

    Advancing is explicit and recorded, so a test can assert the exact
    sequence of moments a runner visited -- which is how the
    end-to-end session test verifies that no cycle ran at a moment
    that had not arrived.
    """

    is_wall_clock = False

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware (UTC)")
        self._now = start.astimezone(timezone.utc)
        #: Every moment this clock was asked to wait until.
        self.waits: List[datetime] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment.astimezone(timezone.utc)
        return self._now

    def sleep_until(self, moment: datetime) -> float:
        """Jumps rather than waits, and records where it jumped to."""
        moment = moment.astimezone(timezone.utc)
        self.waits.append(moment)
        if moment <= self._now:
            return 0.0
        waited = (moment - self._now).total_seconds()
        self._now = moment
        return waited


class ClockModeViolation(RuntimeError):
    """A replay clock was offered to a live session, or the reverse."""


def clock_for(mode: RunMode, clock: Optional[Clock] = None) -> Clock:
    """
    The clock a mode is permitted to use.

    Raises rather than silently correcting. A runner configured with
    the wrong clock is a configuration error the operator must see,
    not something to quietly fix -- quietly fixing it is how a replay
    harness ends up driving a real account.
    """
    if mode.requires_wall_clock:
        if clock is not None and not clock.is_wall_clock:
            raise ClockModeViolation(
                f"{mode.value} requires real wall-clock time; a simulated "
                f"clock ({type(clock).__name__}) cannot drive a session "
                f"against a live venue")
        return clock or WallClock()
    if clock is None:
        raise ClockModeViolation(
            f"{mode.value} needs an explicit clock; refusing to invent one")
    return clock
