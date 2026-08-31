"""
src/portfolio/valuation.py
-------------------------------
Point-in-time pricing for Phase 11, read exclusively from the cached
candles Phase 6 stored.

WHY THE CACHE AND NEVER A LIVE FETCH
----------------------------------------
The legacy RiskScoreCalculator computes volatility by calling yfinance
at the moment it runs. That is fine for a dashboard number and fatal
for a risk engine: the same historical decision, recomputed next
month, would use different inputs and could reach a different verdict.
A risk decision that cannot be reproduced cannot be audited, and an
unauditable risk decision is worth very little.

`price_candle_cache` already solves this — it exists precisely so a
computation reads recorded inputs rather than whatever an API returns
today. This module is the only price source Phase 11 uses. It performs
no network I/O of any kind.

THE ANCHOR IS ENFORCED IN SQL, NOT BY CONVENTION
-----------------------------------------------------
Every query here carries `timestamp <= as_of`. A candle dated after the
anchor is not merely unused — it is never loaded, so no later code
path can reach it by accident. Timestamps in this table are uniform
ISO-8601 UTC strings of equal length, which makes lexicographic
comparison exactly equivalent to chronological comparison; that is
verified by a test rather than assumed here.

STALENESS IS REPORTED, NOT SILENTLY ACCEPTED
------------------------------------------------
The most recent price at or before the anchor may still be weeks old —
a delisted instrument, a gap in the cache, or an anchor set on a
weekend. The price is returned either way, with its age, and marked
STALE past a configured threshold. Spec §40 requires a risk decision to
fail safe on stale inputs; it can only do that if staleness reaches it
as a fact instead of being rounded away.

ADJUSTED CLOSE IS PREFERRED
-------------------------------
Returns are computed from `adjusted_close` where present, falling back
to `close`. An unadjusted series turns a split into a ~50% one-day
"return", which would corrupt every volatility, correlation and VaR
figure downstream.

BATCHED BY DEFAULT
----------------------
Both accessors take a list of instruments and issue ONE query
(spec §53: no N+1). Valuing a 200-position book must not mean 200
round trips.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.portfolio_models import (
    Position, PositionValuation, ValuationStatus, finite_or_none,
)

#: Daily candles are the only interval used for portfolio risk. Minute
#: candles exist in the cache but cover ~30 days for a subset of
#: instruments — too short and too uneven to build portfolio-level
#: volatility or correlation from.
DAILY_INTERVAL = "1d"

#: Past this age a price is still returned, but marked STALE. Five
#: calendar days spans a normal weekend plus a holiday without
#: flagging, while catching a genuinely dead series.
DEFAULT_MAX_PRICE_AGE_DAYS = 5.0


def _require_utc(moment: datetime, name: str = "as_of") -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {moment.utcoffset()})")
    return moment


@dataclass(frozen=True)
class PricePoint:
    """One cached observation, already proven to be at or before the anchor."""
    instrument_id: str
    timestamp: datetime
    price: float
    volume: Optional[float] = None

    def age_days(self, as_of: datetime) -> float:
        return (as_of - self.timestamp).total_seconds() / 86400.0


class PriceRepository:
    """
    Reads the candle cache under a point-in-time anchor.

    Holds no mutable state beyond its connection and configuration, so
    two callers anchored at different moments cannot interfere.
    """

    def __init__(self, conn: sqlite3.Connection,
                 max_price_age_days: float = DEFAULT_MAX_PRICE_AGE_DAYS,
                 interval: str = DAILY_INTERVAL):
        self.conn = conn
        self.max_price_age_days = max_price_age_days
        self.interval = interval

    # ---------------- availability ----------------

    def _table_available(self) -> bool:
        """
        The cache may be absent in a fresh or partially-migrated
        database. Missing is reported as "no prices", never as an
        exception that takes down a whole risk run.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_candle_cache'"
        ).fetchone()
        return row is not None

    # ---------------- latest price at the anchor ----------------

    def prices_as_of(self, instrument_ids: Sequence[str],
                     as_of: datetime) -> Dict[str, PricePoint]:
        """
        The most recent cached price at or before `as_of`, per
        instrument, in ONE query.

        Instruments with no qualifying candle are simply absent from the
        result. The caller must treat absence as "cannot value", which
        is what PortfolioValuator does — an absent price never becomes a
        zero.
        """
        _require_utc(as_of)
        unique = sorted({i for i in instrument_ids if i})
        if not unique or not self._table_available():
            return {}

        placeholders = ",".join("?" for _ in unique)
        anchor = as_of.isoformat()

        # The inner query finds each instrument's latest qualifying
        # timestamp; the outer join fetches that row's values. Both
        # sides carry the anchor, so nothing after it is readable.
        sql = f"""
            SELECT p.instrument_id, p.timestamp,
                   COALESCE(p.adjusted_close, p.close) AS px,
                   p.volume
            FROM price_candle_cache p
            JOIN (
                SELECT instrument_id, MAX(timestamp) AS ts
                FROM price_candle_cache
                WHERE interval = ?
                  AND timestamp <= ?
                  AND COALESCE(adjusted_close, close) IS NOT NULL
                  AND instrument_id IN ({placeholders})
                GROUP BY instrument_id
            ) latest
              ON latest.instrument_id = p.instrument_id
             AND latest.ts = p.timestamp
            WHERE p.interval = ?
              AND p.timestamp <= ?
        """
        params = [self.interval, anchor, *unique, self.interval, anchor]

        found: Dict[str, PricePoint] = {}
        for instrument_id, ts, price, volume in self.conn.execute(sql, params):
            price_value = finite_or_none(price)
            if price_value is None or price_value <= 0:
                # A non-positive or non-finite price is corrupt data,
                # not a valid quote. Dropping it here means the position
                # reports MISSING_PRICE rather than producing a negative
                # market value nobody would notice.
                continue
            timestamp = datetime.fromisoformat(ts)
            if timestamp > as_of:                       # pragma: no cover - SQL already excludes
                continue
            found[instrument_id] = PricePoint(
                instrument_id=instrument_id, timestamp=timestamp,
                price=price_value, volume=finite_or_none(volume))
        return found

    def price_as_of(self, instrument_id: str, as_of: datetime) -> Optional[PricePoint]:
        """Single-instrument convenience wrapper around `prices_as_of`."""
        return self.prices_as_of([instrument_id], as_of).get(instrument_id)

    # ---------------- history for analytics ----------------

    def close_series_batch(self, instrument_ids: Sequence[str], as_of: datetime,
                           lookback_days: int) -> Dict[str, List[PricePoint]]:
        """
        Ascending price history per instrument, within
        [as_of - lookback_days, as_of], in ONE query.

        Ascending order matters: every consumer here computes
        period-over-period returns, and a reversed series would silently
        negate all of them.
        """
        _require_utc(as_of)
        unique = sorted({i for i in instrument_ids if i})
        if not unique or not self._table_available() or lookback_days <= 0:
            return {}

        start = (as_of - timedelta(days=lookback_days)).isoformat()
        placeholders = ",".join("?" for _ in unique)
        sql = f"""
            SELECT instrument_id, timestamp,
                   COALESCE(adjusted_close, close) AS px, volume
            FROM price_candle_cache
            WHERE interval = ?
              AND timestamp <= ?
              AND timestamp >= ?
              AND COALESCE(adjusted_close, close) IS NOT NULL
              AND instrument_id IN ({placeholders})
            ORDER BY instrument_id, timestamp ASC
        """
        params = [self.interval, as_of.isoformat(), start, *unique]

        series: Dict[str, List[PricePoint]] = {}
        for instrument_id, ts, price, volume in self.conn.execute(sql, params):
            price_value = finite_or_none(price)
            if price_value is None or price_value <= 0:
                continue
            series.setdefault(instrument_id, []).append(PricePoint(
                instrument_id=instrument_id, timestamp=datetime.fromisoformat(ts),
                price=price_value, volume=finite_or_none(volume)))
        return series

    def return_series_batch(self, instrument_ids: Sequence[str], as_of: datetime,
                            lookback_days: int) -> Dict[str, List[Tuple[datetime, float]]]:
        """
        Simple period-over-period returns per instrument, ascending.

        Simple rather than log returns because these are aggregated
        ACROSS instruments into a portfolio return, and log returns are
        not additive across positions — summing them would quietly
        produce a portfolio return that is not the portfolio's return.
        """
        out: Dict[str, List[Tuple[datetime, float]]] = {}
        for instrument_id, points in self.close_series_batch(
                instrument_ids, as_of, lookback_days).items():
            returns: List[Tuple[datetime, float]] = []
            for previous, current in zip(points, points[1:]):
                if previous.price <= 0:
                    continue
                value = finite_or_none((current.price - previous.price) / previous.price)
                if value is not None:
                    returns.append((current.timestamp, value))
            if returns:
                out[instrument_id] = returns
        return out


class PortfolioValuator:
    """
    Turns positions into priced valuations at an anchor.

    Deliberately produces a PositionValuation for EVERY position, even
    unpriceable ones. A valuator that returned only what it could price
    would make an incomplete portfolio look complete — the caller would
    receive a tidy list with no way to know something was dropped.
    """

    def __init__(self, prices: PriceRepository):
        self.prices = prices

    def value_positions(self, positions: Sequence[Position],
                        as_of: datetime) -> List[PositionValuation]:
        _require_utc(as_of)
        if not positions:
            return []

        found = self.prices.prices_as_of(
            [p.instrument_id for p in positions], as_of)

        valuations: List[PositionValuation] = []
        for position in positions:
            point = found.get(position.instrument_id)
            if point is None:
                valuations.append(PositionValuation(
                    position=position, as_of=as_of,
                    status=ValuationStatus.MISSING_PRICE))
                continue

            age = point.age_days(as_of)
            status = (ValuationStatus.STALE_PRICE
                      if age > self.prices.max_price_age_days
                      else ValuationStatus.VALUED)
            valuations.append(PositionValuation(
                position=position, as_of=as_of, price=point.price,
                price_timestamp=point.timestamp, status=status,
                price_age_days=round(age, 4)))
        return valuations
