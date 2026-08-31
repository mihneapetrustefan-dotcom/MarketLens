"""
tests/portfolio/helpers.py
-------------------------------
Shared fixtures for the Phase 11 tests.

Builds a real in-memory database with the canonical tables, a price
cache and the portfolio schema, so the tests exercise the same SQL the
production path uses rather than a mock that can drift from it.
"""

import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.schema import initialize_schema
from src.data_access.signal_schema import initialize_signal_schema
from src.domain.portfolio_models import (
    Position, PositionSource, PositionValuation, PortfolioSnapshot, ValuationStatus,
)
from src.domain.signal_models import (
    AgreementState, ModelContribution, Signal, SignalDirection, SignalProvenance,
    SignalStatus, SignalType,
)

AS_OF = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


def make_connection() -> sqlite3.Connection:
    """A database carrying every schema Phase 11 reads."""
    conn = sqlite3.connect(":memory:")
    initialize_schema(conn)
    initialize_price_cache_schema(conn)
    initialize_portfolio_schema(conn)
    initialize_signal_schema(conn)
    return conn


def add_instrument(conn: sqlite3.Connection, instrument_id: str, ticker: str,
                   sector_id: Optional[str] = None, asset_class: str = "stock",
                   currency: str = "USD", company_name: Optional[str] = None) -> None:
    """Insert the full canonical chain for one instrument."""
    company_id = f"{ticker.lower()}-co"
    security_id = f"{ticker.lower()}-sec"

    if sector_id:
        conn.execute("INSERT OR IGNORE INTO sectors VALUES (?,?)",
                     (sector_id, sector_id.replace("-", " ").title()))
    conn.execute("INSERT OR IGNORE INTO exchanges VALUES ('X','X','US','UTC')")
    conn.execute("INSERT OR REPLACE INTO companies VALUES (?,?,'[]',?)",
                 (company_id, company_name or ticker, sector_id))
    conn.execute("INSERT OR REPLACE INTO securities VALUES (?,?,'common_stock',?)",
                 (security_id, company_id, currency))
    conn.execute("INSERT OR REPLACE INTO instruments VALUES (?,?, 'X', ?, ?)",
                 (instrument_id, security_id, ticker, asset_class))
    conn.commit()


def add_candles(conn: sqlite3.Connection, instrument_id: str, as_of: datetime,
                days: int = 200, start_price: float = 100.0,
                daily_drift: float = 0.0005, daily_vol: float = 0.018,
                seed: int = 7, volume: float = 1_000_000.0,
                prices: Optional[Sequence[float]] = None) -> List[float]:
    """
    Daily candles ending at `as_of`.

    An explicit `prices` sequence overrides the random walk, so a test
    that needs an exact return series can state it rather than hoping a
    seed produces it.
    """
    generator = random.Random(seed)
    series: List[float] = []
    price = start_price
    for index in range(days):
        if prices is not None:
            price = prices[index]
        else:
            price *= (1 + generator.gauss(daily_drift, daily_vol))
        series.append(price)
        timestamp = (as_of - timedelta(days=days - index)).replace(
            hour=4, minute=0, second=0, microsecond=0)
        conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (instrument_id, "1d", timestamp.isoformat(), price, price, price,
             price, price, volume, "test", as_of.isoformat()))
    conn.commit()
    return series


def make_position(instrument_id: str, quantity: float, entry: float = 100.0,
                  portfolio_id: str = "pf", position_id: Optional[str] = None,
                  opened_at: Optional[datetime] = None,
                  closed_at: Optional[datetime] = None,
                  currency: str = "USD") -> Position:
    return Position(
        position_id=position_id or f"pos-{instrument_id}",
        portfolio_id=portfolio_id,
        instrument_id=instrument_id,
        quantity=quantity,
        average_entry_price=entry,
        currency=currency,
        source=PositionSource.DECLARED,
        opened_at=opened_at or (AS_OF - timedelta(days=90)),
        closed_at=closed_at,
    )


def make_valuation(instrument_id: str, quantity: float, price: Optional[float],
                   as_of: datetime = AS_OF, entry: float = 100.0,
                   status: ValuationStatus = ValuationStatus.VALUED,
                   age_days: Optional[float] = 0.0,
                   currency: str = "USD") -> PositionValuation:
    return PositionValuation(
        position=make_position(instrument_id, quantity, entry, currency=currency),
        as_of=as_of, price=price,
        price_timestamp=as_of if price is not None else None,
        status=status, price_age_days=age_days)


def make_snapshot(valuations: Sequence[PositionValuation] = (),
                  cash: float = 0.0, portfolio_id: str = "pf",
                  as_of: datetime = AS_OF,
                  unvalued: Sequence[PositionValuation] = ()) -> PortfolioSnapshot:
    """A snapshot with exposure totals filled in the way the service fills them."""
    snapshot = PortfolioSnapshot(
        portfolio_id=portfolio_id, as_of=as_of, cash=cash,
        valuations=list(valuations), unvalued_positions=list(unvalued),
        currencies=[v.position.currency for v in list(valuations) + list(unvalued)])

    for valuation in valuations:
        exposure = valuation.exposure or 0.0
        snapshot.gross_exposure += exposure
        if valuation.position.is_short:
            snapshot.short_exposure += exposure
        else:
            snapshot.long_exposure += exposure
    return snapshot


def make_signal(instrument_id: str, confidence: float = 0.8,
                direction: SignalDirection = SignalDirection.LONG,
                signal_id: Optional[str] = None,
                status: SignalStatus = SignalStatus.ACTIVE,
                cutoff: Optional[datetime] = None,
                valid_until: Optional[datetime] = None,
                strength: float = 0.5) -> Signal:
    return Signal(
        signal_id=signal_id or f"sig-{instrument_id}",
        instrument_id=instrument_id,
        signal_type=SignalType.DIRECTIONAL,
        direction=direction,
        status=status,
        strength=strength,
        confidence=confidence,
        agreement_state=AgreementState.AGREEMENT,
        provenance=SignalProvenance(
            strategy_id="test", strategy_version="v1",
            source_information_cutoff=cutoff or (AS_OF - timedelta(days=1))),
        created_at=AS_OF - timedelta(days=1),
        valid_from=AS_OF - timedelta(days=1),
        valid_until=valid_until or (AS_OF + timedelta(days=4)),
    )
