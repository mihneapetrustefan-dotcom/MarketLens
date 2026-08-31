"""
Runs a Phase 12 backtest and records the result.

WHAT IT REPLAYS
-------------------
The whole decision chain at every historical moment: signals live at T,
the real Phase 11 risk engine over the simulated book, allocation,
simulated execution against cached bars, portfolio update. It does not
re-derive signals — it replays the ones already stored, reconstructing
their historical status from timestamps rather than from today's
lifecycle state.

WHAT IT WILL REPORT ON THIS DATABASE
----------------------------------------
Every signal currently stored is SUPPRESSED (low confidence, stale
prediction, small-sample evidence). A run over them produces zero
orders and says so, with a NO_SIGNALS warning. That is the correct
result for the data, not a failure of the engine — and inventing
signals to make the output look busier would defeat the purpose of
building it.

Use `--synthetic-signals` to exercise the machinery end to end on
generated signals. Runs launched that way are labelled in their name
and must never be read as evidence about the strategy.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- Source tables are read-only inputs; the simulated book lives in
  memory and never touches `positions` or `portfolios`.
- --dry-run computes without writing.
- Re-running the same configuration is idempotent by run id.
- No broker, no order submission, no network.
"""

from __future__ import annotations

import argparse
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

from src.backtest.engine import BacktestEngine
from src.backtest.robustness import RobustnessHarness, bootstrap_trades
from src.data_access.backtest_repository import BacktestRepository
from src.data_access.backtest_schema import initialize_backtest_schema
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.domain.backtest_models import (
    BacktestConfiguration, CostModel, ExecutionAssumptions, ExecutionTiming,
    SlippageMethod, SlippageModel,
)

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")


def fmt(value, digits=2, suffix=""):
    return "n/a" if value is None else f"{value:,.{digits}f}{suffix}"


def fmt_pct(value, digits=2):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def build_synthetic_signals(conn: sqlite3.Connection, universe: List[str],
                            start: datetime, end: datetime, every_days: int = 25):
    """
    Generated signals for exercising the machinery.

    Deliberately crude and clearly labelled. They demonstrate that the
    replay, risk integration, execution and accounting all work; they
    say nothing whatsoever about whether any strategy is any good.
    """
    from src.domain.signal_models import (
        AgreementState, Signal, SignalContext, SignalDirection, SignalProvenance,
        SignalStatus, SignalType,
    )
    signals = []
    span = (end - start).days
    for position, instrument_id in enumerate(universe):
        offset = 15 + position
        while offset < span - 10:
            cutoff = start + timedelta(days=offset)
            signals.append(Signal(
                signal_id=f"synthetic-{instrument_id}-{offset}",
                instrument_id=instrument_id,
                signal_type=SignalType.DIRECTIONAL,
                direction=SignalDirection.LONG,
                status=SignalStatus.ACTIVE, strength=0.6, confidence=0.75,
                agreement_state=AgreementState.AGREEMENT,
                context=SignalContext(event_type="synthetic",
                                      data_quality_level="high"),
                provenance=SignalProvenance(strategy_id="synthetic",
                                            strategy_version="v0",
                                            source_information_cutoff=cutoff),
                created_at=cutoff, valid_from=cutoff,
                valid_until=cutoff + timedelta(days=20)))
            offset += every_days
    return signals


def resolve_universe(conn: sqlite3.Connection, requested: Optional[str],
                     limit: int) -> List[str]:
    if requested:
        return [i.strip() for i in requested.split(",") if i.strip()]
    rows = conn.execute("""
        SELECT instrument_id, COUNT(*) n FROM price_candle_cache
        WHERE interval = '1d' AND instrument_id NOT LIKE 'benchmark%'
        GROUP BY instrument_id HAVING n >= 60 ORDER BY n DESC, instrument_id
        LIMIT ?
    """, (limit,)).fetchall()
    return [r[0] for r in rows]


