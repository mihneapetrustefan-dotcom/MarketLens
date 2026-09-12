"""
scripts/run_session.py
-------------------------------------------
The Phase 25.8 session runner.

WHAT THIS IS
----------------
An unattended runner that operates the existing trading architecture
from market open to market close, on REAL wall-clock time. It replaces
running `run_trading_loop.py --cycles 1` by hand every fifteen
minutes.

WHAT IT IS NOT
------------------
Live autonomous trading. Live remains impossible, orders stay behind
the Phase 14/25 paper gates, and `--allow-paper-orders` is required
before anything can be sent at all. The runner reaching the execution
boundary is not permission to trade.

It is also not fully unattended in the governance sense: the Client
Portal Gateway needs a human browser login, and no model has been
promoted. The runner reports when it is waiting on one of those rather
than pretending otherwise.

RUNTIME DEPENDENCY
----------------------
This needs a host that stays awake for a session. GitHub Actions
cannot do it -- the audit established that a runner cannot hold a
Client Portal session -- so this is a local supervised process or a
scheduled task on a machine that is already running the gateway. That
is an operational deployment dependency, documented rather than
pretended away.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest.calendar import MarketCalendar
from src.data_access.market_data_schema import initialize_market_data_schema
from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import MockIBKRTransport
from src.execution.adapters.ibkr.transport import ClientPortalTransport
from src.execution.instruments import InstrumentRegistry
from src.marketdata import universe as universe_module
from src.marketdata.bars import MinuteBarBuilder
from src.marketdata.service import MarketDataService, session_id_for
from src.trading.clock import RunMode, WallClock
from src.trading.eligibility import EligibilityPolicy
from src.trading.loop import LoopConfig, TradingLoop
from src.trading.schedule import DEFAULT_CADENCES, Schedule
from src.trading.session_runner import SessionRefused, SessionRunner
from src.trading.stack import build_stack

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")


def build_market_data(conn, args):
    config = IBKRConfig.from_environment(
        **({"account_id": args.account} if args.account else {}))
    transport = (MockIBKRTransport(config) if args.mock
                 else ClientPortalTransport(config))
    instruments = InstrumentRegistry(conn)
    instruments.load()

    calendar = MarketCalendar(conn)
    names = [r[0] for r in conn.execute("""
        SELECT instrument_id FROM price_candle_cache WHERE interval='1d'
        GROUP BY instrument_id ORDER BY COUNT(*) DESC LIMIT 25
    """)]
    for entry in universe_module.resolve_universe(conn, limit=args.limit):
        if entry.is_active and entry.instrument_id not in names:
            names.append(entry.instrument_id)
    if names:
        calendar.load(names)

    gateway = IBKRGateway(config, transport, instruments, calendar=calendar)
    gateway.connect()
    service = MarketDataService(conn, gateway, interval_seconds=args.tick)
    service.builder = MinuteBarBuilder(
        session_id=session_id_for(datetime.now(timezone.utc)))
    return gateway, service


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--actor", default="session-runner")
    parser.add_argument("--session", default="sess-paper-loop-1",
                        help="loop session id to advance")
    parser.add_argument("--worker", default="")
    parser.add_argument("--account", help="override IBKR_ACCOUNT_ID")
    parser.add_argument("--mock", action="store_true",
                        help="deterministic double; no venue is contacted")
    parser.add_argument("--limit", type=int, default=25,
                        help="cap the active universe")
    parser.add_argument("--tick", type=float, default=60.0,
                        help="runner heartbeat in seconds")
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="stop after N ticks (for supervised runs)")
    parser.add_argument("--allow-paper-orders", action="store_true",
                        help="open the paper ordering gate; without this "
                             "the runner observes and decides but sends "
                             "nothing")
    parser.add_argument("--experimental", action="store_true")
    parser.add_argument("--describe", action="store_true",
                        help="print operational state as JSON and exit")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    for name in sorted(DEFAULT_CADENCES):
        parser.add_argument(f"--cadence-{name.replace('_', '-')}", type=float,
                            default=None, dest=f"cadence_{name}",
                            help=f"override the {name} cadence in seconds")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database does not exist: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_market_data_schema(conn)
    gateway, market_data = build_market_data(conn, args)

    stack = build_stack(conn, actor=args.actor, mock=args.mock,
                        account_id=args.account,
                        allow_paper_orders=args.allow_paper_orders,
                        universe_limit=args.limit,
                        persist=not args.dry_run)
    loop = TradingLoop(
        conn, stack,
        config=LoopConfig(
            session_id=args.session, name="session runner",
            account_id=args.account or stack.account_id, actor=args.actor,
            cycle_seconds=args.tick,
            universe_limit=args.limit,
            allow_paper_orders=args.allow_paper_orders,
            experimental=args.experimental,
            eligibility=EligibilityPolicy(
                allow_experimental_models=args.experimental),
            dry_run=args.dry_run))

    overrides = {name: getattr(args, f"cadence_{name}")
                 for name in DEFAULT_CADENCES
                 if getattr(args, f"cadence_{name}") is not None}
    schedule = Schedule.default(overrides=overrides, tick_seconds=args.tick)

    runner = SessionRunner(
        conn, loop, market_data,
        mode=RunMode.PAPER_SESSION,
        clock=WallClock(),
        schedule=schedule,
        worker=args.worker or args.actor,
        orders_enabled=args.allow_paper_orders and not args.dry_run,
        max_ticks=args.max_ticks)

    print("=" * 70)
    print("MarketLens - Phase 25.8 session runner")
    print("PAPER ONLY. Live trading is impossible; no flag here enables it.")
    print("=" * 70)
    print(f"  transport        {gateway.transport.name}")
    print(f"  connection       {gateway.connection_state().value}")
    print(f"  clock            {type(runner.clock).__name__} "
          f"(wall clock: {runner.clock.is_wall_clock})")
    print(f"  tick             {args.tick:.0f}s")
    print(f"  orders enabled   {runner.orders_enabled}")

    if args.describe:
        print(json.dumps(runner.describe(), indent=2, default=str))
        return 0

    try:
        state = runner.start(account=args.account or "")
    except SessionRefused as refusal:
        print(f"\nSESSION NOT STARTED: {refusal}")
        return 2

    print(f"\n  session          {state.session_id}")
    print(f"  started          {state.started_at.isoformat()}")
    print("\n  fingerprint:")
    for key, value in sorted(state.fingerprint.items()):
        print(f"    {key:22s} {value}")

    print("\nRunning until session close. Ctrl+C stops cleanly.")
    try:
        state = runner.run_until_close(account=args.account or "")
    except KeyboardInterrupt:
        runner.stop()
        runner.close()
        print("\n  interrupted; session closed cleanly")

    print("\n--- SESSION SUMMARY " + "-" * 48)
    print(f"  ticks            {state.ticks}")
    print(f"  loop cycles      {state.cycles_run}")
    print(f"  orders submitted {state.orders_submitted}")
    print(f"  final health     {state.last_health.value.upper()}")
    print(f"  missed boundaries {schedule.missed_boundaries}")
    for block in state.blocks[:10]:
        print(f"    - {block}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
