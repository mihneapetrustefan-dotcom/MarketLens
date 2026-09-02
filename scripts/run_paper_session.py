"""
Runs a Phase 13 paper trading session.

WHAT THIS DOES
------------------
Advances a durable paper session: for each tick it checks data
freshness, collects the signals live at that moment, runs the REAL
Phase 11 risk engine over the paper book, places orders through the
paper executor, fills them against cached bars, then snapshots,
reconciles and persists.

WHAT IT DOES NOT DO
-----------------------
It does not contact a broker, hold a credential, or place a real order.
`ExecutionVenue.PAPER` is the only venue that exists, and the executor
prices every fill against stored bars.

IT IS NOT A DAEMON, AND THAT IS DELIBERATE
----------------------------------------------
This repository has no persistent runtime — every phase runs as a batch
job under cron. So the session lives in the database and this script
ADVANCES it. `--ticks N` steps N times; `--replay` walks the cached
sessions in a period; a cron schedule calling `--ticks 1` is a
continuously-running paper account. State survives between invocations,
and `--resume` reconstructs it after a crash.

ON THIS DATABASE
--------------------
Every stored signal is suppressed, so a real session produces zero
orders and reports exactly that. `--synthetic-signals` exercises the
machinery; runs launched that way are labelled and must never be read
as evidence about a strategy.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- The paper book lives in its own tables; `positions` and `portfolios`
  are never written.
- Idempotency keys make a repeated invocation safe.
- --dry-run advances in memory without persisting.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.backtest.calendar import MarketCalendar
from src.data_access.paper_repository import PaperRepository
from src.data_access.paper_schema import initialize_paper_schema
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.domain.paper_models import (
    PaperAccount, PaperAccountStatus, PaperSession, PaperSessionConfig,
    PaperSessionStatus,
)
from src.paper.clock import FixedClock, ReplayClock, SystemClock
from src.paper.comparison import compare, detect_drift, paper_metrics_from
from src.paper.session import PaperTradingSession

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")


def fmt(value, digits=2, suffix=""):
    return "n/a" if value is None else f"{value:,.{digits}f}{suffix}"


def fmt_pct(value, digits=2):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def build_synthetic_signals(universe, start, end, every_days=20):
    """
    Generated signals for exercising the machinery.

    Crude and clearly labelled. They prove the pipeline runs; they say
    nothing about whether any strategy is any good.
    """
    from src.domain.signal_models import (
        AgreementState, Signal, SignalContext, SignalDirection, SignalProvenance,
        SignalStatus, SignalType,
    )
    signals = []
    span = (end - start).days
    for position, instrument_id in enumerate(universe):
        offset = 10 + position
        while offset < span - 5:
            cutoff = start + timedelta(days=offset)
            signals.append(Signal(
                signal_id=f"synthetic-{instrument_id}-{offset}",
                instrument_id=instrument_id, signal_type=SignalType.DIRECTIONAL,
                direction=SignalDirection.LONG, status=SignalStatus.ACTIVE,
                strength=0.6, confidence=0.75,
                agreement_state=AgreementState.AGREEMENT,
                context=SignalContext(event_type="synthetic",
                                      data_quality_level="high"),
                provenance=SignalProvenance(strategy_id="synthetic",
                                            strategy_version="v0",
                                            source_information_cutoff=cutoff),
                created_at=cutoff, valid_from=cutoff,
                valid_until=cutoff + timedelta(days=15)))
            offset += every_days
    return signals


def resolve_universe(conn, requested: Optional[str], limit: int) -> List[str]:
    if requested:
        return [i.strip() for i in requested.split(",") if i.strip()]
    rows = conn.execute("""
        SELECT instrument_id, COUNT(*) n FROM price_candle_cache
        WHERE interval = '1d' AND instrument_id NOT LIKE 'benchmark%'
        GROUP BY instrument_id HAVING n >= 60 ORDER BY n DESC, instrument_id
        LIMIT ?
    """, (limit,)).fetchall()
    return [r[0] for r in rows]


def print_tick(result, index: int, total: int) -> None:
    flags = []
    if result.was_blocked:
        flags.append(f"BLOCKED: {result.blocked_reason}")
    if result.orders_rejected:
        flags.append(f"{result.orders_rejected} rejected")
    snapshot = result.snapshot
    print(f"  [{index:>3}/{total}] {result.at.date()} "
          f"signals={result.signals_observed:<3} "
          f"orders={result.orders_created:<3} fills={result.fills:<3} "
          f"equity={fmt(snapshot.equity if snapshot else None):>12} "
          f"data={result.freshness.value:<11} health={result.health.value:<9}"
          + ("  " + " | ".join(flags) if flags else ""))


def print_status(session_runner, repository) -> None:
    session = session_runner.session
    account = session_runner.account
    snapshot = session_runner.snapshots[-1] if session_runner.snapshots else None

    print("\n--- PAPER ACCOUNT " + "-" * 54)
    print("  ! PAPER MODE - no broker is connected and no real order can be placed")
    print(f"  account          {account.account_id} ({account.status.value})")
    print(f"  session          {session.session_id} ({session.status.value})")
    print(f"  ticks processed  {session.ticks_processed}")
    print(f"  initial capital  {fmt(account.initial_capital)} {account.base_currency}")

    if snapshot is not None:
        print(f"  equity           {fmt(snapshot.equity)}")
        print(f"  cash             {fmt(snapshot.cash)}")
        print(f"  positions        {snapshot.open_positions} open")
        print(f"  gross / net      {fmt(snapshot.gross_exposure)} / "
              f"{fmt(snapshot.net_exposure)}")
        print(f"  realized P&L     {fmt(snapshot.realized_pnl)}")
        print(f"  unrealized P&L   {fmt(snapshot.unrealized_pnl)}")
        print(f"  drawdown         {fmt_pct(snapshot.drawdown)}")
        print(f"  data freshness   {snapshot.data_freshness.value}")
        print(f"  health           {snapshot.health.value}")

    print("\n--- EXECUTION " + "-" * 58)
    orders = session_runner.executor.get_orders()
    by_state = {}
    for order in orders:
        by_state[order.state.value] = by_state.get(order.state.value, 0) + 1
    print(f"  orders           {len(orders)} total")
    for state, count in sorted(by_state.items()):
        print(f"      {state:<20} {count}")
    print(f"  fills            {len(session_runner.fills)}")
    print(f"  trades closed    {len(session_runner.ledger.trades)}")
    print(f"  commission       {fmt(session_runner.ledger.total_costs)}")
    print(f"  slippage         {fmt(session_runner.ledger.total_slippage)}")

    rejected = [o for o in orders if o.reject_reason is not None]
    if rejected:
        reasons = {}
        for order in rejected:
            key = order.reject_reason.value
            reasons[key] = reasons.get(key, 0) + 1
        print("  rejection reasons:")
        for reason, count in sorted(reasons.items()):
            print(f"      {reason:<24} {count}")

    print("\n--- PIPELINE HEALTH " + "-" * 52)
    health = session_runner.health_monitor.evaluate(
        session.last_tick_at or datetime.now(timezone.utc))
    print(f"  overall          {health.overall.value}")
    if health.safe_mode:
        print(f"  ! SAFE MODE: {health.safe_mode_reason}")
    for name, component in sorted(health.components.items()):
        age = component.age_seconds(session.last_tick_at
                                    or datetime.now(timezone.utc))
        print(f"      {name:<16} {component.state.value:<10} "
              f"{'' if age is None else f'{age / 3600:.1f}h ago'}  "
              f"{component.detail}")

    if session_runner.alerts:
        print(f"\n--- ALERTS ({len(session_runner.alerts)}) " + "-" * 48)
        for alert in session_runner.alerts[-8:]:
            print(f"  [{alert.severity.value}] {alert.code}: {alert.message}")

    reconciliation = session_runner.reconciler.reconcile(
        session.session_id, session.last_tick_at or datetime.now(timezone.utc),
        orders, session_runner.fills, session_runner.ledger)
    print("\n--- RECONCILIATION " + "-" * 53)
    print(f"  checks performed {reconciliation.checks_performed}")
    print(f"  clean            {reconciliation.is_clean}")
    for discrepancy in reconciliation.discrepancies:
        print(f"      ! {discrepancy.kind}: {discrepancy.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--account", default="paper-1")
    parser.add_argument("--session", default=None,
                        help="session id; defaults to one derived from the account")
    parser.add_argument("--name", default="paper session")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--universe", default=None)
    parser.add_argument("--universe-limit", type=int, default=10)
    parser.add_argument("--target-weight", type=float, default=0.05)
    parser.add_argument("--commission-bps", type=float, default=2.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--constraints", default="v1")

    parser.add_argument("--ticks", type=int, default=1,
                        help="advance this many ticks")
    parser.add_argument("--replay", action="store_true",
                        help="walk every cached session in the period")
    parser.add_argument("--days", type=int, default=60,
                        help="how far back --replay starts")
    parser.add_argument("--resume", action="store_true",
                        help="restore state from the last checkpoint first")
    parser.add_argument("--checkpoint-every", type=int, default=10)

    parser.add_argument("--synthetic-signals", action="store_true",
                        help="exercise the machinery on generated signals")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume-session", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--emergency-stop", action="store_true")
    parser.add_argument("--status", action="store_true",
                        help="print state without advancing")
    parser.add_argument("--export", default=None,
                        help="write a session export to this path")
    parser.add_argument("--compare-backtest", default=None,
                        help="backtest run_id to compare against")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database does not exist: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_portfolio_schema(conn)
    initialize_paper_schema(conn)
    repository = PaperRepository(conn)

    session_id = args.session or f"sess-{args.account}"
    universe = resolve_universe(conn, args.universe, args.universe_limit)
    if not universe:
        print("ERROR: no instruments with at least 60 cached daily bars.")
        conn.close()
        return 1

    # --- account ---
    account = repository.get_account(args.account)
    if account is None:
        account = PaperAccount(
            account_id=args.account, name=args.name,
            initial_capital=args.capital,
            created_at=datetime.now(timezone.utc))
        repository.save_account(account)
        print(f"Created paper account {args.account} with "
              f"{fmt(args.capital)} {account.base_currency}.")

    # --- session ---
    session = repository.get_session(session_id)
    if session is None:
        config = PaperSessionConfig(
            universe=universe, constraint_set_version=args.constraints,
            sizing_target_weight=args.target_weight,
            commission_bps=args.commission_bps, slippage_bps=args.slippage_bps)
        session = PaperSession(
            session_id=session_id, account_id=account.account_id,
            name=args.name + (" [SYNTHETIC SIGNALS]" if args.synthetic_signals else ""),
            config=config)
        repository.save_session(session)
        print(f"Created paper session {session_id}.")

    # --- clock and period ---
    calendar = MarketCalendar(conn)
    calendar.load(universe)
    bounds = conn.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM price_candle_cache "
        "WHERE interval='1d'").fetchone()
    newest = datetime.fromisoformat(bounds[1])
    start = newest - timedelta(days=args.days)
    anchors = calendar.evaluation_dates(universe, start, newest)

    signals = (build_synthetic_signals(universe, start, newest)
               if args.synthetic_signals else None)

    # --- recovery ---
    ledger = None
    if args.resume:
        ledger, restored_to, method = repository.restore_ledger(
            session_id, account.initial_capital, account.base_currency)
        print(f"Restored ledger via {method}"
              + (f" from checkpoint at {restored_to.isoformat()}"
                 if restored_to else " (no checkpoint; replayed all fills)"))

    clock = (ReplayClock(anchors) if (args.replay and anchors)
             else FixedClock(anchors[-1]) if anchors else SystemClock())
    # Recorded only now, because the session row is written before the
    # period is known. Without this a replay would be stored — and
    # displayed — as if it had run against wall-clock time.
    if not args.dry_run:
        repository.save_session(session, clock_kind=clock.kind)
    runner = PaperTradingSession(conn, account, session, clock=clock,
                                 ledger=ledger, signals=signals)

    if args.resume:
        for order in repository.working_orders(session_id):
            runner.executor._orders[order.order_id] = order
            if order.idempotency_key:
                runner.executor._by_idempotency[order.idempotency_key] = order.order_id

    print("\n=== MarketLens - Phase 13 paper trading ===")
    print("  ! PAPER MODE - no broker, no credentials, no real orders")
    print(f"  account          {account.account_id}")
    print(f"  session          {session_id}")
    print(f"  universe         {len(universe)} instrument(s)")
    print(f"  costs            {args.commission_bps} bps commission, "
          f"{args.slippage_bps} bps slippage")
    print(f"  clock            {clock.kind}")
    if args.synthetic_signals:
        print("  ! SIGNALS ARE SYNTHETIC - exercises the machinery, NOT evidence "
              "about any strategy")

    # --- control actions ---
    now = anchors[-1] if anchors else datetime.now(timezone.utc)
    if args.pause:
        runner.pause(at=now, reason="requested from the command line")
        print("\nSession paused.")
    if args.resume_session:
        runner.resume(at=now, reason="requested from the command line")
        print("\nSession resumed.")
    if args.emergency_stop:
        runner.emergency_stop(at=now, reason="requested from the command line")
        print("\nEMERGENCY STOP engaged — no new orders will be created.")
    if args.stop:
        runner.stop(at=now, reason="requested from the command line")
        print("\nSession stopped.")

    # --- advance ---
    if not args.status and session.accepts_ticks:
        moments = anchors if args.replay else (
            anchors[-args.ticks:] if anchors else [])
        if not moments:
            print("\nNo cached sessions in the period; nothing to tick.")
        else:
            print(f"\n--- ADVANCING {len(moments)} TICK(S) " + "-" * 44)
            for index, moment in enumerate(moments, start=1):
                if isinstance(clock, FixedClock):
                    clock.set(moment)
                result = runner.tick(moment)
                print_tick(result, index, len(moments))

                if not args.dry_run:
                    repository.save_tick(
                        session, result, runner.executor.get_orders(),
                        runner.fills, runner.events,
                        health=runner.health_monitor.evaluate(moment),
                        alerts=runner.alerts,
                        controls=runner.controls.audit_trail(),
                        ledger=runner.ledger)
                    if index % max(1, args.checkpoint_every) == 0:
                        repository.save_checkpoint(session, runner.ledger, moment)

            if not args.dry_run and moments:
                repository.save_checkpoint(session, runner.ledger, moments[-1])

    print_status(runner, repository)

    # --- comparison ---
    if args.compare_backtest:
        snapshots = repository.snapshots_for(session_id)
        orders = repository.orders_for(session_id, limit=100_000)
        fills = repository.fills_for(session_id, limit=100_000)
        paper = paper_metrics_from(snapshots, orders, fills,
                                   account.initial_capital)

        from src.data_access.backtest_repository import BacktestRepository
        stored = BacktestRepository(conn).metrics_for(args.compare_backtest)
        values = stored.get("values", {})
        backtest = {
            "total_return": values.get("total_return"),
            "win_rate": values.get("win_rate"),
            "max_drawdown": values.get("max_drawdown"),
            "turnover": values.get("turnover"),
            "avg_holding_days": values.get("average_holding_days"),
            "cost_per_trade": (values.get("total_costs") / values["total_trades"]
                               if values.get("total_trades") else None),
        }
        report = compare(session_id, paper, backtest, args.compare_backtest)
        print("\n--- PAPER vs BACKTEST " + "-" * 50)
        print(f"  conclusive       {report.is_conclusive}")
        for metric in report.measurable:
            print(f"      {metric.metric:<20} backtest {fmt(metric.backtest, 4):>12}"
                  f"   paper {fmt(metric.paper, 4):>12}"
                  f"   {'DRIFT' if metric.has_drifted and metric.is_diagnostic else ''}")
        for note in report.notes:
            print(f"  - {note}")
        for finding in detect_drift(report):
            print(f"  ! {finding['metric']}: {finding['direction']} — "
                  f"{finding['likely_causes']}")

    # --- export ---
    if args.export:
        payload = repository.export_session(session_id)
        with open(args.export, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        print(f"\nExported session to {args.export}")

    if args.dry_run:
        print("\n(dry run - nothing was written)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
