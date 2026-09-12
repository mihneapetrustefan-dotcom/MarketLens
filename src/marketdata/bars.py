"""
src/marketdata/bars.py
-------------------------------------------
One-minute bars built from operational quotes (Phase 25.7, §11-§13).

WHAT A BAR MEANS HERE
-------------------------
A bar in `price_candle_cache` is a research fact from a data vendor. A
bar in this module is a SUMMARY OF WHAT WE OBSERVED -- a poller
sampling roughly once a minute sees a handful of prices per minute,
not every trade. So `observation_count` is recorded on every bar: a
bar built from one observation is a single price wearing OHLC clothing,
and a consumer deserves to be able to tell.

That is also why these never go near the research cache. They are
honest operational telemetry and dishonest research data.

THE RULES THAT MATTER
-------------------------
COMPLETE ONLY WHEN THE MINUTE IS OVER. A minute still in progress is
not a bar. Handing a partial minute to a strategy expecting a closed
one silently changes what the strategy reacts to, and the change is
invisible in the number.

GAPS ARE RECORDED, NEVER INTERPOLATED. A minute with no observation
produces a row with `is_gap=True` and no prices. Inventing a price to
fill the hole makes a fabricated bar indistinguishable from a real one
forever after.

OLDER DATA NEVER OVERWRITES NEWER. An out-of-order or duplicate quote
is ignored with a reason, because a late arrival that rewrote the
close would move a bar a strategy had already acted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.domain.market_data_models import (
    MinuteBar, OperationalQuote, OperationalSource,
)

MINUTE = timedelta(minutes=1)


def minute_floor(moment: datetime) -> datetime:
    """The start of the minute containing `moment`, in UTC."""
    moment = moment.astimezone(timezone.utc)
    return moment.replace(second=0, microsecond=0)


@dataclass
class _Accumulator:
    """Working state for one instrument's current minute."""
    instrument_id: str
    bar_start: datetime
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    observation_count: int = 0
    #: The reference time of the newest quote folded in. Guards
    #: against out-of-order arrivals rewriting the close.
    last_reference: Optional[datetime] = None
    session_id: str = ""

    def fold(self, price: float, volume: Optional[float],
             reference: Optional[datetime]) -> None:
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        if volume is not None:
            self.volume = volume
        self.observation_count += 1
        if reference is not None:
            self.last_reference = reference

    def to_bar(self, complete: bool) -> MinuteBar:
        return MinuteBar(
            instrument_id=self.instrument_id,
            bar_start=self.bar_start,
            bar_end=self.bar_start + MINUTE,
            open=self.open, high=self.high, low=self.low, close=self.close,
            volume=self.volume,
            observation_count=self.observation_count,
            is_complete=complete,
            source=OperationalSource.IBKR_SNAPSHOT,
            session_id=self.session_id,
            is_gap=False,
        )


class MinuteBarBuilder:
    """
    Folds operational quotes into one-minute bars.

    Stateful across a session and deliberately in memory: a bar is only
    persisted once complete, so a crash loses at most the minute in
    progress. Reconstructing that partial minute from storage would
    mean persisting incomplete bars, which is exactly what must not
    happen.
    """

    def __init__(self, session_id: str = "",
                 record_gaps: bool = True):
        self.session_id = session_id
        self.record_gaps = record_gaps
        self._open: Dict[str, _Accumulator] = {}
        #: Minute already emitted per instrument. Anything at or before
        #: this is history and cannot be reopened.
        self._sealed: Dict[str, datetime] = {}
        self.rejected: List[str] = []

    # ---------------- ingestion ----------------

    def observe(self, quote: OperationalQuote,
                now: Optional[datetime] = None) -> List[MinuteBar]:
        """
        Fold one quote in, returning any bars this completes.

        A quote is only usable if it carries a price and a reference
        time. Anything else is counted as rejected with a reason rather
        than silently dropped.
        """
        price = quote.reference_price
        reference = quote.reference_time
        if price is None:
            self.rejected.append(
                f"{quote.instrument_id}: no price in quote")
            return []
        if reference is None:
            self.rejected.append(
                f"{quote.instrument_id}: no timestamp, cannot place in a minute")
            return []

        bucket = minute_floor(reference)
        sealed = self._sealed.get(quote.instrument_id)
        if sealed is not None and bucket <= sealed:
            # Out-of-order or duplicate: this minute is already
            # published. Rewriting it would move a bar a consumer may
            # have acted on.
            self.rejected.append(
                f"{quote.instrument_id}: quote for {bucket:%H:%M} arrived after "
                f"that minute was sealed at {sealed:%H:%M}")
            return []

        current = self._open.get(quote.instrument_id)
        completed: List[MinuteBar] = []

        if current is not None and bucket > current.bar_start:
            completed.extend(self._seal(quote.instrument_id, bucket))
            current = None

        if current is None:
            current = _Accumulator(
                instrument_id=quote.instrument_id, bar_start=bucket,
                session_id=self.session_id or quote.session_id)
            self._open[quote.instrument_id] = current

        if (current.last_reference is not None
                and reference < current.last_reference):
            # Same minute, but older than what we already folded. The
            # extremes are already correct; letting it set `close`
            # would make the bar end on a stale price.
            self.rejected.append(
                f"{quote.instrument_id}: out-of-order quote within "
                f"{bucket:%H:%M}, not applied to close")
            return completed

        current.fold(price, quote.volume, reference)
        return completed

    # ---------------- sealing ----------------

    def _seal(self, instrument_id: str,
              up_to: datetime) -> List[MinuteBar]:
        """
        Close the open minute and emit any empty minutes before
        `up_to` as explicit gaps.
        """
        emitted: List[MinuteBar] = []
        current = self._open.pop(instrument_id, None)
        if current is None:
            return emitted

        emitted.append(current.to_bar(complete=True))
        self._sealed[instrument_id] = current.bar_start

        if self.record_gaps:
            cursor = current.bar_start + MINUTE
            while cursor < up_to:
                emitted.append(MinuteBar(
                    instrument_id=instrument_id,
                    bar_start=cursor, bar_end=cursor + MINUTE,
                    observation_count=0, is_complete=True, is_gap=True,
                    source=OperationalSource.IBKR_SNAPSHOT,
                    session_id=self.session_id))
                self._sealed[instrument_id] = cursor
                cursor += MINUTE
        return emitted

    def flush(self, now: datetime,
              force_incomplete: bool = False) -> List[MinuteBar]:
        """
        Emit every minute that is definitively over.

        `now` decides. A minute is complete only once the clock has
        moved past its end, never because the caller ran out of
        quotes. `force_incomplete` is for session close, where the
        final partial minute is real but must still be labelled
        incomplete.
        """
        now = now.astimezone(timezone.utc)
        boundary = minute_floor(now)
        emitted: List[MinuteBar] = []
        for instrument_id in list(self._open):
            current = self._open[instrument_id]
            if current.bar_start < boundary:
                emitted.extend(self._seal(instrument_id, boundary))
            elif force_incomplete:
                self._open.pop(instrument_id)
                emitted.append(current.to_bar(complete=False))
                self._sealed[instrument_id] = current.bar_start
        return emitted

    @property
    def open_minutes(self) -> Dict[str, datetime]:
        return {k: v.bar_start for k, v in self._open.items()}