def print_result(result, quality, engine) -> None:
    metrics = result.metrics
    stats = result.execution_stats

    print("\n--- RUN " + "-" * 62)
    print(f"  run id           {result.run_id}")
    print(f"  status           {result.status.value.upper()}")
    print(f"  period           {result.configuration.start.date()} .. "
          f"{result.configuration.end.date()}")
    print(f"  observations     {result.observations_processed}")
    print(f"  duration         {fmt(result.duration_seconds)}s")

    print("\n--- REPRODUCIBILITY " + "-" * 50)
    identity = result.identity
    for label, value in (
        ("config fingerprint", identity.config_fingerprint),
        ("risk engine", identity.risk_engine_version),
        ("constraints", identity.constraint_set_version),
        ("execution model", identity.execution_model_version),
        ("cost model", identity.cost_model_version),
        ("slippage model", identity.slippage_model_version),
        ("calendar", identity.calendar_version),
        ("code", identity.code_version),
    ):
        print(f"  {label:20s} {value}")

    print("\n--- PERFORMANCE " + "-" * 54)
    print(f"  total return     {fmt_pct(metrics.total_return)}")
    print(f"  CAGR             {fmt_pct(metrics.cagr)}")
    print(f"  volatility       {fmt_pct(metrics.volatility)}")
    print(f"  Sharpe           {fmt(metrics.sharpe)}")
    print(f"  Sortino          {fmt(metrics.sortino)}")
    print(f"  Calmar           {fmt(metrics.calmar)}")
    print(f"  max drawdown     {fmt_pct(metrics.max_drawdown)}")
    print(f"  benchmark        {fmt_pct(metrics.benchmark_return)}")
    print(f"  excess           {fmt_pct(metrics.excess_return)}")

    print("\n--- TRADING " + "-" * 58)
    print(f"  trades           {metrics.total_trades} "
          f"({metrics.winning_trades}W / {metrics.losing_trades}L)")
    print(f"  win rate         {fmt_pct(metrics.win_rate)}")
    print(f"  profit factor    {fmt(metrics.profit_factor)}")
    print(f"  expectancy       {fmt(metrics.expectancy)}")
    print(f"  avg holding      {fmt(metrics.average_holding_days, 1)} days")
    print(f"  turnover         {fmt(metrics.turnover)}x "
          f"(annualized {fmt(metrics.annualized_turnover)}x)")
    print(f"  costs            {fmt(metrics.total_costs)} commission, "
          f"{fmt(metrics.total_slippage)} slippage")

    print("\n--- EXECUTION " + "-" * 56)
    print(f"  orders           {stats.orders_created} created, "
          f"{stats.orders_filled} filled, {stats.orders_rejected} rejected")
    print(f"  fill rate        {fmt_pct(stats.fill_rate)}")
    print(f"  avg slippage     {fmt(stats.average_slippage_bps, 2)} bps")
    print(f"  avg fill delay   {fmt(stats.average_fill_delay_days, 2)} days")
    if stats.reject_reasons:
        for reason, count in sorted(stats.reject_reasons.items()):
            print(f"      ! {reason}: {count}")

    print("\n--- RISK " + "-" * 61)
    for state, count in sorted(result.risk_decision_counts.items()):
        print(f"  {state:22s} {count}")
    print(f"  rejected allocations  {len(result.rejected_allocations)}")
    print(f"  modified allocations  {len(result.modified_allocations)}")

    if metrics.unavailable:
        print("\n--- COULD NOT BE MEASURED " + "-" * 44)
        for metric, reason in sorted(metrics.unavailable.items()):
            print(f"  - {metric}: {reason}")

    if result.warnings:
        print("\n--- RESEARCH QUALITY WARNINGS " + "-" * 40)
        for warning in result.warnings:
            print(f"  ! {warning.code.value}: {warning.message}")
            if warning.detail:
                print(f"      {warning.detail}")

    if quality is not None:
        print("\n--- RESEARCH QUALITY SCORE " + "-" * 43)
        print(f"  {fmt(quality.score, 3)} ({quality.band})")
        print(f"  NOTE: {quality.MEANING}")
        for name, value in sorted(quality.factors.items()):
            print(f"      {name:26s} {value:.2f}")
        for note in quality.notes:
            print(f"      - {note}")

    if result.errors:
        print("\n--- ERRORS " + "-" * 59)
        for error in result.errors:
            print(f"  {'FATAL' if error.fatal else 'error'}: "
                  f"{error.code} - {error.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--name", default="backtest")
    parser.add_argument("--start", default=None, help="ISO-8601 UTC")
    parser.add_argument("--end", default=None, help="ISO-8601 UTC")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--universe", default=None,
                        help="comma-separated instrument ids")
    parser.add_argument("--universe-limit", type=int, default=15)
    parser.add_argument("--benchmark", default="benchmark-spy")
    parser.add_argument("--commission-bps", type=float, default=2.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--timing", default="next_bar_open",
                        choices=[t.value for t in ExecutionTiming])
    parser.add_argument("--rebalance-days", type=int, default=5)
    parser.add_argument("--target-weight", type=float, default=0.05)
    parser.add_argument("--constraints", default="v1")
    parser.add_argument("--risk-free", type=float, default=0.0)
    parser.add_argument("--synthetic-signals", action="store_true",
                        help="exercise the machinery on generated signals")
    parser.add_argument("--cost-sensitivity", action="store_true",
                        help="also run 0/5/10/20 bps commission scenarios")
    parser.add_argument("--bootstrap", action="store_true",
                        help="resample closed trades (needs 30+ trades)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database does not exist: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_portfolio_schema(conn)
    initialize_backtest_schema(conn)

    universe = resolve_universe(conn, args.universe, args.universe_limit)
    if not universe:
        print("ERROR: no instruments with at least 60 cached daily bars.")
        conn.close()
        return 1

    # Default the period to what the cache actually covers, rather than
    # to a range that would silently produce an empty run.
    bounds = conn.execute("""
        SELECT MIN(timestamp), MAX(timestamp) FROM price_candle_cache
        WHERE interval = '1d'
    """).fetchone()
    start = (datetime.fromisoformat(args.start) if args.start
             else datetime.fromisoformat(bounds[0]))
    end = (datetime.fromisoformat(args.end) if args.end
           else datetime.fromisoformat(bounds[1]))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    name = args.name + (" [SYNTHETIC SIGNALS]" if args.synthetic_signals else "")
    config = BacktestConfiguration(
        name=name, start=start, end=end, initial_capital=args.capital,
        universe=universe, benchmark_instrument_id=args.benchmark,
        constraint_set_version=args.constraints,
        sizing_target_weight=args.target_weight,
        execution=ExecutionAssumptions(timing=ExecutionTiming(args.timing)),
        costs=CostModel(commission_bps=args.commission_bps),
        slippage=SlippageModel(
            method=(SlippageMethod.NONE if args.slippage_bps == 0
                    else SlippageMethod.FIXED_BPS),
            base_bps=args.slippage_bps),
        rebalance_days=args.rebalance_days, risk_free_rate=args.risk_free,
        risk_free_source=("supplied on the command line" if args.risk_free
                          else "assumed zero — no risk-free series in this database"))

    signals = (build_synthetic_signals(conn, universe, start, end)
               if args.synthetic_signals else None)

    print("\n=== MarketLens - Phase 12 backtest ===")
    print(f"  name             {name}")
    print(f"  period           {start.date()} .. {end.date()}")
    print(f"  universe         {len(universe)} instrument(s)")
    print(f"  benchmark        {args.benchmark}")
    print(f"  execution        {config.execution.describe()}")
    print(f"  costs            {args.commission_bps} bps commission, "
          f"{args.slippage_bps} bps slippage")
    if args.synthetic_signals:
        print("  ! SIGNALS ARE SYNTHETIC - this exercises the machinery and is "
              "NOT evidence about any strategy")

    engine = BacktestEngine(conn, config, signals=signals)
    result = engine.run()
    quality = engine.assess(result)
    print_result(result, quality, engine)

    if args.bootstrap and result.trades:
        boot = bootstrap_trades([t.net_pnl for t in result.trades], seed=0)
        print("\n--- TRADE BOOTSTRAP " + "-" * 50)
        if boot.insufficient_data:
            print(f"  unavailable: {boot.note}")
        else:
            print(f"  mean total P&L   {fmt(boot.mean_total_pnl)}")
            print(f"  5th / 95th       {fmt(boot.percentile_5)} / "
                  f"{fmt(boot.percentile_95)}")
            print(f"  P(loss)          {fmt_pct(boot.probability_of_loss)}")
        print(f"  ASSUMPTION: {boot.assumption}")

    if args.cost_sensitivity:
        print("\n--- COST SENSITIVITY " + "-" * 49)
        harness = RobustnessHarness(
            lambda cfg: BacktestEngine(conn, cfg, signals=signals).run())
        report = harness.cost_sensitivity(config)
        for scenario in report.scenarios:
            print(f"  {scenario.label:>10s}  return {fmt_pct(scenario.total_return)}"
                  f"   trades {scenario.trades}")
        print(f"  spread {fmt_pct(report.spread)} - "
              f"{'FRAGILE (sign flips)' if report.flips_sign else 'sign is stable'}")

    if not args.dry_run:
        repository = BacktestRepository(conn)
        repository.save_result(result, quality)
        print(f"\nStored run {result.run_id}")
    else:
        print("\n(dry run - nothing was written)")

    conn.close()
    return 0 if not result.has_fatal_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
