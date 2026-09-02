"""
src/paper/clock.py
-----------------------
A controllable time source (Phase 13, spec §47, §48).

WHY NOT JUST CALL datetime.now()
------------------------------------
Because three different things get called "now" in a trading system,
and conflating them is how backtest and live behaviour silently
diverge:

    EVENT TIME     when the market produced the observation
    RECEIPT TIME   when this system received it
    DECISION TIME  when the pipeline acted on it

Only the last is "now" in the wall-clock sense. Phase 12 anchored
everything on event time because it replayed history. Phase 13 has to
hold all three at once, and the difference between them IS the latency
and freshness this phase exists to measure.

Hardcoding `datetime.now(timezone.utc)` into each component would also
make the session untestable without waiting in real time, and would
make a recorded session unreplayable — spec §51 requires both.

THREE IMPLEMENTATIONS, DELIBERATELY SMALL
---------------------------------------------
`SystemClock` for live operation, `FixedClock` for tests, `ReplayClock`
for stepping through recorded or historical moments. Spec §48 warns
against building a large simulation framework here, and there is no
fourth case this system needs.

The clock is passed in, never constructed inside a component. That is
what lets one session run against wall time while a test runs the same
code deterministically over a fixed sequence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence


def require_utc(value: datetime, name: str = "moment") -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


class Clock(ABC):
    """The time source a session reads. Never `datetime.now()` directly."""

    #: Recorded on sessions so a replay knows how time was being read.
    kind: str = "abstract"

    @abstractmethod
    def now(self) -> datetime:
        """The current decision time, always UTC-aware."""

    def describe(self) -> dict:
        return {"kind": self.kind, "now": self.now().isoformat()}


class SystemClock(Clock):
    """Wall-clock time. What a live session uses."""

    kind = "system"

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """
    A clock pinned to one moment, movable only by explicit calls.

    Used by tests and by any caller that needs the pipeline to behave
    identically on every run. `advance` moves it forward; it refuses to
    move backwards, because a decision loop that could see time reverse
    would produce orderings no live system could reproduce.
    """

    kind = "fixed"

    def __init__(self, moment: datetime):
        self._now = require_utc(moment, "moment")

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("a clock may not move backwards")
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        moment = require_utc(moment, "moment")
        if moment < self._now:
            raise ValueError(
                f"cannot set the clock backwards from {self._now.isoformat()} "
                f"to {moment.isoformat()}")
        self._now = moment
        return self._now


class ReplayClock(Clock):
    """
    Steps through a prepared sequence of moments.

    This is what makes a recorded session replayable (spec §51) and a
    historical period walkable at any speed (spec §48). The sequence is
    supplied by the caller — usually the market calendar's sessions —
    so the clock never invents a moment the data does not have.

    Exhausting the sequence pins the clock at its last moment rather
    than raising: a session that has processed every available tick is
    finished, not broken.
    """

    kind = "replay"

    def __init__(self, moments: Sequence[datetime]):
        ordered = [require_utc(m, "moment") for m in moments]
        if ordered != sorted(ordered):
            raise ValueError("replay moments must be in ascending order")
        self._moments: List[datetime] = ordered
        self._index = 0

    def now(self) -> datetime:
        if not self._moments:
            raise ValueError("ReplayClock was given no moments")
        index = min(self._index, len(self._moments) - 1)
        return self._moments[index]

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._moments) - 1

    @property
    def remaining(self) -> int:
        return max(0, len(self._moments) - 1 - self._index)

    def step(self) -> Optional[datetime]:
        """Advance one moment, or return None when the sequence is spent."""
        if self._index >= len(self._moments) - 1:
            return None
        self._index += 1
        return self._moments[self._index]

    def reset(self) -> None:
        self._index = 0

    def describe(self) -> dict:
        described = super().describe()
        described.update({"moments": len(self._moments),
                          "index": self._index,
                          "remaining": self.remaining})
        return described


def clock_from_kind(kind: str, moment: Optional[datetime] = None,
                    moments: Optional[Sequence[datetime]] = None) -> Clock:
    """
    Rebuild a clock from a stored session's recorded kind.

    Used on recovery: a session that was running on wall time resumes on
    wall time, and one that was replaying resumes replaying. Restoring
    the wrong kind would silently change what "now" means mid-session.
    """
    if kind == "system":
        return SystemClock()
    if kind == "fixed":
        if moment is None:
            raise ValueError("a fixed clock needs a moment to restore to")
        return FixedClock(moment)
    if kind == "replay":
        if not moments:
            raise ValueError("a replay clock needs its moment sequence to restore")
        return ReplayClock(moments)
    raise ValueError(f"unknown clock kind {kind!r}")
