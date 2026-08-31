"""
Evaluates a portfolio against the Phase 11 risk limits and records the
decision.

WHAT THIS SCRIPT DOES
-------------------------
  1. Creates the Phase 11 tables if they do not exist (idempotent).
  2. Seeds the default constraint set under its version, if absent.
  3. Builds a point-in-time snapshot of the portfolio.
  4. Measures it — volatility, VaR/ES, concentration, correlation,
     drawdown — from the CACHED price history only.
  5. Sizes an allocation proposal from whatever signals are actionable
     at the anchor.
  6. Asks the risk engine for a verdict and stores it.

WHAT IT DOES NOT DO
-----------------------
It does not place an order, contact a broker, or fetch a live price.
Its most action-like output is an inert OrderIntent row. There is no
execution path in this phase, and this script is not one.

WHY IT IS MANUAL, NOT PART OF THE DAILY PIPELINE
----------------------------------------------------
Phase 11 has nothing to act on yet: no portfolio is declared by
default, and every signal in the current database is suppressed. Wiring
this into daily.yml would add a step that runs three times a day to
report an empty portfolio. It becomes a pipeline step when there is a
portfolio to watch — that is a deliberate decision to make then, not a
default to inherit now.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- Every source table is a read-only input.
- --dry-run evaluates and prints without writing.
- Re-running with the same anchor is idempotent: the decision id is
  derived from (engine version, constraint version, portfolio, anchor,
  proposal), so the same evaluation rewrites its own row rather than
  appending a near-duplicate.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.portfolio_repository import PortfolioRepository
from src.data_access.portfolio_schema import initialize_portfolio_schema
from src.domain.portfolio_models import ExposureDimension
from src.portfolio.constraints import DEFAULT_CONSTRAINT_VERSION
from src.portfolio.service import PortfolioService
from src.portfolio.sizing import FixedFractionSizing, VolatilityTargetSizing

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

SIZING_STRATEGIES = {
    "fixed_fraction": FixedFractionSizing,
    "volatility_target": VolatilityTargetSizing,
}


def fmt_money(value, currency="USD"):
    return "n/a" if value is None else f"{value:,.2f} {currency}"


def fmt_pct(value, digits=2):
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def print_snapshot(snapshot) -> None:
    print("\n--- PORTFOLIO STATE " + "-" * 52)
    print(f"  as of            {snapshot.as_of.isoformat()}")
    print(f"  equity           {fmt_money(snapshot.equity, snapshot.base_currency)}")
    print(f"  cash             {fmt_money(snapshot.cash, snapshot.base_currency)}")
    print(f"  gross exposure   {fmt_money(snapshot.gross_exposure)}")
    print(f"  net exposure     {fmt_money(snapshot.net_exposure)}")
    print(f"  long / short     {fmt_money(snapshot.long_exposure)} / "
          f"{fmt_money(snapshot.short_exposure)}")
    leverage = snapshot.leverage
    print(f"  leverage         {'n/a' if leverage is None else f'{leverage:.2f}x'}")
    print(f"  unrealized P&L   {fmt_money(snapshot.unrealized_pnl)}")
    print(f"  positions        {len(snapshot.valuations)} priced, "
          f"{len(snapshot.unvalued_positions)} unpriced")

    if not snapshot.is_complete:
        print("  ! INCOMPLETE - these positions had no price at the anchor:")
        for valuation in snapshot.unvalued_positions:
            print(f"      {valuation.position.instrument_id}")
    if snapshot.has_stale_prices:
        print("  ! STALE - priced from an old candle:")
        for valuation in snapshot.stale_valuations:
            print(f"      {valuation.position.instrument_id} "
                  f"({valuation.price_age_days:.1f} days old)")
    if snapshot.is_multi_currency:
        print(f"  ! MULTI-CURRENCY {sorted(set(snapshot.currencies))} - "
              f"no FX data exists, so totals mix units")


def print_exposures(service, snapshot) -> None:
    print("\n--- EXPOSURE " + "-" * 59)
    for dimension, breakdown in service.exposures.all_breakdowns(snapshot).items():
        if not breakdown.buckets and not breakdown.unclassified_exposure:
            continue
        print(f"  {dimension.value}")
        for bucket in breakdown.buckets[:10]:
            weight = "n/a" if bucket.weight is None else f"{bucket.weight * 100:6.2f}%"
            print(f"      {weight}  {bucket.label}  "
                  f"({bucket.position_count} position(s))")
        if breakdown.unclassified_count:
            print(f"      ! {breakdown.unclassified_count} position(s) "
                  f"unclassified, {fmt_money(breakdown.unclassified_exposure)}")


def print_metrics(metrics) -> None:
    print("\n--- RISK METRICS " + "-" * 55)
    volatility = metrics.volatility
    if volatility.insufficient_data:
        print(f"  volatility       unavailable ({volatility.note})")
    else:
        print(f"  volatility       {fmt_pct(volatility.value)} annualized "
              f"({volatility.observations} obs, {volatility.method})")

    var = metrics.value_at_risk
    if var.insufficient_data:
        print(f"  VaR / ES         unavailable ({var.note})")
    else:
        print(f"  VaR {var.confidence_level:.0%}         {fmt_pct(var.value)} "
              f"of equity, {var.horizon_days}d, {var.method}")
        print(f"  expected shortfall {fmt_pct(var.expected_shortfall)}")

    drawdown = metrics.drawdown
    if drawdown.insufficient_data:
        print("  drawdown         unavailable (needs stored snapshot history)")
    else:
        print(f"  max drawdown     {fmt_pct(drawdown.max_drawdown)}")
        print(f"  current drawdown {fmt_pct(drawdown.current_drawdown)}")

    concentration = metrics.concentration
    hhi = concentration.hhi
    print(f"  HHI              {'n/a' if hhi is None else f'{hhi:.4f}'}")
    if concentration.effective_positions is not None:
        print(f"  effective breadth {concentration.effective_positions:.2f} "
              f"of {concentration.position_count} position(s)")
    if concentration.largest_weight is not None:
        print(f"  largest position {fmt_pct(concentration.largest_weight)} "
              f"({concentration.largest_instrument_id})")

    correlation = metrics.correlation
    if correlation.computed_pairs:
        print(f"  correlation      avg {correlation.average_correlation:.3f} "
              f"over {correlation.computed_pairs} pair(s); "
              f"{correlation.insufficient_pairs} too thin to measure")
        for left, right, value in correlation.highly_correlated_pairs[:5]:
            print(f"      ! {left} / {right} = {value:.2f}")
    elif correlation.insufficient_pairs:
        print(f"  correlation      unavailable "
              f"({correlation.insufficient_pairs} pair(s) too thin)")

    for metric, reason in metrics.unavailable.items():
        print(f"  - {metric}: {reason}")


def print_decision(decision, intents) -> None:
    print("\n--- RISK DECISION " + "-" * 54)
    print(f"  state            {decision.state.value.upper()}")
    print(f"  summary          {decision.summary}")
    print(f"  decision id      {decision.decision_id}")
    print(f"  engine / limits  {decision.provenance.risk_engine_version} / "
          f"{decision.provenance.constraint_set_version}")

    if decision.violations:
        print("\n  violations:")
        for violation in decision.violations:
            marker = ("(remediated)" if violation.remediated
                      else "HARD" if violation.is_hard else "soft")
            print(f"      [{marker}] {violation.message}")
            if violation.current_value is not None:
                print(f"               current {violation.current_value:.4f} -> "
                      f"proposed {violation.observed_value:.4f}, "
                      f"limit {violation.limit_value:.4f}")

    if decision.reasons:
        print("\n  reasoning:")
        for reason in decision.reasons:
            print(f"      - {reason}")

    if decision.skipped_scopes:
        print("\n  checks that could not run:")
        for scope, reason in decision.skipped_scopes.items():
            print(f"      - {scope}: {reason}")

    if decision.evaluated_scopes:
        print(f"\n  checks evaluated: {len(decision.evaluated_scopes)}")

    print(f"\n  order intents    {len(intents)} "
          f"(inert - no execution path exists in this phase)")
    for intent in intents:
        print(f"      {intent.side.upper():4s} {intent.instrument_id} -> "
              f"target {fmt_pct(intent.target_weight)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--portfolio", required=True,
                        help="portfolio_id to evaluate")
    parser.add_argument("--as-of", default=None,
                        help="ISO-8601 UTC anchor; defaults to now")
    parser.add_argument("--constraints", default=DEFAULT_CONSTRAINT_VERSION,
                        help="constraint set version to evaluate against")
    parser.add_argument("--sizing", default="fixed_fraction",
                        choices=sorted(SIZING_STRATEGIES),
                        help="position sizing strategy for the proposal")
    parser.add_argument("--target-weight", type=float, default=0.05,
                        help="target weight per new position (fixed_fraction)")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print without writing anything")
    parser.add_argument("--create", action="store_true",
                        help="create the portfolio row if it does not exist")
    parser.add_argument("--cash", type=float, default=0.0,
                        help="cash balance when creating a portfolio")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database does not exist: {args.db}")
        return 1

    if args.as_of:
        anchor = datetime.fromisoformat(args.as_of)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        anchor = datetime.now(timezone.utc)

    conn = sqlite3.connect(args.db)
    initialize_portfolio_schema(conn)
    repository = PortfolioRepository(conn)

    if repository.get_portfolio(args.portfolio) is None:
        if not args.create:
            print(f"ERROR: portfolio '{args.portfolio}' does not exist. "
                  f"Re-run with --create to declare it.")
            conn.close()
            return 1
        repository.save_portfolio(args.portfolio, args.portfolio, cash=args.cash,
                                  created_at=datetime.now(timezone.utc))
        print(f"Created portfolio '{args.portfolio}' with "
              f"{fmt_money(args.cash)} cash.")

    service = PortfolioService(conn, constraint_version=args.constraints)
    strategy_class = SIZING_STRATEGIES[args.sizing]
    sizing = (strategy_class(args.target_weight)
              if strategy_class is FixedFractionSizing else strategy_class())

    signals = service.actionable_signals(anchor)
    print(f"\n=== MarketLens - Phase 11 portfolio risk evaluation ===")
    print(f"  portfolio        {args.portfolio}")
    print(f"  anchor           {anchor.isoformat()}")
    print(f"  constraints      {args.constraints}")
    print(f"  sizing           {args.sizing}")
    print(f"  actionable signals at the anchor: {len(signals)}")

    result = service.evaluate(
        args.portfolio, anchor, sizing=sizing, signals=signals,
        persist=not args.dry_run)

    print_snapshot(result.snapshot)
    print_exposures(service, result.snapshot)
    print_metrics(result.metrics)

    if result.proposal is not None:
        print(f"\n--- PROPOSAL " + "-" * 59)
        print(f"  {result.proposal.sizing_strategy_id}:"
              f"{result.proposal.sizing_version} "
              f"-> {len(result.proposal.changes)} change(s)")
        for change in result.proposal.changes:
            print(f"      {change.instrument_id}: "
                  f"{fmt_pct(change.current_weight)} -> {fmt_pct(change.target_weight)}"
                  f"   {change.reason}")
        if result.proposal.note:
            print(f"      note: {result.proposal.note}")

    print_decision(result.decision, result.intents)

    if args.dry_run:
        print("\n(dry run - nothing was written)")
    else:
        print(f"\nStored decision {result.decision.decision_id}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
