#!/usr/bin/env python3
"""
scripts/run_trading_loop.py
---------------------------------
The Phase 25 paper-trading operating loop.

WHAT THIS DOES
------------------
Advances the loop by one or more bounded cycles. Each cycle reads the
mode, the broker, the market data and the signals; decides eligibility;
asks the REAL Phase 11 risk engine; converts what it approved through
the REAL Phase 17 intake; submits through the REAL Phase 14
orchestrator to IBKR PAPER; then polls, fills, reconciles, prices and
records — every time, whether or not it traded.

WHAT IT CANNOT DO
---------------------
Place a real-money order. `IBKR_ENVIRONMENT` may only be `paper`, the
config refuses anything else at construction, the Phase 14 safety layer
refuses a live environment before anything runs, and the durable
trading mode has no LIVE value that resolves to permission. Four
independent refusals, none of which can be reached by a flag here.

TWO TRANSPORTS
------------------
`--mock` runs the whole path against the deterministic double — no
gateway, no account, no network. Without it the loop talks to a real
Client Portal Gateway that you must start and log into yourself; see
docs/PHASE_15_IBKR_RUNBOOK.md.

NO CREDENTIAL PASSES THROUGH THIS SCRIPT
--------------------------------------------
There is no username or password argument and none is read. The
gateway holds the session, and this process asks it whether it is
authenticated.

SAFETY
----------
- `--dry-run` is the DEFAULT. Orders are validated and not sent.
- Trading mode must be PAPER and is stored in the database, not here.
- The kill switch is durable: `--kill-switch on` survives the process.
- Idempotency keys and a deterministic cycle anchor make a repeated
  invocation safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.trading_loop_models import (
    DEFAULT_CYCLE_SECONDS, TradingMode, TradingModeRefused, cycle_anchor,
)
from src.trading.api import TradingLoopAPI
from src.trading.eligibility import EligibilityPolicy
from src.trading.loop import LoopConfig, TradingLoop
from src.trading.mode import TradingModeStore
from src.trading.stack import build_stack
from src.trading.validation import PaperValidator

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")


def line(title: str) -> None:
    print("\n" + title)
    print("-" * max(12, len(title)))


def fmt(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


# ============================================================
# Commands
# ============================================================

def show_status(conn: sqlite3.Connection, now: datetime) -> None:
    api = TradingLoopAPI(conn)
    state = api.state(now)

    line("TRADING MODE")
    print(f"  mode                  {state['mode'].upper()}")
    if state.get("mode_reason"):
        print(f"  reason                {state['mode_reason']}")
    print(f"  source                {state.get('mode_source', 'n/a')}")
    print(f"  kill switch           "
          f"{'ACTIVE' if state.get('kill_switch') else 'clear'}"
          + (f" — {state['kill_reason']}" if state.get("kill_reason") else ""))
    print("  live trading          BLOCKED (no code path exists)")

    if not state.get("available"):
        print(f"\n  {state.get('reason', 'the loop has not run yet')}")
        return

    account = state.get("account")
    line("ACCOUNT (as the broker reported it)")
    if account is None:
        print("  no account state has been recorded")
    else:
        print(f"  broker/account        {account['broker_id']} / "
              f"{account['account_id']}")
        print(f"  source                {account['source']}")
        print(f"  equity                {fmt(account['equity'])} "
              f"{account['base_currency']}")
        print(f"  cash                  {fmt(account['cash'])}")
        print(f"  buying power          {fmt(account['buying_power'])}")
        print(f"  open positions        {account['open_positions']}")
        print(f"  connection            {account['connection_state']}")
        print(f"  observed at           {account['observed_at']}")

    positions = state.get("positions") or []
    line(f"POSITIONS ({len(positions)}) — reconciled from the broker")
    for position in positions:
        print(f"  {position['instrument_id']:<24} "
              f"{position['quantity']:>10,.2f} @ "
              f"{fmt(position['average_price'])}  "
              f"unrealised {fmt(position['unrealized_pnl'])}")
    if not positions:
        print("  none")

    conversion = api.conversion()
    line("CONVERSION")
    print(f"  signals seen          {conversion['signals_seen']}")
    print(f"  eligible              {conversion['eligible']}")
    print(f"  orders                {conversion['orders']}")
    print(f"  fills                 {conversion['fills']}")
    print(f"  trade outcomes        {conversion['outcomes']}")

    rejections = api.rejections()
    if rejections:
        line("WHY SIGNALS DID NOT TRADE")
        for row in rejections:
            print(f"  {row['count']:>5}  {row['code']}")
            if row.get("example"):
                print(f"         {row['example'][:96]}")

    cycles = state.get("cycles") or []
    line(f"RECENT CYCLES ({len(cycles)})")
    for cycle in cycles[:10]:
        blocks = ", ".join(b["reason"] for b in cycle.get("blocks") or [])
        print(f"  {cycle['anchor'][:16]}  {cycle['status']:<10} "
              f"{cycle['health']:<9} "
              f"sig {cycle['signals_seen']:>3}/{cycle['signals_eligible']:<3} "
              f"ord {cycle['orders_submitted']:>2} "
              f"fill {cycle['fills_recorded']:>2}"
              + (f"  [{blocks}]" if blocks else ""))


def show_integrity(conn: sqlite3.Connection) -> int:
    api = TradingLoopAPI(conn)
    report = api.integrity_check()
    line("INTEGRITY")
    for check in report["checks"]:
        mark = ("PASS" if check["ok"] is True
                else "FAIL" if check["ok"] is False else "NOT RUN")
        print(f"  [{mark:^7}] {check['name']}")
        print(f"            {check['detail']}")
    print(f"\n  {report['passed']} passed, {report['failed']} failed, "
          f"{report['not_run']} could not run")
    if not report["conclusive"]:
        print("  NOTE: a check that could not run is not a check that passed.")
    return 0 if report["ok"] else 1


def run_cycles(conn: sqlite3.Connection, args, now: datetime) -> int:
    policy = EligibilityPolicy(
        min_confidence=args.min_confidence,
        min_strength=args.min_strength,
        allow_experimental_models=args.experimental,
        **({"max_signal_age_hours": args.max_signal_age_hours}
           if args.max_signal_age_hours is not None else {}))

    stack = build_stack(conn, actor=args.actor, mock=args.mock,
                        account_id=args.account,
                        allow_paper_orders=args.allow_paper_orders,
                        universe_limit=args.universe_limit,
                        persist=not args.dry_run)

    config = LoopConfig(
        session_id=args.session, name=args.name,
        account_id=args.account or stack.account_id, actor=args.actor,
        cycle_seconds=args.cycle_seconds,
        universe_limit=args.universe_limit,
        constraint_version=args.constraints or "",
        strategy_id=args.strategy, strategy_version=args.strategy_version,
        challenger_id=args.challenger,
        allow_paper_orders=args.allow_paper_orders,
        experimental=args.experimental, eligibility=policy,
        dry_run=args.dry_run)

    loop = TradingLoop(conn, stack, config)

    line("GATEWAY")
    print(f"  transport             {stack.transport.name}")
    print(f"  connection            {stack.connect_detail}")
    print(f"  account               {stack.account_id or '(none resolved)'}")
    print(f"  ordering enabled      {stack.config.ordering_enabled}")
    print(f"  may submit            {stack.may_submit}")
    if args.dry_run:
        print("  DRY RUN               orders are validated and NOT sent")

    exit_code = 0
    for index in range(args.cycles):
        # REAL TIME, NOT SIMULATED TIME (Phase 25.8).
        #
        # This read:
        #     moment = now + timedelta(seconds=index * args.cycle_seconds)
        # which ran every cycle immediately while stamping them at
        # now, +15m, +30m, +45m. Three of four cycles claimed to have
        # happened at moments that had not arrived, on real signals
        # writing real rows. Phase 25.5's anchor-drift guard did not
        # catch it: it rejects anchors more than FOUR HOURS out, and a
        # 45-minute forward drift passes.
        #
        # Each cycle now asks the clock. `--cycles N` with no waiting
        # means N cycles at the same anchor, which the idempotency
        # keys correctly collapse into one -- honest, and very
        # different from inventing three futures. Use
        # scripts/run_session.py for a runner that actually waits.
        moment = datetime.now(timezone.utc)
        if index and args.wait_between_cycles:
            target = moment + timedelta(seconds=args.cycle_seconds)
            while datetime.now(timezone.utc) < target:
                time.sleep(min(5.0, (target - datetime.now(timezone.utc))
                               .total_seconds()))
            moment = datetime.now(timezone.utc)
        result = loop.run_cycle(moment, worker=args.worker or args.actor)
        line(f"CYCLE {index + 1}/{args.cycles} — {result.cycle_id}")
        print(f"  anchor                {result.anchor.isoformat()}")
        print(f"  status                {result.status.value.upper()}")
        print(f"  mode / health         {result.mode.value} / "
              f"{result.health.value}")
        print(f"  signals               {result.signals_seen} seen, "
              f"{result.signals_eligible} eligible")
        print(f"  targets               {result.targets_set}")
        print(f"  intents               {result.intents_created} created, "
              f"{result.intents_rejected} rejected")
        print(f"  orders                {result.orders_submitted} submitted, "
              f"{result.orders_rejected} rejected")
        print(f"  fills                 {result.fills_recorded}")
        print(f"  positions             {result.positions_reconciled} "
              f"reconciled, {result.discrepancies} discrepancy/ies")
        print(f"  outcomes              {result.outcomes_recorded}")
        if result.blocks:
            print("  BLOCKED BY:")
            for block in result.blocks:
                print(f"    - {block.reason.value}: {block.detail}")
        if args.verbose:
            print("  stages:")
            for stage in result.stages:
                print(f"    {stage.stage.value:<16} "
                      f"{stage.outcome.value:<8} {stage.detail[:80]}")
            not_reached = result.stages_not_reached()
            if not_reached:
                print(f"    not reached: {', '.join(not_reached)}")
        out_of_order = result.timestamps.out_of_order()
        if out_of_order:
            print(f"  CLOCK PROBLEM: {'; '.join(out_of_order)}")
            exit_code = 1

    return exit_code


def set_mode(conn: sqlite3.Connection, args, now: datetime) -> int:
    store = TradingModeStore(conn)
    try:
        mode = TradingMode(args.set_mode)
    except ValueError:
        print(f"{args.set_mode!r} is not a trading mode.")
        return 2
    try:
        resolution = store.set_mode(mode, actor=args.actor,
                                    reason=args.reason, at=now)
    except TradingModeRefused as error:
        print(f"REFUSED: {error}")
        return 1
    print(f"trading mode is now {resolution.mode.value.upper()}"
          + (f" — {resolution.reason}" if resolution.reason else ""))
    return 0


def operate_kill_switch(conn: sqlite3.Connection, args, now: datetime) -> int:
    store = TradingModeStore(conn)
    if args.kill_switch == "on":
        resolution = store.activate_kill_switch(
            actor=args.actor, reason=args.reason, at=now)
        print("KILL SWITCH ACTIVE. No new orders will be created.")
        print("Working orders are NOT cancelled and positions are NOT closed —")
        print("what to do about existing exposure is a human decision.")
    else:
        resolution = store.release_kill_switch(
            actor=args.actor, reason=args.reason, at=now)
        print("kill switch released")
    print(f"effective mode: {resolution.mode.value.upper()}")
    return 0


def review_validation(conn: sqlite3.Connection, args, now: datetime) -> int:
    from src.domain.trading_loop_models import PaperStrategyState, PromotionRefused
    validator = PaperValidator(conn)
    try:
        target = PaperStrategyState(args.review_state)
    except ValueError:
        print(f"{args.review_state!r} is not a paper state.")
        return 2
    try:
        review_id = validator.review(
            args.review, to_state=target, reviewer=args.reviewer,
            reason=args.reason, at=now)
    except (PromotionRefused, ValueError) as error:
        print(f"REFUSED: {error}")
        return 1
    print(f"recorded review {review_id}: {args.review} -> {target.value}")
    print(f"reviewer {args.reviewer}: {args.reason}")
    return 0


# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 25 — the IBKR paper-trading operating loop.")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--actor", default="loop-operator")
    parser.add_argument("--worker", default="",
                        help="worker identity for the cycle claim")

    parser.add_argument("--status", action="store_true",
                        help="show mode, account, positions and conversion")
    parser.add_argument("--integrity", action="store_true")
    parser.add_argument("--cycles", type=int, default=0,
                        help="advance the loop this many cycles")

    parser.add_argument("--session", default="sess-paper-loop-1")
    parser.add_argument("--name", default="paper loop")
    parser.add_argument("--account", help="override IBKR_ACCOUNT_ID")
    parser.add_argument("--cycle-seconds", type=int,
                        default=DEFAULT_CYCLE_SECONDS)
    parser.add_argument("--universe-limit", type=int, default=25)
    parser.add_argument("--constraints", help="risk constraint set version")
    parser.add_argument("--strategy")
    parser.add_argument("--strategy-version")
    parser.add_argument("--challenger",
                        help="a Phase 24 challenger id; must be PAPER_CANDIDATE")

    parser.add_argument("--mock", action="store_true",
                        help="run against the deterministic IBKR double")
    parser.add_argument("--allow-paper-orders", action="store_true",
                        help="permit submission (still PAPER only)")
    parser.add_argument("--experimental", action="store_true",
                        help="allow signals from models nobody promoted; the "
                             "run is labelled experimental on every row")
    parser.add_argument("--max-signal-age-hours", type=float, default=None,
                        help="how old the INFORMATION behind a signal may be. "
                             "On this pipeline a signal's cutoff is already "
                             "~39h old when the signal is created, so the "
                             "default 48h leaves a tradeable window of about "
                             "nine hours. Set it knowingly; it is recorded in "
                             "the session fingerprint.")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--min-strength", type=float, default=0.0)

    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="actually submit to IBKR PAPER")
    parser.add_argument("--wait-between-cycles", action="store_true",
                        help=("actually wait cycle_seconds between "
                              "cycles instead of running them back to "
                              "back at the same anchor"))
    parser.add_argument("--verbose", action="store_true")

    parser.add_argument("--set-mode", choices=("off", "paper", "live"),
                        help="record the durable trading mode ('live' is refused)")
    parser.add_argument("--kill-switch", choices=("on", "off"))
    parser.add_argument("--reason", default="",
                        help="required for a mode change, the kill switch or a review")

    parser.add_argument("--review", metavar="VALIDATION_ID")
    parser.add_argument("--review-state", default="human_review")
    parser.add_argument("--reviewer", default="")

    args = parser.parse_args()
    now = datetime.now(timezone.utc)

    if not os.path.exists(args.db):
        print(f"No database at {args.db}")
        return 2

    conn = sqlite3.connect(args.db)
    try:
        if args.set_mode:
            if not args.reason:
                print("--reason is required to change the trading mode.")
                return 2
            return set_mode(conn, args, now)

        if args.kill_switch:
            if not args.reason:
                print("--reason is required to operate the kill switch.")
                return 2
            return operate_kill_switch(conn, args, now)

        if args.review:
            if not args.reviewer or not args.reason:
                print("--reviewer and --reason are both required for a review.")
                return 2
            return review_validation(conn, args, now)

        if args.integrity:
            return show_integrity(conn)

        if args.cycles > 0:
            code = run_cycles(conn, args, now)
            show_status(conn, now)
            return code

        show_status(conn, now)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
