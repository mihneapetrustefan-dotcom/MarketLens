"""
tests/trading/helpers.py
------------------------------
Fixtures for the Phase 25 loop tests.

WHAT IS REAL HERE AND WHAT IS NOT
-------------------------------------
Real: the database and every schema, the Phase 11 portfolio and risk
engine, the Phase 14 orchestrator, validator, state machine and
reconciler, the Phase 15 IBKR gateway, the Phase 17 intake, and the
Phase 25 loop itself.

A double: only the VENUE. `MockIBKRTransport` stands where the Client
Portal Gateway would be. That is the same seam Phase 15's own suite
uses, and the same caveat applies — the mock proves the adapter behaves
correctly against IBKR's shapes, not that IBKR behaves the way the mock
does. Closing that gap needs an actual paper account, which is what
`scripts/run_trading_loop.py --real` is for.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.backtest_schema import initialize_backtest_schema
from src.data_access.execution_schema import initialize_execution_schema
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.schema import initialize_schema
from src.data_access.signal_repository import SignalRepository
from src.data_access.signal_schema import initialize_signal_schema
from src.data_access.trading_loop_schema import initialize_trading_loop_schema
from src.domain.signal_models import Signal
from src.domain.trading_loop_models import TradingMode
from src.execution.adapters.ibkr.mock_transport import MOCK_ACCOUNT, MockContract
from src.trading.eligibility import EligibilityPolicy
from src.trading.loop import LoopConfig, TradingLoop
from src.trading.mode import TradingModeStore
from src.trading.stack import build_stack
from tests.backtest.helpers import add_bars, add_instrument, make_signal

#: Anchored on a 15-minute boundary so `cycle_anchor` is the identity
#: and a test asserting on cycle ids does not depend on rounding.
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


class AlwaysOpenCalendar:
    """
    Reports every instrument open.

    Same stub Phase 15's suite uses and for the same reason: the Phase
    12 calendar is built from cached bars, and an instrument the
    calendar has never seen is correctly UNKNOWN, which the validator
    correctly refuses. That is right behaviour that would block every
    test order, so the session gate is stubbed while the rest stays
    real.
    """

    def has_data(self, instrument_id: str) -> bool:
        return True

    def is_open(self, instrument_id, day) -> bool:
        return True


def make_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for initialize in (initialize_schema, initialize_price_cache_schema,
                       initialize_portfolio_schema, initialize_signal_schema,
                       initialize_backtest_schema, initialize_execution_schema,
                       initialize_trading_loop_schema):
        initialize(conn)
    conn.execute("INSERT OR IGNORE INTO exchanges VALUES ('X','X','US','UTC')")
    conn.commit()
    return conn


def universe(conn: sqlite3.Connection, price: float = 100.0) -> List[str]:
    """
    One instrument on a flat series.

    Flat rather than a random walk: a test asserting on a fill price or
    a target quantity should depend on the behaviour under test, not on
    a seed.
    """
    add_instrument(conn, "i-aapl", "AAPL", "technology")
    add_bars(conn, "i-aapl", end=NOW, days=60, prices=[price] * 45,
             volume=5_000_000.0)
    return ["i-aapl"]


def store_signals(conn: sqlite3.Connection,
                  signals: Sequence[Signal]) -> None:
    repository = SignalRepository(conn)
    for signal in signals:
        repository.save(signal)
    conn.commit()


def a_live_signal(instrument_id: str = "i-aapl",
                  cutoff: Optional[datetime] = None, **kwargs) -> Signal:
    return make_signal(instrument_id, cutoff or (NOW - timedelta(hours=6)),
                       signal_id=kwargs.pop("signal_id", "sig-live-1"),
                       **kwargs)


def enable_paper(conn: sqlite3.Connection, at: Optional[datetime] = None) -> None:
    TradingModeStore(conn).set_mode(
        TradingMode.PAPER, actor="test", reason="fixture", at=at or NOW)


def build_loop(conn: sqlite3.Connection, *, dry_run: bool = False,
               allow_paper_orders: bool = True,
               experimental: bool = True,
               session_id: str = "sess-test",
               contracts: Sequence[str] = ("AAPL",),
               positions: Optional[Dict[str, float]] = None,
               **config_overrides) -> TradingLoop:
    """
    A loop wired to the mock venue, ready to advance.

    `experimental=True` by default because no model is promoted in a
    fresh database — the eligibility gate would refuse every signal for
    the right reason, and a test about order flow would then be
    testing model governance instead.
    """
    stack = build_stack(conn, actor="test", mock=True,
                        allow_paper_orders=allow_paper_orders, persist=True)
    stack.gateway.calendar = AlwaysOpenCalendar()

    # The mock seeds AAPL, MSFT and IBM. Adding a second contract for a
    # symbol it already knows makes `search_contracts` return two, the
    # resolver correctly reports it AMBIGUOUS, and every order is then
    # refused for NO_INSTRUMENT_MAPPING -- which cost a debugging pass
    # the first time. Only unseeded symbols get a contract added.
    known = {c.symbol for c in stack.transport.contracts.values()}
    for index, symbol in enumerate(contracts):
        if symbol not in known:
            stack.transport.add_contract(
                MockContract(conid=str(900000 + index), symbol=symbol))
        resolution = stack.gateway.resolve_contract(
            f"i-{symbol.lower()}", symbol, sec_type="STK", currency="USD")
        if not resolution.ok:
            raise AssertionError(
                f"the fixture could not resolve {symbol}: {resolution.detail}")

    for instrument_id, quantity in (positions or {}).items():
        conid = stack.gateway._conid_for(instrument_id)
        if conid:
            stack.transport.set_position(conid, quantity, 100.0)

    policy = EligibilityPolicy(allow_experimental_models=experimental)
    config = LoopConfig(session_id=session_id, name="test loop",
                        account_id=MOCK_ACCOUNT, actor="test",
                        allow_paper_orders=allow_paper_orders,
                        experimental=experimental, eligibility=policy,
                        dry_run=dry_run, **config_overrides)
    return TradingLoop(conn, stack, config)


def table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    Row counts for every table in the database.

    The Phase 23.5 boundary method: count before, count after, and
    assert that ONLY the expected tables moved. A boundary claim that
    cannot see what it certifies is worse than no claim, because it
    gets quoted.
    """
    counts: Dict[str, int] = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            counts[name] = conn.execute(
                'SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
        except sqlite3.OperationalError:
            counts[name] = -1
    return counts


def moved(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    """Tables whose row count changed, and by how much."""
    names = set(before) | set(after)
    return {name: after.get(name, 0) - before.get(name, 0)
            for name in sorted(names)
            if after.get(name, 0) != before.get(name, 0)}


def settle(loop, order, price: float, commission: float = 1.0) -> None:
    """
    Fill an order at the venue.

    Named rather than calling `transport.fill` inline because the venue
    does more than record an execution: `MockIBKRTransport.fill` also
    moves the position book and debits cash, which is what makes the
    NEXT cycle see a smaller equity and therefore not top the position
    up. A first version of this helper debited cash a second time and
    the loop dutifully sold the difference — the fixture was wrong and
    the loop was right, which is the failure mode worth naming here.
    """
    loop.stack.transport.fill(order.broker_order_id, order.quantity, price,
                              commission=commission)
