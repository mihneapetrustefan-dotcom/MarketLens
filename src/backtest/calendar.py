"""
src/backtest/calendar.py
-----------------------------
Market sessions, derived from the data rather than declared
(Phase 12, spec §10, §71).

WHY THE CALENDAR IS DERIVED, NOT HARD-CODED
-----------------------------------------------
The obvious approach is a holiday table: US markets closed on these
dates, crypto open always. This project cannot honestly do that. It has
no holiday dataset, no exchange session metadata, and instruments
spanning US equities, Romanian equities and crypto — three different
session regimes, only one of which is widely tabulated.

What it does have is the record of when each instrument ACTUALLY
traded: 44,686 cached daily bars from Polygon. That record is a better
calendar than any table I could write, because it is the ground truth
the backtest will price against anyway. If a bar exists, the market was
open and a fill was possible; if it does not, no fill can be simulated
no matter what a holiday table claims.

The observed pattern confirms the approach: equities in this cache show
190 bars across Mon-Fri with zero weekend rows, while crypto shows 273
bars spread evenly across all seven weekdays.

WHAT THIS COSTS, STATED PLAINLY
-----------------------------------
A derived calendar cannot distinguish "the market was closed" from "we
failed to fetch that day". Both look like a missing bar. So a gap is
reported as an unfillable moment rather than diagnosed, and a run whose
instruments have sparse coverage carries a MISSING_PRICES warning. It
also cannot represent early closes or pre/after-hours sessions, because
daily bars carry no session boundaries — a limitation this module
records rather than papers over.

NO SYNTHETIC BARS, EVER
---------------------------
There is no interpolation and no forward-fill. A missing bar stays
missing, and the execution simulator refuses to fill against it. A
forward-filled price is a price nobody could have traded at, and
filling against one manufactures performance out of a data gap.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

CALENDAR_VERSION = "cal-derived-v1"

#: Only daily bars drive the backtest calendar. Minute bars exist for a
#: subset of instruments over ~30 days — too sparse and too uneven to
#: define sessions from without misrepresenting coverage.
DAILY_INTERVAL = "1d"


def _require_utc(moment: datetime, name: str = "moment") -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {moment.utcoffset()})")
    return moment


@dataclass(frozen=True)
class Bar:
    """One cached session. The only thing a fill may be priced against."""
    instrument_id: str
    timestamp: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]

    def price_for(self, field_name: str) -> Optional[float]:
        return getattr(self, field_name, None)


class MarketCalendar:
    """
    Per-instrument sessions and bar lookup, loaded once per run.

    Loading eagerly is deliberate: a backtest asks "what is the next bar
    after T for instrument X" thousands of times, and answering each
    from SQL would be the N+1 pattern spec §80 warns about. One query
    per run, held in memory, keeps the replay loop cheap.
    """

    version = CALENDAR_VERSION

    def __init__(self, conn: sqlite3.Connection, interval: str = DAILY_INTERVAL):
        self.conn = conn
        self.interval = interval
        self._bars: Dict[str, List[Bar]] = {}
        self._timestamps: Dict[str, List[datetime]] = {}
        self._loaded: set = set()

    # ---------------- loading ----------------

    def _table_available(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_candle_cache'"
        ).fetchone() is not None

    def load(self, instrument_ids: Sequence[str],
             start: Optional[datetime] = None,
             end: Optional[datetime] = None) -> None:
        """
        Load sessions for a set of instruments in ONE query.

        `end` bounds the load, not the visibility rule: point-in-time
        correctness is enforced by the accessors below, which never
        return a bar at or after the moment being asked about unless the
        caller explicitly asked for a later one.
        """
        wanted = sorted({i for i in instrument_ids if i} - self._loaded)
        if not wanted or not self._table_available():
            self._loaded.update(instrument_ids)
            return

        placeholders = ",".join("?" for _ in wanted)
        sql = f"""
            SELECT instrument_id, timestamp, open, high, low,
                   COALESCE(adjusted_close, close) AS close, volume
            FROM price_candle_cache
            WHERE interval = ? AND instrument_id IN ({placeholders})
        """
        params: List = [self.interval, *wanted]
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(_require_utc(start, "start").isoformat())
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(_require_utc(end, "end").isoformat())
        sql += " ORDER BY instrument_id, timestamp ASC"

        for instrument_id, ts, o, h, l, c, v in self.conn.execute(sql, params):
            self._bars.setdefault(instrument_id, []).append(Bar(
                instrument_id=instrument_id, timestamp=datetime.fromisoformat(ts),
                open=o, high=h, low=l, close=c, volume=v))

        for instrument_id in wanted:
            bars = self._bars.get(instrument_id, [])
            self._timestamps[instrument_id] = [b.timestamp for b in bars]
        self._loaded.update(wanted)

    # ---------------- sessions ----------------

    def has_data(self, instrument_id: str) -> bool:
        return bool(self._bars.get(instrument_id))

    def bars(self, instrument_id: str) -> List[Bar]:
        return self._bars.get(instrument_id, [])

    def first_session(self, instrument_id: str) -> Optional[datetime]:
        stamps = self._timestamps.get(instrument_id) or []
        return stamps[0] if stamps else None

    def last_session(self, instrument_id: str) -> Optional[datetime]:
        stamps = self._timestamps.get(instrument_id) or []
        return stamps[-1] if stamps else None

    def is_open(self, instrument_id: str, day: date) -> bool:
        """True when this instrument has a session on that calendar date."""
        for stamp in self._timestamps.get(instrument_id) or []:
            if stamp.date() == day:
                return True
        return False

    def sessions_between(self, instrument_id: str, start: datetime,
                         end: datetime) -> List[Bar]:
        stamps = self._timestamps.get(instrument_id) or []
        left = bisect_left(stamps, _require_utc(start, "start"))
        right = bisect_right(stamps, _require_utc(end, "end"))
        return self._bars.get(instrument_id, [])[left:right]

    # ---------------- point-in-time accessors ----------------

    def bar_at_or_before(self, instrument_id: str, moment: datetime) -> Optional[Bar]:
        """The most recent session at or before `moment`. The valuation accessor."""
        _require_utc(moment, "moment")
        stamps = self._timestamps.get(instrument_id) or []
        index = bisect_right(stamps, moment) - 1
        return self._bars[instrument_id][index] if index >= 0 else None

    def next_bar_after(self, instrument_id: str, moment: datetime) -> Optional[Bar]:
        """
        The first session STRICTLY after `moment`.

        Strictly after, not at-or-after: an order cut at the moment a
        bar is stamped must not fill against that same bar. That single
        inequality is the difference between a realistic fill and the
        most common look-ahead bug in backtesting.
        """
        _require_utc(moment, "moment")
        stamps = self._timestamps.get(instrument_id) or []
        index = bisect_right(stamps, moment)
        return self._bars[instrument_id][index] if index < len(stamps) else None

    def next_bars_after(self, instrument_id: str, moment: datetime,
                        limit: int) -> List[Bar]:
        """Up to `limit` sessions strictly after `moment`, in order."""
        _require_utc(moment, "moment")
        stamps = self._timestamps.get(instrument_id) or []
        index = bisect_right(stamps, moment)
        return self._bars.get(instrument_id, [])[index:index + max(0, limit)]

    def bar_on(self, instrument_id: str, moment: datetime) -> Optional[Bar]:
        """The session stamped exactly at `moment`, if one exists."""
        _require_utc(moment, "moment")
        stamps = self._timestamps.get(instrument_id) or []
        index = bisect_left(stamps, moment)
        if index < len(stamps) and stamps[index] == moment:
            return self._bars[instrument_id][index]
        return None

    # ---------------- the replay clock ----------------

    def evaluation_dates(self, instrument_ids: Sequence[str], start: datetime,
                         end: datetime) -> List[datetime]:
        """
        The chronological moments the replay loop will step through.

        Built from the UNION of every instrument's sessions, so a
        crypto-and-equity book steps on crypto's weekend sessions too —
        an equity order simply finds no bar there and waits, which is
        exactly the real behaviour.

        Deduplicated by calendar date to one moment per day: daily bars
        carry no intraday structure, so several instruments stamped at
        different hours of the same day are one evaluation point, not
        three.
        """
        _require_utc(start, "start")
        _require_utc(end, "end")
        seen: Dict[str, datetime] = {}
        for instrument_id in instrument_ids:
            for stamp in self._timestamps.get(instrument_id) or []:
                if stamp < start or stamp > end:
                    continue
                key = stamp.date().isoformat()
                # Latest stamp on the day, so an evaluation sees every
                # instrument's session for that date as already closed.
                if key not in seen or stamp > seen[key]:
                    seen[key] = stamp
        return [seen[k] for k in sorted(seen)]

    def coverage(self, instrument_ids: Sequence[str]) -> Dict[str, int]:
        """Session count per instrument — the input to a coverage warning."""
        return {i: len(self._timestamps.get(i) or []) for i in instrument_ids}

    def describe(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "interval": self.interval,
            "instruments_loaded": len(self._loaded),
            "instruments_with_data": sum(1 for i in self._loaded if self.has_data(i)),
            "derivation": "observed sessions in price_candle_cache",
            "limitations": [
                "cannot distinguish a market holiday from a fetch gap",
                "no early closes, pre-market or after-hours sessions",
                "daily bars only",
            ],
        }
