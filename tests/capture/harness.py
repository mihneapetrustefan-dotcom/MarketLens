"""
tests/capture/harness.py
------------------------------
A venue that moves with a replay clock, and a capture store in memory.

Everything real except two things: the transport is the Phase 15 mock
and time is a `ReplayClock`. The gateway, guard, service, bar builder,
archive, feature builder, calendar and lease are the production objects.
"""

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from src.capture.runner import CaptureConfig, CaptureRunner
from src.capture.schema import initialize_capture_schema
from src.capture.stack import build_capture_gateway
from src.capture.universe import load_definition
from src.execution.adapters.ibkr.config import paper_config
from src.execution.adapters.ibkr.mock_transport import MockContract, MockIBKRTransport
from src.trading.clock import ReplayClock

# Friday 2026-09-18, a regular session: 13:30-20:00 UTC.
SESSION_DAY = datetime(2026, 9, 18, tzinfo=timezone.utc)
OPEN = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)

#: AAPL is deliberately absent: the mock already seeds an AAPL contract,
#: so adding a second makes it a genuine AMBIGUOUS case (used on purpose
#: by the mapping tests).
TICKERS = ("SPY", "AMZN", "COST", "VZ", "KMB", "ITW")


def write_universe(directory, tickers=TICKERS, version="vtest"):
    members = []
    for i, ticker in enumerate(tickers):
        members.append({
            "instrument_id": ("benchmark-spy" if ticker == "SPY"
                              else f"us_and_intl-{ticker.lower()}"),
            "ticker": ticker, "sector_id": f"s{i}", "asset_class":
            "etf" if ticker == "SPY" else "stock", "sec_type": "STK",
            "currency": "USD", "role": "benchmark" if ticker == "SPY" else "member"})
    path = os.path.join(directory, f"capture_universe_{version}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": version, "instruments": members}, handle)
    return path


class MovingVenue(MockIBKRTransport):
    """Prices are a deterministic function of the clock: a snapshot at
    the same moment always returns the same price, so runs reproduce."""

    def __init__(self, clock, tickers=TICKERS, **kwargs):
        super().__init__(paper_config(), **kwargs)
        self.clock = clock
        self.snapshot_calls = 0
        self.silent = set()          # conids the venue says nothing about
        self.stale_seconds = 0       # >0: quotes carry an old venue timestamp
        for i, ticker in enumerate(tickers):
            conid = str(900000 + i)
            self.add_contract(MockContract(conid=conid, symbol=ticker,
                                           primary_exchange="NYSE",
                                           company_name=ticker))

    def market_snapshot(self, conids, fields=()):
        self.snapshot_calls += 1
        now = self.clock.now()
        seconds = (now - SESSION_DAY).total_seconds()
        for conid in conids:
            if conid in self.quotes:
                seed = int(hashlib.sha1(conid.encode()).hexdigest()[:6], 16) % 50
                price = 100 + seed + 2 * math.sin(seconds / 900.0 + seed)
                self.set_quote(conid, price, price - 0.01, price + 0.01)
        payloads = super().market_snapshot(conids, fields)
        if self.stale_seconds:
            stamp = int((now.timestamp() - self.stale_seconds) * 1000)
            payloads = [dict(p, _updated=stamp) for p in payloads]
        return [p for p in payloads if p["conid"] not in self.silent]


def make_runner(conn, clock, directory, tickers=TICKERS, owner="supervisor-test",
                venue=None, config=None, stop=lambda: False):
    venue = venue or MovingVenue(clock, tickers)
    gateway = build_capture_gateway(conn, transport=venue)
    definition = load_definition(write_universe(directory, tickers))
    runner = CaptureRunner(conn, gateway, definition, clock=clock,
                           lease_owner=owner, config=config or CaptureConfig(),
                           stop_requested=stop)
    return runner, venue


def capture_db():
    conn = sqlite3.connect(":memory:")
    initialize_capture_schema(conn)
    return conn


def run_until(runner, clock, until, max_steps=100000):
    """Drive step/sleep exactly as `run` does, up to a moment."""
    steps = 0
    while clock.now() < until and steps < max_steps:
        delay = runner.step()
        runner._sleep(delay)
        steps += 1
    return steps
