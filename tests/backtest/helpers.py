"""
tests/backtest/helpers.py
------------------------------
Shared fixtures for the Phase 12 tests.

Builds a real in-memory database carrying every schema the backtest
engine reads, with deterministic price series. Deterministic matters
more here than anywhere else in the project: a reproducibility test
that used a random walk without a fixed seed would fail intermittently
and teach everyone to ignore it.
"""

import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import sqlite3

from src.data_access.backtest_schema import initialize_backtest_schema
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.schema import initialize_schema
from src.data_access.signal_schema import initialize_signal_schema
from src.domain.backtest_models import (
    BacktestConfiguration, CostModel, ExecutionAssumptions, SlippageMethod,
    SlippageModel,
)
from src.domain.signal_models import (
    AgreementState, Signal, SignalContext, SignalDirection, SignalProvenance,
    SignalStatus, SignalType,
)

END = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
START = END - timedelta(days=200)


def make_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for initialize in (initialize_schema, initialize_price_cache_schema,
                       initialize_portfolio_schema, initialize_signal_schema,
                       initialize_backtest_schema):
        initialize(conn)
    conn.execute("INSERT OR IGNORE INTO exchanges VALUES ('X','X','US','UTC')")
    conn.commit()
    return conn


def add_instrument(conn: sqlite3.Connection, instrument_id: str, ticker: str,
                   sector_id: Optional[str] = None,
                   asset_class: str = "stock") -> None:
    if sector_id:
        conn.execute("INSERT OR IGNORE INTO sectors VALUES (?,?)",
                     (sector_id, sector_id.replace("-", " ").title()))
    conn.execute("INSERT OR REPLACE INTO companies VALUES (?,?,'[]',?)",
                 (f"{ticker}-co", ticker, sector_id))
    conn.execute("INSERT OR REPLACE INTO securities VALUES (?,?,'common_stock','USD')",
                 (f"{ticker}-sec", f"{ticker}-co"))
    conn.execute("INSERT OR REPLACE INTO instruments VALUES (?,?, 'X', ?, ?)",
                 (instrument_id, f"{ticker}-sec", ticker, asset_class))
    conn.commit()


def add_bars(conn: sqlite3.Connection, instrument_id: str,
             end: datetime = END, days: int = 220, start_price: float = 100.0,
             seed: int = 1, drift: float = 0.0008, vol: float = 0.015,
             weekdays_only: bool = True, volume: float = 5_000_000.0,
             prices: Optional[Sequence[float]] = None) -> List[float]:
    """
    Daily bars ending at `end`.

    `weekdays_only` reproduces the real cache's shape: equities have no
    weekend sessions, crypto does. An explicit `prices` sequence
    overrides the walk so a test can state the exact series it needs.
    """
    generator = random.Random(seed)
    written: List[float] = []
    price = start_price
    index = 0
    for offset in range(days):
        timestamp = (end - timedelta(days=days - offset)).replace(
            hour=4, minute=0, second=0, microsecond=0)
        if weekdays_only and timestamp.weekday() >= 5:
            continue
        if prices is not None:
            if index >= len(prices):
                break
            price = prices[index]
        else:
            price *= (1 + generator.gauss(drift, vol))
        index += 1
        written.append(price)
        conn.execute(
            "INSERT OR REPLACE INTO price_candle_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (instrument_id, "1d", timestamp.isoformat(), price * 0.995,
             price * 1.01, price * 0.99, price, price, volume, "test",
             end.isoformat()))
    conn.commit()
    return written


def make_signal(instrument_id: str, cutoff: datetime, signal_id: Optional[str] = None,
                confidence: float = 0.75, strength: float = 0.6,
                direction: SignalDirection = SignalDirection.LONG,
                valid_days: float = 20.0, event_type: str = "earnings",
                strategy_id: str = "test",
                suppressed: bool = False) -> Signal:
    signal = Signal(
        signal_id=signal_id or f"sig-{instrument_id}-{cutoff.date().isoformat()}",
        instrument_id=instrument_id, signal_type=SignalType.DIRECTIONAL,
        direction=direction, status=SignalStatus.ACTIVE, strength=strength,
        confidence=confidence, agreement_state=AgreementState.AGREEMENT,
        context=SignalContext(event_type=event_type, data_quality_level="high"),
        provenance=SignalProvenance(strategy_id=strategy_id, strategy_version="v1",
                                    source_information_cutoff=cutoff),
        created_at=cutoff, valid_from=cutoff,
        valid_until=cutoff + timedelta(days=valid_days))
    if suppressed:
        from src.domain.signal_models import SuppressionReason
        signal.suppress(SuppressionReason.LOW_CONFIDENCE)
    return signal


def make_config(**overrides) -> BacktestConfiguration:
    defaults = dict(
        name="test", start=START, end=END, initial_capital=100_000.0,
        universe=["i-aaa", "i-bbb"], benchmark_instrument_id=None,
        costs=CostModel(commission_bps=2.0),
        slippage=SlippageModel(method=SlippageMethod.FIXED_BPS, base_bps=5.0),
        execution=ExecutionAssumptions(), rebalance_days=5,
        sizing_target_weight=0.10)
    defaults.update(overrides)
    return BacktestConfiguration(**defaults)


def standard_universe(conn: sqlite3.Connection) -> None:
    """Two equities in different sectors plus a benchmark, all with bars."""
    add_instrument(conn, "i-aaa", "AAA", "technology")
    add_instrument(conn, "i-bbb", "BBB", "energy")
    add_instrument(conn, "bench", "SPY", "index")
    add_bars(conn, "i-aaa", start_price=100.0, seed=1)
    add_bars(conn, "i-bbb", start_price=50.0, seed=2)
    add_bars(conn, "bench", start_price=400.0, seed=3)


def signals_across(instrument_ids: Sequence[str], count: int = 5,
                   first_offset_days: int = 20, spacing_days: int = 25,
                   **kwargs) -> List[Signal]:
    """A spread of signals through the standard period."""
    out: List[Signal] = []
    for position, instrument_id in enumerate(instrument_ids):
        for index in range(count):
            cutoff = START + timedelta(
                days=first_offset_days + index * spacing_days + position)
            out.append(make_signal(
                instrument_id, cutoff,
                signal_id=f"sig-{instrument_id}-{index}", **kwargs))
    return out
