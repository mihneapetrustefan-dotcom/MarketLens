"""
scripts/run_market_data.py
-------------------------------------------
Operator CLI for the Phase 25.7 market-data service.

SAFE BY CONSTRUCTION. This script cannot place an order: it never
imports the execution layer, never builds an IntentRequest, and the
gateway's ordering gate stays closed regardless of what is passed
here. Its whole job is to observe.

  --status      what the current market state is, per instrument
  --universe    which instruments are pollable, and why the rest are not
  --capacity    whether the universe fits the broker budget
  --cycles N    run N acquisition cycles, spaced by --interval
  --health      whether current state can be trusted right now
  --prune       apply the retention ceiling

Nothing here activates a scheduler. Phase 25.7 deliberately stops
short of a session-aware trading loop; that is Phase 25.8.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.calendar import MarketCalendar
from src.data_access.market_data_schema import (
    initialize_market_data_schema, prune as prune_market_data,
)
from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import MockIBKRTransport
from src.execution.adapters.ibkr.transport import ClientPortalTransport
from src.execution.instruments import InstrumentRegistry
from src.marketdata import universe as universe_module
from src.marketdata.bars import MinuteBarBuilder
from src.marketdata.prices import operational_price, research_price
from src.marketdata.repository import MarketDataRepository
from src.marketdata.service import (
    DEFAULT_INTERVAL_SECONDS, MarketDataService, session_id_for,
)

DEFAULT_DB = os.path.join("data", "marketlens.db")


def build_gateway(conn, args):
    config = IBKRConfig.from_environment(
        **({"account_id": args.account} if args.account else {}))
    transport = (MockIBKRTransport(config) if args.mock
                 else ClientPortalTransport(config))
    instruments = InstrumentRegistry(conn)
    instruments.load()

    calendar = MarketCalendar(conn)
    universe = [r[0] for r in conn.execute("""
        SELECT instrument_id FROM price_candle_cache WHERE interval='1d'
        GROUP BY instrument_id ORDER BY COUNT(*) DESC LIMIT 25
    """)]
    # Same discipline as run_ibkr.py: whatever we are polling must be
    # in the calendar, whatever its rank by bar count.
    for entry in universe_module.resolve_universe(conn, limit=args.limit):
        if entry.is_active and entry.instrument_id not in universe:
            universe.append(entry.instrument_id)
    if universe:
        calendar.load(universe)

    gateway = IBKRGateway(config, transport, instruments, calendar=calendar)
    gateway.connect()
    return gateway


def banner(config) -> None:
    print("=" * 70)
    print("MarketLens - Phase 25.7 operational market data")
    print("OBSERVE ONLY. This command cannot create or send an order.")
    print("=" * 70)


def show_universe(conn, args) -> None:
    entries = universe_module.resolve_universe(conn, limit=args.limit)
    active = universe_module.active(entries)
    print(f"\n--- UNIVERSE ({len(active)} active of {len(entries)}) " + "-" * 24)
    for entry in active:
        print(f"  {entry.instrument_id:26s} conid={entry.conid:<12s} "
              f"{entry.broker_symbol:8s} {entry.venue}")
    blocked = universe_module.excluded(entries)
    if blocked:
        print(f"\n  blocked ({len(blocked)}):")
        for instrument_id, reason in sorted(blocked.items())[:20]:
            print(f"    {instrument_id:26s} {reason}")
        if len(blocked) > 20:
            print(f"    ... and {len(blocked) - 20} more")


def show_capacity(service, conn, args) -> None:
    entries = universe_module.resolve_universe(conn, limit=args.limit)
    fit = service.capacity(entries)
    print("\n--- CAPACITY " + "-" * 55)
    for key in ("instruments", "requests_per_cycle", "cycles_per_minute",
                "requests_per_minute", "budget_per_minute", "headroom"):
        print(f"  {key:22s} {fit[key]}")
    print(f"  {'fits budget':22s} {'YES' if fit['fits'] else 'NO - REFUSED'}")


def show_status(conn, args) -> None:
    repository = MarketDataRepository(conn)
    rows = repository.all_latest()
    now = datetime.now(timezone.utc)
    print(f"\n--- CURRENT MARKET STATE ({len(rows)}) " + "-" * 30)
    if not rows:
        print("  (nothing recorded; run --cycles 1 first)")
        return
    for row in rows:
        instrument_id = str(row["instrument_id"])
        quote = operational_price(conn, instrument_id, now)
        age = (f"{quote.age_seconds:.0f}s"
               if quote.age_seconds is not None else "n/a")
        price = f"{quote.price:.4f}" if quote.price is not None else "n/a"
        print(f"  {instrument_id:26s} {price:>12s}  age={age:>7s}  "
              f"{quote.freshness.value:11s} {quote.availability:10s} "
              f"{'TRADEABLE' if quote.is_tradeable else 'not tradeable'}")
        if quote.note:
            print(f"      note: {quote.note}")


def show_health(service, args) -> None:
    report = service.health(limit=args.limit)
    print("\n--- MARKET DATA HEALTH " + "-" * 45)
    for key, value in report.summary().items():
        print(f"  {key:22s} {value}")
    if report.delayed_instruments:
        print(f"  delayed: {', '.join(report.delayed_instruments[:10])}")
    if report.missing_instruments:
        print(f"  missing: {', '.join(report.missing_instruments[:10])}")
    for reason in report.reasons[:10]:
        print(f"  - {reason}")


def run_cycles(service, args) -> int:
    builder = MinuteBarBuilder(session_id=session_id_for(
        datetime.now(timezone.utc)))
    service.builder = builder
    worst = 0
    for index in range(1, args.cycles + 1):
        cycle = service.run_cycle(limit=args.limit, write=not args.dry_run)
        print(f"\n--- CYCLE {index}/{args.cycles} " + "-" * 50)
        print(f"  cycle_id        {cycle.cycle_id}")
        print(f"  health          {cycle.health.value.upper()}")
        print(f"  requested       {cycle.requested}")
        print(f"  received        {cycle.received}")
        print(f"  tradeable       {cycle.tradeable}")
        print(f"  stale/invalid   {cycle.stale}/{cycle.invalid}")
        print(f"  unavailable     {cycle.unavailable}")
        print(f"  broker requests {cycle.broker_requests}")
        print(f"  bars written    {cycle.bars_written} "
              f"(gaps {cycle.gaps_recorded})")
        if cycle.duration_seconds is not None:
            print(f"  duration        {cycle.duration_seconds:.2f}s")
        for note in cycle.notes[:8]:
            print(f"    - {note}")
        if cycle.health.value in ("failed",):
            worst = 1
        if index < args.cycles:
            time.sleep(args.interval)
    if args.dry_run:
        print("\nDRY RUN - nothing was written.")
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--account", help="override IBKR_ACCOUNT_ID")
    parser.add_argument("--mock", action="store_true",
                        help="deterministic double; no venue is contacted")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the active universe (deterministic)")
    parser.add_argument("--interval", type=float,
                        default=DEFAULT_INTERVAL_SECONDS,
                        help="seconds between cycles")
    parser.add_argument("--cycles", type=int, default=0,
                        help="run N acquisition cycles")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--universe", action="store_true")
    parser.add_argument("--capacity", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--instrument", help="inspect one instrument")
    parser.add_argument("--dry-run", action="store_true",
                        help="acquire but write nothing")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database does not exist: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_market_data_schema(conn)
    gateway = build_gateway(conn, args)
    banner(gateway.config)
    print(f"  transport      {gateway.transport.name}")
    print(f"  connection     {gateway.connection_state().value}")

    service = MarketDataService(conn, gateway, interval_seconds=args.interval)

    if args.universe:
        show_universe(conn, args)
    if args.capacity:
        show_capacity(service, conn, args)
    if args.cycles:
        code = run_cycles(service, args)
        show_status(conn, args)
        return code
    if args.status:
        show_status(conn, args)
    if args.health:
        show_health(service, args)
    if args.instrument:
        now = datetime.now(timezone.utc)
        live = operational_price(conn, args.instrument, now)
        cached = research_price(conn, args.instrument, now)
        print(f"\n--- {args.instrument} " + "-" * 40)
        print(f"  OPERATIONAL  price={live.price} freshness={live.freshness.value} "
              f"age={live.age_seconds} tradeable={live.is_tradeable}")
        print(f"  RESEARCH     price={cached.price} age={cached.age_seconds}s "
              f"tradeable={cached.is_tradeable}  ({cached.note})")
    if args.prune:
        removed = prune_market_data(conn)
        print(f"\n--- RETENTION " + "-" * 54)
        print(f"  bars removed   {removed['bars']}")
        print(f"  cycles removed {removed['cycles']}")

    if not any((args.universe, args.capacity, args.cycles, args.status,
                args.health, args.instrument, args.prune)):
        show_universe(conn, args)
        show_capacity(service, conn, args)
        show_health(service, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
