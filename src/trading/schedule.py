"""
src/trading/schedule.py
-------------------------------------------
Cadences and boundaries for the session runner (Phase 25.8, §9, §33, §36).

WHY A SCHEDULER RATHER THAN A SLEEP
---------------------------------------
`sleep(900)` after each cycle drifts. If the work takes 37 seconds,
the next cycle starts at 15:37, then 31:14, then 46:51 -- the loop
walks away from its own grid and its cycle anchors stop lining up with
the 15-minute boundaries the idempotency keys are derived from.

So the runner computes the NEXT BOUNDARY on a fixed grid and waits
until it. Work duration is absorbed by the wait, not added to it.

    boundary  10:15:00
    work ends 10:15:37
    next      10:20:00       (waits 4m23s, not 5m)

WHEN WORK OVERRUNS
----------------------
If a cycle takes longer than its interval, the boundary it should have
run at is already past. The scheduler SKIPS to the next future
boundary rather than running a backlog: the loop's purpose is to act
on current market state, and replaying four stale boundaries in a row
would submit decisions about moments that have gone. Skips are counted
and reported, never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

#: Defaults derived from Phase 25.7's measured behaviour and the
#: strategies that actually exist, not from habit.
#:
#:   market data   60s  -- one batched IBKR request, 1/50 of budget
#:   bars          60s  -- a minute cannot complete faster than a minute
#:   features     300s  -- shortest strategy horizon is intraday 5m
#:   signals      300s  -- evaluated against completed 5m of bars
#:   portfolio    300s  -- revalue on the same grid as signals
#:   risk         300s  -- periodic; ALSO runs before every order
#:   reconcile    900s  -- broker truth, expensive, rarely changes
DEFAULT_CADENCES: Dict[str, float] = {
    "market_data": 60.0,
    "bars": 60.0,
    "features": 300.0,
    "signals": 300.0,
    "portfolio": 300.0,
    "risk": 300.0,
    "reconciliation": 900.0,
}


def align(moment: datetime, seconds: float) -> datetime:
    """
    The most recent grid boundary at or before `moment`.

    Aligned to the UTC day so boundaries are stable across restarts:
    a runner that restarts at 10:17 lands on the same grid the one
    that started at 09:30 was using.
    """
    moment = moment.astimezone(timezone.utc)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (moment - day).total_seconds()
    return day + timedelta(seconds=(int(elapsed // seconds) * seconds))


def next_boundary(moment: datetime, seconds: float) -> datetime:
    """The first grid boundary strictly after `moment`."""
    return align(moment, seconds) + timedelta(seconds=seconds)


@dataclass
class Cadence:
    """One named rhythm, and when it last actually ran."""
    name: str
    interval_seconds: float
    last_run: Optional[datetime] = None
    runs: int = 0
    skips: int = 0

    def is_due(self, now: datetime) -> bool:
        """
        Due when the grid boundary for `now` is newer than the last
        run. Comparing boundaries rather than raw elapsed time keeps a
        cadence on its grid even when a cycle arrives slightly early
        or late.
        """
        if self.last_run is None:
            return True
        return align(now, self.interval_seconds) > align(
            self.last_run, self.interval_seconds)

    def mark(self, now: datetime) -> None:
        self.last_run = now.astimezone(timezone.utc)
        self.runs += 1

    def age_seconds(self, now: datetime) -> Optional[float]:
        if self.last_run is None:
            return None
        return (now.astimezone(timezone.utc) - self.last_run).total_seconds()


@dataclass
class Schedule:
    """
    The set of cadences a session runs on.

    `tick_seconds` is the runner's heartbeat -- the finest grid it
    wakes on. Every cadence is a multiple of it in practice; nothing
    enforces that, because a cadence that is not a multiple simply
    fires on the first tick at or after its own boundary, which is
    still correct and merely coarser than asked.
    """
    tick_seconds: float = 60.0
    cadences: Dict[str, Cadence] = field(default_factory=dict)
    #: Boundaries the runner never reached because work overran.
    missed_boundaries: int = 0

    @classmethod
    def default(cls, overrides: Optional[Dict[str, float]] = None,
                tick_seconds: float = 60.0) -> "Schedule":
        intervals = dict(DEFAULT_CADENCES)
        intervals.update(overrides or {})
        return cls(
            tick_seconds=tick_seconds,
            cadences={name: Cadence(name, seconds)
                      for name, seconds in intervals.items()})

    def due(self, now: datetime) -> List[str]:
        """Which cadences are due at `now`, in a stable order."""
        return [name for name in sorted(self.cadences)
                if self.cadences[name].is_due(now)]

    def mark(self, name: str, now: datetime) -> None:
        cadence = self.cadences.get(name)
        if cadence is not None:
            cadence.mark(now)

    def next_tick(self, now: datetime) -> datetime:
        """The next heartbeat boundary after `now`."""
        return next_boundary(now, self.tick_seconds)

    def next_tick_after_work(self, started: datetime,
                             finished: datetime) -> datetime:
        """
        Where to wake up, given a cycle that began at `started` and
        ended at `finished`.

        Counts every boundary the work ran past, so an overrun is
        visible rather than absorbed. Returns the first boundary in
        the future -- never a backlog.
        """
        intended = next_boundary(started, self.tick_seconds)
        if finished >= intended:
            overran = int(
                (finished - intended).total_seconds() // self.tick_seconds) + 1
            self.missed_boundaries += overran
        return next_boundary(finished, self.tick_seconds)

    def summary(self, now: datetime) -> Dict[str, object]:
        return {
            "tick_seconds": self.tick_seconds,
            "missed_boundaries": self.missed_boundaries,
            "cadences": {
                name: {
                    "interval": cadence.interval_seconds,
                    "runs": cadence.runs,
                    "age_seconds": cadence.age_seconds(now),
                    "due": cadence.is_due(now),
                }
                for name, cadence in sorted(self.cadences.items())
            },
        }
