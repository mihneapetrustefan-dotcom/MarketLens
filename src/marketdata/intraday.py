"""
src/marketdata/intraday.py
-------------------------------------------
The research view of one-minute bars (Phase 25.9F).

WHY THIS EXISTS
-------------------
Phase 25.7 built `market_data_bars`: operational telemetry from the
IBKR poller, with a **thirty-day retention ceiling** and a `prune()`
that deletes. That is the right policy for telemetry and the wrong one
for research -- a dataset built on a table that silently drops its own
history cannot be reproduced next month.

Meanwhile the research cache (`price_candle_cache`, interval `1m`)
already holds durable, vendor-sourced one-minute candles fetched around
canonical events. That is the corpus research should read, and it is
already append-only and reproducible.

So this module does NOT create a third copy of the same minutes. It:

  1. reads one-minute bars from the RESEARCH cache, as research data;
  2. offers `archive_operational_bars()` as the bridge that rescues
     operational bars into the research cache BEFORE retention deletes
     them, so live-captured minutes become durable research data by an
     explicit, audited transformation rather than by accident.

Operational state stays operational (Phase 25.7's rule, unchanged):
nothing here writes `market_data_state`, and nothing here is consulted
to price an order.

WHAT A RESEARCH BAR CARRIES THAT A ROW DOES NOT
---------------------------------------------------
Quality. A minute with one observation behind it is a single price
wearing OHLC clothing; a minute whose predecessor is missing cannot
support a one-minute return at all. Both are still returned -- with
flags -- because dropping them silently is how a gap becomes an
invented number downstream.

THE CLOSED-BAR RULE
-----------------------
`IntradayBar.timestamp` is `bar_end`, deliberately. The point-in-time
lens filters on whatever timestamp it is handed, so exposing
`bar_start` would make the minute currently in progress visible to a
decision taken inside it -- the exact look-ahead §10 forbids. A bar is
knowable only once it has ended.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.marketdata.calendar import NEW_YORK, USEquityCalendar

MINUTE = timedelta(minutes=1)

#: The research corpus of one-minute candles.
RESEARCH_INTERVAL = "1m"

#: A bar built from fewer observations than this is flagged. The poller
#: samples roughly once a minute, so one observation is normal for the
#: operational source and says nothing is wrong -- it is the vendor
#: source, which aggregates real trades, where a thin minute is a
#: liquidity fact worth carrying.
LOW_OBSERVATION_THRESHOLD = 1


class BarQuality(str, Enum):
    """
    What is known about a bar's reliability.

    Flags are ADDITIVE and a bar may carry several: a minute can be
    both `GAP_BEFORE` and `LOW_OBSERVATION_COUNT`. `INVALID` is the
    only one that means "do not compute on this at all".
    """
    COMPLETE = "complete"
    PARTIAL = "partial"                       # the minute had not ended
    GAP_BEFORE = "gap_before"                 # the preceding minute is absent
    LOW_OBSERVATION_COUNT = "low_observation_count"
    OUT_OF_ORDER_INPUT = "out_of_order_input"
    DELAYED_SOURCE = "delayed_source"
    SESSION_START = "session_start"           # first bar of its session
    OUTSIDE_REGULAR_HOURS = "outside_regular_hours"
    INVALID = "invalid"                       # unusable: no price, or inconsistent OHLC


@dataclass(frozen=True)
class IntradayBar:
    """
    One research-grade minute.

    `price` and `volume` exist so this can be handed straight to the
    Phase 8 `FeatureContext`, whose accessors read exactly those names.
    `timestamp` is `bar_end` -- see the module docstring.
    """
    instrument_id: str
    bar_start: datetime
    bar_end: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    observation_count: int = 0
    source: str = ""
    quality: Tuple[BarQuality, ...] = ()

    @property
    def timestamp(self) -> datetime:
        """When this bar became a completed fact. The PIT key."""
        return self.bar_end

    @property
    def price(self) -> Optional[float]:
        """The close. Named `price` for the Phase 8 feature context."""
        return self.close

    @property
    def session_date(self) -> date:
        return self.bar_start.astimezone(timezone.utc).date()

    @property
    def is_usable(self) -> bool:
        return BarQuality.INVALID not in self.quality and self.close is not None

    @property
    def starts_a_run(self) -> bool:
        """True when the preceding minute is absent or a session began."""
        return (BarQuality.GAP_BEFORE in self.quality
                or BarQuality.SESSION_START in self.quality)

    @property
    def in_regular_hours(self) -> bool:
        return BarQuality.OUTSIDE_REGULAR_HOURS not in self.quality

    def as_dict(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "bar_start": self.bar_start.isoformat(),
            "bar_end": self.bar_end.isoformat(),
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
            "observation_count": self.observation_count,
            "source": self.source,
            "quality": [q.value for q in self.quality],
        }


def _utc(raw: object) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else \
        moment.astimezone(timezone.utc)


def _classify(instrument_id: str, stamp: datetime, previous: Optional[datetime],
              open_: Optional[float], high: Optional[float], low: Optional[float],
              close: Optional[float], observations: int,
              calendar: Optional[USEquityCalendar],
              governs_session: bool) -> Tuple[BarQuality, ...]:
    """Every quality flag this bar has earned. Order is stable."""
    flags: List[BarQuality] = []

    if close is None:
        flags.append(BarQuality.INVALID)
    elif None not in (high, low) and (high < low or close > high or close < low):
        # A bar whose own extremes contradict its close is corrupt, not
        # merely thin. Computing a return from it would propagate the
        # corruption silently.
        flags.append(BarQuality.INVALID)

    if previous is None:
        flags.append(BarQuality.SESSION_START)
    elif stamp - previous > MINUTE:
        flags.append(BarQuality.GAP_BEFORE)

    # A bar the exchange calendar governs which falls outside regular
    # hours is still recorded, but a consumer must be able to tell: an
    # overnight "one-minute return" is a gap, not momentum (§9). The
    # first regular-hours minute of a session is marked SESSION_START
    # so no window silently spans the overnight break.
    if governs_session and calendar is not None:
        window = calendar.session(stamp.astimezone(NEW_YORK).date())
        if not window.is_trading_day:
            flags.append(BarQuality.OUTSIDE_REGULAR_HOURS)
        elif not (window.opens_at <= stamp < window.closes_at):
            flags.append(BarQuality.OUTSIDE_REGULAR_HOURS)
        elif previous is not None and previous < window.opens_at:
            flags.append(BarQuality.SESSION_START)

    if observations and observations <= LOW_OBSERVATION_THRESHOLD:
        flags.append(BarQuality.LOW_OBSERVATION_COUNT)

    if not flags:
        flags.append(BarQuality.COMPLETE)
    return tuple(flags)


def load_research_bars(conn: sqlite3.Connection, instrument_id: str,
                       start: Optional[datetime] = None,
                       end: Optional[datetime] = None,
                       interval: str = RESEARCH_INTERVAL,
                       calendar: Optional[USEquityCalendar] = None,
                       governs_session: bool = False) -> List[IntradayBar]:
    """
    One instrument's research minutes, oldest first, quality-flagged.

    Reads the RESEARCH cache only. `end` is applied to `bar_end`, so a
    caller asking "as of T" can never receive the minute still running
    at T.
    """
    clauses = ["instrument_id = ?", "interval = ?"]
    params: List[object] = [instrument_id, interval]
    if start is not None:
        clauses.append("timestamp >= ?")
        params.append(start.astimezone(timezone.utc).isoformat())
    if end is not None:
        # `timestamp` is the bar's START; the bar ends a minute later,
        # so the closed-bar bound is one minute tighter.
        clauses.append("timestamp <= ?")
        params.append((end.astimezone(timezone.utc) - MINUTE).isoformat())

    try:
        rows = conn.execute(
            "SELECT timestamp, open, high, low, "
            "COALESCE(adjusted_close, close), volume, source "
            "FROM price_candle_cache WHERE " + " AND ".join(clauses) +
            " ORDER BY timestamp ASC", params).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return []
        raise

    bars: List[IntradayBar] = []
    previous: Optional[datetime] = None
    for stamp_raw, open_, high, low, close, volume, source in rows:
        stamp = _utc(stamp_raw)
        if stamp is None:
            continue
        quality = _classify(instrument_id, stamp, previous, open_, high, low,
                            close, 0, calendar, governs_session)
        bars.append(IntradayBar(
            instrument_id=instrument_id, bar_start=stamp, bar_end=stamp + MINUTE,
            open=open_, high=high, low=low, close=close, volume=volume,
            observation_count=0, source=str(source or ""), quality=quality))
        previous = stamp
    return bars


def bars_as_of(bars: Sequence[IntradayBar], cutoff: datetime
               ) -> List[IntradayBar]:
    """
    Only the bars that had ENDED at `cutoff` (§10).

    The one filter every intraday feature depends on. A bar ending
    exactly at the cutoff is included: it is a completed fact at that
    instant.
    """
    anchor = cutoff.astimezone(timezone.utc)
    return [bar for bar in bars if bar.bar_end <= anchor]


def contiguous_runs(bars: Sequence[IntradayBar]) -> List[List[IntradayBar]]:
    """
    Split into runs of consecutive, usable minutes.

    Rolling intraday features are only defined inside a run. On this
    project's real data the median run is a couple of minutes long, so
    a feature that quietly spanned a gap would be computing an
    overnight or cross-event jump and calling it one-minute momentum.
    """
    runs: List[List[IntradayBar]] = []
    current: List[IntradayBar] = []
    for bar in bars:
        if not bar.is_usable:
            if current:
                runs.append(current)
            current = []
            continue
        if current and bar.bar_start - current[-1].bar_start == MINUTE:
            current.append(bar)
        else:
            if current:
                runs.append(current)
            current = [bar]
    if current:
        runs.append(current)
    return runs


def trailing_run(bars: Sequence[IntradayBar], cutoff: datetime,
                 minimum: int = 1) -> List[IntradayBar]:
    """
    The unbroken run of closed minutes ending at `cutoff`.

    Returns `[]` when the run is shorter than `minimum` — which is what
    makes "not enough contiguous history" a distinct, explicit outcome
    rather than a quietly shorter window.
    """
    closed = bars_as_of(bars, cutoff)
    if not closed:
        return []
    runs = contiguous_runs(closed)
    if not runs:
        return []
    last = runs[-1]
    # The run must actually REACH the cutoff, not merely be the newest
    # one on record. A run that ended an hour ago describes an hour ago;
    # treating it as the window for this minute is how a feature goes
    # stale without anyone noticing. Found by its own test, which
    # asserted the behaviour this comment had claimed.
    if last[-1].bar_end != cutoff.astimezone(timezone.utc):
        return []
    return last if len(last) >= minimum else []


#: Asset classes the US equity calendar may govern. Crypto trades
#: continuously and the Bucharest listings keep their own hours, so
#: neither is judged against NYSE sessions.
US_SESSION_ASSET_CLASSES = ("stock", "etf", "equity")


def session_governed(conn: sqlite3.Connection, instrument_id: str) -> bool:
    """
    Whether the US equity calendar governs this instrument's session.

    A DOCUMENTED APPROXIMATION. This database records one exchange for
    every listed name -- `US_AND_INTL`, "US & International
    (unspecified)" -- so it genuinely cannot say whether a given stock
    trades on NYSE or in Frankfurt. US hours are therefore applied to
    the whole bucket. That affects only the informational
    `OUTSIDE_REGULAR_HOURS` flag and the two session-position
    features; no return is ever computed or dropped because of it.
    """
    try:
        row = conn.execute(
            "SELECT asset_class FROM instruments WHERE instrument_id = ?",
            (instrument_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return False
        raise
    if row is None:
        # Unlisted here: the SPY benchmark is the real case, and it is
        # a US instrument.
        return instrument_id.startswith("benchmark-")
    return str(row[0] or "").lower() in US_SESSION_ASSET_CLASSES


class BarIndex:
    """
    One instrument's bars, indexed for repeated point-in-time lookup.

    WHY THIS EXISTS. `trailing_run` scans the whole series and rebuilds
    every run each time it is called. That is fine for one decision and
    quadratic for a research batch: building a dataset over the real
    corpus called it once per observation per instrument, and the
    benchmark alone carries 25,706 minutes. Measured before this
    existed, a full build did not finish in ten minutes.

    The index computes the runs ONCE and answers by lookup. It returns
    the same objects `trailing_run` would, so the batch path and the
    operational path cannot drift apart -- asserted by test.
    """

    def __init__(self, bars: Sequence[IntradayBar]):
        self.bars = list(bars)
        #: bar_end -> (run, position within that run)
        self._where: Dict[datetime, Tuple[List[IntradayBar], int]] = {}
        for run in contiguous_runs(self.bars):
            for position, bar in enumerate(run):
                self._where[bar.bar_end] = (run, position)

    def trailing_run(self, cutoff: datetime,
                     minimum: int = 1) -> List[IntradayBar]:
        """The unbroken run of closed minutes ending exactly at `cutoff`."""
        found = self._where.get(cutoff.astimezone(timezone.utc))
        if found is None:
            return []
        run, position = found
        window = run[:position + 1]
        return window if len(window) >= minimum else []

    def bar_ending(self, moment: datetime) -> Optional[IntradayBar]:
        found = self._where.get(moment.astimezone(timezone.utc))
        return found[0][found[1]] if found else None

    @property
    def bar_ends(self) -> List[datetime]:
        return sorted(self._where)


def instruments_with_bars(conn: sqlite3.Connection,
                          interval: str = RESEARCH_INTERVAL) -> List[str]:
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT instrument_id FROM price_candle_cache "
            "WHERE interval = ? ORDER BY instrument_id", (interval,))]
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return []
        raise


# ======================================================================
# The operational -> research bridge
# ======================================================================

#: Version of the operational -> research archival transformation. Recorded
#: by the capture layer beside every archived minute, so a later change to
#: what "archivable" means is visible in the data it produced.
ARCHIVE_VERSION = "v1"

#: The research-cache `source` written for live-captured minutes. Its
#: canonical classification is `IBKR_LIVE_CAPTURE` (see src/capture).
LIVE_CAPTURE_SOURCE = "ibkr_operational_archive"

#: Rows written per transaction. A crash loses at most one batch, never a
#: whole session (Phase 25.9G, §58).
ARCHIVE_BATCH = 500


def archive_operational_bars(conn: sqlite3.Connection, *,
                             source_label: str = LIVE_CAPTURE_SOURCE,
                             interval: str = RESEARCH_INTERVAL,
                             complete_only: bool = True,
                             since: Optional[datetime] = None,
                             until: Optional[datetime] = None,
                             instruments: Optional[Sequence[str]] = None,
                             batch_size: int = ARCHIVE_BATCH
                             ) -> Dict[str, object]:
    """
    Rescue completed operational bars into the durable research cache.

    `market_data_bars` is pruned at thirty days (Phase 25.7's own
    retention policy). Without this, every minute the live poller ever
    captured would leave the database before it could support a
    reproducible study — the operational layer would generate research
    data and then destroy it.

    EXPLICIT, NOT AUTOMATIC. Only complete, non-gap bars cross the
    boundary, they are written with their own source label so a
    research consumer can always tell live-captured minutes from
    vendor ones, and an existing research candle is never overwritten:
    the vendor's record of a minute wins over our sampled one.

    PHASE 25.9G, FOR CONTINUOUS USE. Audited before the capture process
    called it for the first time, and it had never been called or
    tested. It rescanned the WHOLE operational table on every call and
    committed the lot as one transaction. Now:

      - `since` / `until` bound the scan to a window of bar STARTS, so a
        once-a-minute call reads minutes, not the month;
      - writes commit every `batch_size` rows;
      - the insert is `INSERT OR IGNORE` on the research key, so a
        second pass -- a retry, a restart, a catch-up -- is harmless;
      - the result carries `archived`, the (instrument, bar_start) keys
        actually written, so the caller can record provenance.

    Called with no bounds it behaves exactly as before.
    """
    written: Dict[str, object] = {"considered": 0, "written": 0,
                                  "skipped_existing": 0, "skipped_quality": 0,
                                  "archived": []}
    clauses: List[str] = []
    params: List[object] = []
    if since is not None:
        clauses.append("bar_start >= ?")
        params.append(since.astimezone(timezone.utc).isoformat())
    if until is not None:
        clauses.append("bar_start < ?")
        params.append(until.astimezone(timezone.utc).isoformat())
    if instruments:
        clauses.append("instrument_id IN (%s)" % ",".join("?" * len(instruments)))
        params.extend(instruments)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = conn.execute(
            "SELECT instrument_id, bar_start, open, high, low, close, volume, "
            "is_complete, is_gap FROM market_data_bars" + where +
            " ORDER BY bar_start, instrument_id", params).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return written
        raise

    pending = 0
    archived: List[Tuple[str, str]] = []
    for (instrument_id, bar_start, open_, high, low, close, volume,
         is_complete, is_gap) in rows:
        written["considered"] += 1
        if complete_only and (not is_complete or is_gap or close is None):
            written["skipped_quality"] += 1
            continue
        stamp = _utc(bar_start)
        if stamp is None:
            written["skipped_quality"] += 1
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO price_candle_cache (instrument_id, interval, "
            "timestamp, open, high, low, close, adjusted_close, volume, source, "
            "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (instrument_id, interval, stamp.isoformat(), open_, high, low,
             close, close, volume, source_label,
             datetime.now(timezone.utc).isoformat()))
        if cursor.rowcount == 1:
            written["written"] += 1
            archived.append((instrument_id, stamp.isoformat()))
            pending += 1
            if pending >= batch_size:
                conn.commit()
                pending = 0
        else:
            written["skipped_existing"] += 1
    conn.commit()
    written["archived"] = archived
    return written
