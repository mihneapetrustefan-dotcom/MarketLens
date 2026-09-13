"""
src/impact/anchoring.py
-------------------------------------------
Which prices a post-event window is measured between (Phase 25.9B).

TWO METHODS, BOTH REPRODUCIBLE
----------------------------------
`anchor-v1` is what `ImpactEngine._compute_returns` does today, and
what every row in `event_study_returns` was built with. It is
reproduced here exactly so its behaviour can be tested and so v1
results remain rebuildable after v2 exists. It is NOT removed.

`anchor-v2` corrects two demonstrable defects in it.

THE ROOT CAUSE
------------------
`scripts/build_event_studies.load_candles` merges daily and minute
candles into ONE list, and `Candle` has no resolution field -- the
`interval` column is read and discarded. So the engine cannot tell a
daily candle from a minute candle, and every lookup is "latest
timestamp at or before", whichever resolution that happens to be.

Daily candles are timestamped at the session DATE (04:00 UTC), not at
the close. So on the day before an event, a pre-market minute print
sorts AFTER the daily candle and wins.

DEFECT 1 -- a stale, mixed-resolution base.
    Traced on study es-86b69ecaf738ef05 (us_and_intl-ge):
      anchor                     Sat 2026-08-01 10:02 UTC (market closed)
      v1 price_before, ALL windows  355.94
        = Fri 08:03 UTC pre-market MINUTE print
      Friday's actual session close  360.07
    A daily window was measured from a 4 a.m. ET minute trade to a
    session close twenty days later.

DEFECT 2 -- intraday windows snap across closures.
    `_candle_at_or_after(end)` has no upper bound. With no candle near
    a Saturday anchor, both intraday_5m and intraday_60m jumped to
    Monday's first candle: price_after 368.93 for BOTH. A "5-minute
    return" spanned three days, and distinct windows collapsed onto one
    price.

WHAT v2 CHANGES, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------------
MINUTES windows: `before` must be a MINUTE price within a tolerance of
the window start, and `after` a MINUTE price within a tolerance of the
window end, both on the anchor's UTC date. Otherwise the window is
MISSING with a named reason. Never snapped across a closure.

TRADING_DAYS windows: `before` is the last DAILY close whose session
date is strictly before the anchor's date -- a close that had
necessarily happened by the time of the event, so point-in-time safe
without inventing close times. `after` is the DAILY close of the
session `window_bounds` already selects. Resolution is consistent at
both ends.

NOT CHANGED: the window END. `window_bounds` walks the real session
list, not calendar days, and that is the project's definition of "N
trading days". Nor the abnormal-return formula (raw minus the
market-adjusted benchmark return, beta 1). v2 is an ANCHOR fix, not a
target redefinition.

OPEN, AND LEFT OPEN: whether a daily window should start from the
prior close or from the first session after the event. That changes
what "d20" measures, which is a definition decision, not a bug fix. v2
keeps v1's intent -- the last pre-event price -- and only makes it the
right resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from src.domain.impact_models import EventWindow, WindowKind, WindowUnit

ANCHOR_METHOD_V1 = "anchor-v1"
ANCHOR_METHOD_V2 = "anchor-v2"

#: How far a minute price may sit from the moment it is meant to
#: represent. Five minutes: the shortest intraday window is five
#: minutes, so a price further away than that cannot honestly stand
#: for the window's edge.
DEFAULT_INTRADAY_TOLERANCE = timedelta(minutes=5)


@dataclass
class PricePoint:
    timestamp: datetime
    price: float


@dataclass
class WindowResolution:
    """The two prices a window is measured between, or why it has none."""
    method: str
    window_name: str
    before: Optional[PricePoint] = None
    after: Optional[PricePoint] = None
    #: Empty when resolved. Named when not -- a missing label must say
    #: why, never be silently dropped or filled with zero.
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return (not self.reason and self.before is not None
                and self.after is not None)


def _price(candle) -> Optional[float]:
    return getattr(candle, "price", None)


def _latest_at_or_before(candles, moment: datetime):
    eligible = [c for c in candles if c.timestamp <= moment and _price(c) is not None]
    return max(eligible, key=lambda c: c.timestamp) if eligible else None


def _earliest_at_or_after(candles, moment: datetime):
    eligible = [c for c in candles if c.timestamp >= moment and _price(c) is not None]
    return min(eligible, key=lambda c: c.timestamp) if eligible else None


def _point(candle) -> Optional[PricePoint]:
    return PricePoint(candle.timestamp, float(_price(candle))) if candle else None


def _trading_day_end(anchor: datetime, window: EventWindow,
                     session_timestamps: Sequence[datetime]) -> Optional[datetime]:
    """The END session, exactly as ImpactEngine.window_bounds selects it."""
    sessions = sorted(set(session_timestamps or []))
    future = [s for s in sessions if s >= anchor]
    index = int(window.end_offset)
    return future[index] if 0 <= index < len(future) else None


# ---------------- v1: reproduced, unchanged ----------------

def resolve_v1(anchor: datetime, window: EventWindow, merged_candles,
               session_timestamps: Sequence[datetime]) -> WindowResolution:
    """
    Exactly what ImpactEngine._compute_returns does for a POST_EVENT
    window. Kept so v1 remains reproducible and its defects testable.
    """
    result = WindowResolution(method=ANCHOR_METHOD_V1, window_name=window.name)
    base = _latest_at_or_before(merged_candles, anchor)
    if base is None:
        result.reason = "no pre-event price available"
        return result

    if window.unit == WindowUnit.MINUTES:
        end = anchor + timedelta(minutes=window.end_offset)
    else:
        end = _trading_day_end(anchor, window, session_timestamps)
        if end is None:
            result.reason = "window end beyond available sessions"
            return result

    after = _earliest_at_or_after(merged_candles, end)
    result.before = _point(base)
    result.after = _point(after)
    if after is None:
        result.reason = f"no price for window '{window.name}'"
    return result


# ---------------- v2: corrected anchor ----------------

def resolve_v2(anchor: datetime, window: EventWindow,
               minute_candles, daily_candles,
               session_timestamps: Sequence[datetime],
               tolerance: timedelta = DEFAULT_INTRADAY_TOLERANCE
               ) -> WindowResolution:
    """
    Resolution-aware: takes minute and daily candles SEPARATELY, so the
    v1 confusion between them cannot occur.
    """
    result = WindowResolution(method=ANCHOR_METHOD_V2, window_name=window.name)

    if window.kind != WindowKind.POST_EVENT:
        result.reason = "anchor-v2 resolves post-event windows only"
        return result

    if window.unit == WindowUnit.MINUTES:
        start = anchor + timedelta(minutes=window.start_offset)
        end = anchor + timedelta(minutes=window.end_offset)

        before = _latest_at_or_before(minute_candles, start)
        if before is None or start - before.timestamp > tolerance:
            result.reason = (f"no minute price within {int(tolerance.total_seconds() // 60)} "
                             f"minutes before the window start; the event was not in an "
                             f"observable session")
            return result
        if before.timestamp.date() != anchor.date():
            result.reason = "nearest minute price before the event is on a different day"
            return result

        after = _earliest_at_or_after(minute_candles, end)
        if after is None or after.timestamp - end > tolerance:
            result.reason = (f"no minute price within {int(tolerance.total_seconds() // 60)} "
                             f"minutes after the window end; refusing to snap across a closure")
            return result
        if after.timestamp.date() != before.timestamp.date():
            result.reason = "window end would cross a session boundary"
            return result

        result.before, result.after = _point(before), _point(after)
        return result

    # TRADING_DAYS
    prior = [c for c in daily_candles
             if _price(c) is not None and c.timestamp.date() < anchor.date()]
    if not prior:
        result.reason = "no daily close before the event date"
        return result
    base = max(prior, key=lambda c: c.timestamp)

    end = _trading_day_end(anchor, window, session_timestamps)
    if end is None:
        result.reason = "window end beyond available sessions (label not yet resolvable)"
        return result
    at_end = [c for c in daily_candles
              if _price(c) is not None and c.timestamp.date() == end.date()]
    if not at_end:
        result.reason = "no daily close for the window's end session"
        return result

    result.before = _point(base)
    result.after = _point(at_end[0])
    return result


def raw_and_abnormal(resolution: WindowResolution,
                     benchmark: Optional[WindowResolution]
                     ) -> Tuple[Optional[float], Optional[float], str]:
    """
    Raw and abnormal simple return, using the project's existing
    market-adjusted definition: abnormal = raw - benchmark_raw (beta 1).
    Unchanged from v1; only the prices it is fed differ.
    """
    from src.impact.calculations import (
        compute_abnormal_return, compute_return, expected_return_market_adjusted,
    )
    if not resolution.resolved:
        return None, None, resolution.reason
    raw = compute_return(resolution.before.price, resolution.after.price)
    if benchmark is None or not benchmark.resolved:
        return raw, None, "benchmark prices unavailable for this window"
    bench = compute_return(benchmark.before.price, benchmark.after.price)
    return raw, compute_abnormal_return(raw, expected_return_market_adjusted(bench)), ""
