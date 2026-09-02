"""
tests/paper/helpers.py
---------------------------
Shared fixtures for the Phase 13 tests.

Builds a real in-memory database with every schema the paper session
reads, plus deterministic price series. Determinism matters here for the
same reason it did in Phase 12: a recovery test that used a random walk
without a fixed seed would fail intermittently and teach everyone to
ignore it.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.backtest_schema import initialize_backtest_schema
from src.data_access.paper_schema import initialize_paper_schema
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.schema import initialize_schema
from src.data_access.signal_schema import initialize_signal_schema
from src.domain.paper_models import (
    OrderSide, PaperAccount, PaperFill, PaperOrder, PaperOrderType, PaperSession,
    PaperSessionConfig, TimeInForce,
)
from tests.backtest.helpers import add_bars, add_instrument, make_signal

END = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
START = END - timedelta(days=200)


def make_connection() -> sqlite3.Connection:
    """A database carrying every schema Phase 13 touches."""
    conn = sqlite3.connect(":memory:")
    for initialize in (initialize_schema, initialize_price_cache_schema,
                       initialize_portfolio_schema, initialize_signal_schema,
                       initialize_backtest_schema, initialize_paper_schema):
        initialize(conn)
    conn.execute("INSERT OR IGNORE INTO exchanges VALUES ('X','X','US','UTC')")
    conn.commit()
    return conn


def standard_universe(conn: sqlite3.Connection) -> List[str]:
    """Two equities in different sectors, both with deterministic bars."""
    add_instrument(conn, "i-aaa", "AAA", "technology")
    add_instrument(conn, "i-bbb", "BBB", "energy")
    add_bars(conn, "i-aaa", end=END, days=220, start_price=100.0, seed=1)
    add_bars(conn, "i-bbb", end=END, days=220, start_price=50.0, seed=2)
    return ["i-aaa", "i-bbb"]


def flat_universe(conn: sqlite3.Connection, price: float = 100.0,
                  volume: float = 10_000.0) -> List[str]:
    """
    One instrument on a perfectly flat series.

    Used wherever a test needs an exactly predictable fill price — a
    random walk would make the assertion depend on the seed rather than
    on the behaviour under test.
    """
    add_instrument(conn, "i-flat", "FLAT", "technology")
    add_bars(conn, "i-flat", end=END, days=40, prices=[price] * 30,
             volume=volume)
    return ["i-flat"]


def make_account(account_id: str = "acct-1", capital: float = 100_000.0,
                 **overrides) -> PaperAccount:
    defaults = dict(account_id=account_id, name="Test account",
                    initial_capital=capital, created_at=START)
    defaults.update(overrides)
    return PaperAccount(**defaults)


def make_config(universe: Sequence[str], **overrides) -> PaperSessionConfig:
    defaults = dict(universe=list(universe), sizing_target_weight=0.10,
                    commission_bps=2.0, slippage_bps=5.0,
                    signal_to_order_seconds=60.0)
    defaults.update(overrides)
    return PaperSessionConfig(**defaults)


def make_session(config: PaperSessionConfig, session_id: str = "sess-1",
                 account_id: str = "acct-1", **overrides) -> PaperSession:
    defaults = dict(session_id=session_id, account_id=account_id,
                    name="Test session", config=config)
    defaults.update(overrides)
    return PaperSession(**defaults)


def make_order(instrument_id: str = "i-flat", at: Optional[datetime] = None,
               **overrides) -> PaperOrder:
    defaults = dict(
        order_id="o-1", session_id="sess-1", account_id="acct-1",
        instrument_id=instrument_id, side=OrderSide.BUY, quantity=10.0,
        order_type=PaperOrderType.MARKET, time_in_force=TimeInForce.DAY,
        created_at=at or END, decided_at=at or END)
    defaults.update(overrides)
    return PaperOrder(**defaults)


def make_fill(order_id: str = "o-1", instrument_id: str = "i-flat",
              at: Optional[datetime] = None, **overrides) -> PaperFill:
    defaults = dict(
        fill_id="f-1", session_id="sess-1", order_id=order_id,
        account_id="acct-1", instrument_id=instrument_id, side=OrderSide.BUY,
        quantity=10.0, price=100.0, reference_price=100.0,
        filled_at=at or END, commission=1.0, slippage_cost=0.5)
    defaults.update(overrides)
    return PaperFill(**defaults)


def signals_for(instrument_ids: Sequence[str], end: datetime = END,
                count: int = 4, spacing_days: int = 12, **kwargs) -> list:
    """A spread of live signals through the recent period."""
    out = []
    for position, instrument_id in enumerate(instrument_ids):
        for index in range(count):
            cutoff = end - timedelta(days=50 - index * spacing_days + position)
            out.append(make_signal(
                instrument_id, cutoff,
                signal_id=f"sg-{instrument_id}-{index}",
                confidence=kwargs.pop("confidence", 0.8), **kwargs))
    return out


def anchors_for(conn: sqlite3.Connection, universe: Sequence[str],
                days: int = 40, end: datetime = END) -> List[datetime]:
    """The cached sessions a replay would step through."""
    from src.backtest.calendar import MarketCalendar
    calendar = MarketCalendar(conn)
    calendar.load(list(universe))
    return calendar.evaluation_dates(list(universe), end - timedelta(days=days), end)
