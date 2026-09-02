#!/usr/bin/env python3
"""
scripts/run_execution.py
-----------------------------
The Phase 14 execution CLI.

WHAT THIS DOES
------------------
Exposes the execution service: list brokers and accounts, inspect
capabilities and health, dry-run an order, validate one, submit one to
PAPER, reconcile against a broker, resolve unknown orders, trace an
order end to end, and operate the kill switch.

WHAT IT CANNOT DO
---------------------
Place a real-money order. There is no flag for it, no environment
variable that enables it, and no adapter that could carry it. Passing
`--environment live` is refused by the safety layer before anything
else runs, and `--broker mt5` reaches an adapter that declares no
capabilities and refuses every submission.

WHY THIS RATHER THAN AN HTTP API
------------------------------------
The repository has no web framework, no server and no user accounts —
every phase runs as a batch job. The OPERATIONS an API would expose
live in `src/execution/service.py`; this script is one caller of them,
and the dashboard is another. Adding a server for one phase would be
the parallel architecture the brief forbids.

SAFETY
----------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- Execution tables are separate; `positions` and `portfolios` are
  never written.
- `--dry-run` is the DEFAULT for anything that would create an order.
- Idempotency keys make a repeated invocation safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import MarketCalendar
from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from src.domain.broker_models import (
    Broker, BrokerAccount, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionPermission,
)
from src.execution.adapters.disabled_gateway import planned_gateways
from src.execution.adapters.paper_gateway import PaperBrokerGateway
from src.execution.instruments import InstrumentRegistry, default_equity_mapping
from src.execution.orchestrator import (
    BrokerRegistry, ExecutionOrchestrator, IntentRequest,
)
from src.execution.safety import ExecutionSafety
from src.execution.service import Caller, ExecutionService, PermissionDenied
from src.paper.executor import PaperExecutor

DEFAULT_DB = os.path.join("data", "marketlens.db")
RULE = "-" * 70


def line(title: str) -> None:
    print(f"\n--- {title} {RULE[:max(0, 66 - len(title))]}")


def fmt(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


# ============================================================
# Wiring
# ============================================================

def universe_from(conn: sqlite3.Connection, limit: int) -> List[str]:
    """The instruments with the most cached history, as Phase 12 does."""
    rows = conn.execute("""
        SELECT instrument_id, COUNT(*) AS bars
        FROM price_candle_cache WHERE interval = '1d'
        GROUP BY instrument_id ORDER BY bars DESC LIMIT ?
    """, (limit,)).fetchall()
    return [r[0] for r in rows]


def ticker_for(conn: sqlite3.Connection, instrument_id: str) -> str:
    row = conn.execute("SELECT ticker FROM instruments WHERE instrument_id = ?",
                       (instrument_id,)).fetchone()
    return row[0] if row else instrument_id


def build(conn: sqlite3.Connection, args) -> ExecutionService:
    """Assemble the registry, the orchestrator and the service."""
    universe = universe_from(conn, args.universe_limit)
    calendar = MarketCalendar(conn)
    calendar.load(universe)

    instruments = InstrumentRegistry(conn)
    instruments.load()
    for instrument_id in universe:
        if instruments.get("paper", instrument_id) is None:
            instruments.register(default_equity_mapping(
                instrument_id, "paper", ticker_for(conn, instrument_id)))

    ledger = PortfolioLedger(args.capital, run_id="execution-cli")
    executor = PaperExecutor(
        calendar, ledger,
        CostModel(version="cost-v1", commission_bps=args.commission_bps),
        SlippageModel(version="slip-v1", method=SlippageMethod.FIXED_BPS,
                      base_bps=args.slippage_bps),
        account_id=args.account, session_id="execution-cli",
        max_participation=0.10)

    gateway = PaperBrokerGateway(executor, calendar, instruments,
                                 account_id=args.account, broker_id="paper")
    gateway.connect()
    gateway.set_market_context(available_cash=args.capital)

    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id="paper", name="Paper (simulated)",
               environment=ExecutionEnvironment.PAPER,
               adapter="paper-gateway-v1",
               created_at=datetime.now(timezone.utc)),
        gateway,
        [BrokerAccount(account_id=args.account, broker_id="paper",
                       name="Paper account",
                       environment=ExecutionEnvironment.PAPER,
                       created_at=datetime.now(timezone.utc))])

    # The venues this project intends to support, listed truthfully
    # rather than hidden: a reader seeing MetaTrader 5 here learns it
    # is planned and absent.
    for broker_id, planned in planned_gateways().items():
        registry.register(
            Broker(broker_id=broker_id, name=planned.name,
                   environment=planned.environment, adapter="none",
                   implemented=False, enabled=False,
                   created_at=datetime.now(timezone.utc)),
            planned)

    safety = ExecutionSafety(actor=args.actor)
    orchestrator = ExecutionOrchestrator(registry, instruments, safety,
                                         actor=args.actor)
    repository = ExecutionRepository(conn)

    # Persist what we registered, so the dashboard can read it.
    if not args.dry_run:
        for entry in registry.all():
            repository.save_broker(entry.broker)
            repository.save_capability(entry.gateway.get_capabilities(),
                                       at=datetime.now(timezone.utc))
            for account in entry.accounts.values():
                repository.save_account(account)
        repository.save_mappings(instruments.for_broker("paper"))

    service = ExecutionService(orchestrator, repository)

    # Recovery, every time (spec §60). A fresh process has an empty
    # idempotency index, so without this a re-run would build a second
    # order carrying a key the database already holds — and find out
    # only when the unique constraint refused the write, after the
    # order had already been sent to the venue.
    recovery = repository.restore(orchestrator)
    if recovery["in_flight"]:
        # Submitted with no outcome recorded. The venue may hold an
        # order nothing local knows the fate of, so it becomes UNKNOWN
        # and waits for reconciliation rather than being assumed either
        # way.
        orchestrator.mark_in_flight_unknown(
            datetime.now(timezone.utc),
            reason="process restarted while the submission was in flight")

    # The paper venue is in-process and forgets on restart, unlike a
    # real broker. Restoring its book keeps reconciliation from
    # reporting every recovered order as one the venue never saw.
    recovery["venue_orders"] = gateway.restore_orders(
        list(orchestrator.orders.values()))

    service._cli_context = {
        "calendar": calendar, "gateway": gateway, "ledger": ledger,
        "universe": universe, "executor": executor, "recovery": recovery,
    }
    return service


def caller_for(args) -> Caller:
    """
    Permissions come from the flags, and the live one cannot be had.

    `--allow-paper` is required to submit anything: the default caller
    can look but not trade, which is the right default for a CLI that
    might be run to answer a question.
    """
    permissions = list(Caller.read_only(args.actor).permissions)
    permissions.append(ExecutionPermission.DRY_RUN_EXECUTION)
    if args.allow_paper:
        permissions.append(ExecutionPermission.PAPER_EXECUTION)
    return Caller(name=args.actor, permissions=tuple(permissions))


# ============================================================
# Commands
# ============================================================

def show_safety(service: ExecutionService, caller: Caller) -> None:
    state = service.safety_state(caller)
    line("EXECUTION SAFETY")
    print(f"  real-money orders     {'ENABLED' if state['allow_real_orders'] else 'IMPOSSIBLE'}")
    if state["real_orders_env_set"]:
        print("  NOTE: MARKETLENS_ALLOW_REAL_ORDERS is set in the environment.")
        print("        It changes nothing. No live adapter exists in this phase.")
    print(f"  execution enabled     {state['execution_enabled']}")
    print(f"  paper execution       {state['paper_execution_enabled']}")
    print(f"  demo execution        {state['demo_execution_enabled']} (no adapter)")
    print(f"  emergency stop        {state['emergency_stop']}"
          + (f" — {state['emergency_stop_reason']}" if state["emergency_stop"] else ""))
    print("  environments:")
    for name, info in state["environments"].items():
        flag = "implemented" if info["implemented"] else "NOT implemented"
        money = " [REAL MONEY — refused]" if info["real_money"] else ""
        print(f"      {name:<12} {flag}{money}")


def show_brokers(service: ExecutionService, caller: Caller,
                 now: datetime) -> None:
    line("BROKERS")
    for record in service.list_brokers(caller):
        status = "tradable" if record["can_trade"] else "not tradable"
        print(f"  {record['broker_id']:<8} {record['name']:<24} "
              f"{record['environment']:<10} {status}")
        print(f"      adapter {record['adapter']} | connection "
              f"{record['connection']} | accounts {', '.join(record['accounts']) or '—'}")
        if record["notes"]:
            print(f"      {record['notes']}")
        capability = service.capabilities(caller, record["broker_id"])
        if capability:
            types = capability.as_dict()["order_types"]
            print(f"      order types: {', '.join(types) if types else 'none'}")


def show_accounts(service: ExecutionService, caller: Caller,
                  now: datetime) -> None:
    line("ACCOUNTS")
    for record in service.list_accounts(caller):
        print(f"  {record['account_id']:<16} {record['broker_id']:<8} "
              f"{record['environment']:<8} "
              f"{'enabled' if record['enabled'] else 'disabled'}")
        snapshot = service.get_account(caller, record["broker_id"],
                                       record["account_id"], now)
        if snapshot:
            print(f"      cash {fmt(snapshot.cash)} | equity "
                  f"{fmt(snapshot.equity)} | buying power "
                  f"{fmt(snapshot.buying_power)}")
        positions = service.positions(caller, record["broker_id"],
                                      record["account_id"], now)
        for position in positions:
            print(f"      {position.instrument_id:<12} {position.quantity:>10,.4f} "
                  f"@ {fmt(position.average_price)} ({position.side.value})")


def show_orders(service: ExecutionService, caller: Caller) -> None:
    orders = service.orders(caller)
    line(f"ORDERS ({len(orders)})")
    if not orders:
        print("  none")
        return
    for order in orders[:30]:
        print(f"  {order.order_id:<24} {order.instrument_id:<12} "
              f"{order.side.value:<4} {order.quantity:>10,.4f} "
              f"{order.state.value}")
        print(f"      broker {order.broker_id} | client {order.client_order_id} "
              f"| broker_order {order.broker_order_id or '—'}")
        if order.reject_code:
            print(f"      rejected: {order.reject_code.value} "
                  f"{order.reject_detail}")


def do_dry_run(service: ExecutionService, caller: Caller, args,
               now: datetime) -> None:
    request = build_request(service, args, now)
    result = service.dry_run(caller, request)
    print()
    print(result.render())


def build_request(service: ExecutionService, args,
                  now: datetime) -> IntentRequest:
    context = service._cli_context
    instrument_id = args.instrument or (context["universe"][0]
                                        if context["universe"] else "")
    reference = None
    bar = context["calendar"].bar_at_or_before(instrument_id, now)
    if bar is not None:
        reference = bar.close

    return IntentRequest(
        intent_id=args.intent_id, broker_id=args.broker,
        account_id=args.account, instrument_id=instrument_id,
        side=CanonicalOrderSide(args.side), quantity=args.quantity, now=now,
        order_type=CanonicalOrderType(args.order_type) if args.order_type else None,
        time_in_force=CanonicalTimeInForce(args.time_in_force),
        limit_price=args.limit_price, stop_price=args.stop_price,
        policy=args.policy, reference_price=reference,
        decision_price=reference, intent_version=args.intent_version,
        strategy_id=args.strategy, portfolio_id=args.portfolio,
        signal_id=args.signal,
        # A CLI order carries no risk verdict of its own, and "not
        # consulted" is deliberately not approval — so an unattended
        # submit is refused rather than waved through.
        risk_approved=True if args.assume_risk_approved else None,
        risk_detail=("assumed approved via --assume-risk-approved"
                     if args.assume_risk_approved else
                     "no risk engine verdict was supplied to this CLI"))


def do_submit(service: ExecutionService, caller: Caller, args,
              now: datetime) -> None:
    request = build_request(service, args, now)
    try:
        result = service.submit(caller, request)
    except PermissionDenied as denied:
        line("SUBMIT REFUSED")
        print(f"  {denied}")
        print("  (pass --allow-paper to grant paper execution)")
        return

    line("SUBMIT")
    print(f"  accepted   {result.accepted}")
    print(f"  reason     {result.explanation}")
    if result.duplicate_of:
        print(f"  duplicate of {result.duplicate_of} — no second order was created")
    if result.order:
        order = result.order
        print(f"  order      {order.order_id} ({order.state.value})")
        print(f"  broker id  {order.broker_order_id or '—'}")
        if args.fill:
            context = service._cli_context
            gateway = context["gateway"]
            gateway.set_market_context(available_cash=context["ledger"].cash)
            # The next SESSION, from the calendar — not now + 1 day.
            # A calendar day need not be a trading day, and filling
            # against a bar that does not exist is exactly the
            # synthetic-bar behaviour Phase 12 refuses.
            later = [b.timestamp for b in context["calendar"].sessions_between(
                order.instrument_id, now, now + timedelta(days=10))
                if b.timestamp > now]
            if not later:
                print("  fills      0 (no session after the order; nothing to "
                      "fill against)")
                fills = []
            else:
                fills = gateway.try_fill(order, later[0])
                service.orchestrator.record_fills(fills)
                print(f"  fills      {len(fills)} at {later[0].date()}")
            for fill in fills:
                print(f"      {fill.quantity:,.4f} @ {fmt(fill.price)} "
                      f"(ref {fmt(fill.reference_price)}, "
                      f"commission {fmt(fill.commission)})")


def do_reconcile(service: ExecutionService, caller: Caller, args,
                 now: datetime) -> None:
    context = service._cli_context
    ledger = context["ledger"]
    positions = {p.instrument_id: p.quantity for p in ledger.open_positions()}
    record = service.reconcile(caller, args.broker, args.account, now,
                               internal_positions=positions,
                               internal_cash=ledger.cash)
    line("RECONCILIATION")
    if record is None:
        print("  no such broker")
        return
    print(f"  checks     {record.checks_performed}")
    print(f"  orders     {record.orders_compared}")
    print(f"  positions  {record.positions_compared}")
    print(f"  clean      {record.is_clean}")
    for mismatch in record.mismatches:
        print(f"      [{mismatch.kind.value}] {mismatch.detail}")


def do_resolve(service: ExecutionService, caller: Caller, args,
               now: datetime) -> None:
    line("UNKNOWN ORDER RESOLUTION")
    print("  Querying the broker. Nothing is resubmitted — a timed-out")
    print("  order may already exist at the venue.")
    try:
        resolutions = service.resolve_unknown(caller, args.broker, now)
    except PermissionDenied as denied:
        print(f"  refused: {denied}")
        return
    if not resolutions:
        print("  no unknown orders")
        return
    for resolution in resolutions:
        mark = "resolved" if resolution["resolved"] else "STILL UNKNOWN"
        print(f"  {resolution['order_id']}: {mark} -> {resolution['state']}")
        print(f"      {resolution['detail']}")


def do_trace(service: ExecutionService, caller: Caller, order_id: str) -> None:
    trace = service.trace(caller, order_id)
    line("EXECUTION TRACE")
    if not trace:
        print(f"  no order {order_id}")
        return
    for key in ("correlation_id", "model_version", "prediction_id", "signal_id",
                "strategy_id", "portfolio_id", "decision_id", "intent_id",
                "order_id", "client_order_id", "broker_order_id", "broker_id",
                "account_id", "environment", "execution_policy", "state"):
        print(f"  {key:<18} {trace.get(key) or '—'}")
    print("  state history:")
    for step in trace["states"]:
        print(f"      {step['seq']}. {step['from'] or 'start'} -> {step['to']}"
              f"  ({step['reason']})")
    print(f"  fills: {len(trace['fills'])}")
    for fill in trace["fills"]:
        print(f"      {fill['quantity']:,.4f} @ {fmt(fill['price'])}")
    if trace.get("slippage_bps") is not None:
        print(f"  slippage: {trace['slippage_bps']:.2f} bps against "
              f"{fmt(trace['decision_price'])}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--actor", default="cli-operator")
    parser.add_argument("--broker", default="paper")
    parser.add_argument("--account", default="paper-account")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--universe-limit", type=int, default=10)
    parser.add_argument("--commission-bps", type=float, default=2.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)

    parser.add_argument("--status", action="store_true",
                        help="brokers, accounts, safety (the default)")
    parser.add_argument("--orders", action="store_true")
    parser.add_argument("--dry-run-order", action="store_true",
                        help="validate an order and stop before submission")
    parser.add_argument("--submit", action="store_true",
                        help="place an order (paper only; needs --allow-paper)")
    parser.add_argument("--fill", action="store_true",
                        help="after submitting, ask the paper venue to fill")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--resolve-unknown", action="store_true")
    parser.add_argument("--trace", metavar="ORDER_ID")
    parser.add_argument("--kill-switch", choices=("on", "off"))
    parser.add_argument("--reason", default="operator request")

    parser.add_argument("--instrument")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--quantity", type=float, default=10.0)
    parser.add_argument("--order-type",
                        choices=("market", "limit", "stop", "stop_limit"))
    parser.add_argument("--time-in-force", choices=("day", "gtc", "ioc", "fok"),
                        default="day")
    parser.add_argument("--limit-price", type=float)
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--policy", default="market")
    parser.add_argument("--intent-id", default="cli-intent-1")
    parser.add_argument("--intent-version", type=int, default=1)
    parser.add_argument("--strategy", default="cli")
    parser.add_argument("--portfolio", default="cli")
    parser.add_argument("--signal")

    parser.add_argument("--allow-paper", action="store_true",
                        help="grant this caller paper execution")
    parser.add_argument("--assume-risk-approved", action="store_true",
                        help="state that risk approved; without it a submit "
                             "is refused, because 'not consulted' is not "
                             "approval")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="evaluate at this moment instead of now. The "
                             "cached bars end well before today, so wall "
                             "time reports every market closed — which is "
                             "correct, and unhelpful for exercising the "
                             "path. Defaults to the last cached session.")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not write anything to the database")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_execution_schema(conn)

    service = build(conn, args)
    caller = caller_for(args)

    context = service._cli_context
    instrument_id = args.instrument or (context["universe"][0]
                                        if context["universe"] else "")
    if args.as_of:
        now = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)
    else:
        # The SECOND-to-last session OF THE INSTRUMENT BEING TRADED.
        # Not the last: a fill needs a bar strictly after the order, so
        # anchoring on the final bar leaves nothing to fill against and
        # reports zero fills for a reason unrelated to the order. And
        # not the newest across the universe: instruments end on
        # different days, and another instrument's later bar says
        # nothing about this one.
        bars = context["calendar"].bars(instrument_id) if instrument_id else []
        if len(bars) >= 2:
            now = bars[-2].timestamp
        elif bars:
            now = bars[-1].timestamp
        else:
            now = datetime.now(timezone.utc)

    print("=" * 70)
    print("MarketLens — Phase 14 execution")
    print("NO REAL-MONEY EXECUTION EXISTS IN THIS PHASE.")
    print(f"Evaluating at {now.isoformat()}"
          + ("" if args.as_of else " (last fillable cached session)"))
    recovery = service._cli_context["recovery"]
    if recovery["orders"]:
        print(f"Recovered {recovery['orders']} order(s), "
              f"{recovery['fills']} fill(s), "
              f"{recovery['event_keys']} event key(s) from the database.")
        if recovery["in_flight"]:
            print(f"  {recovery['in_flight']} order(s) were in flight and are "
                  f"now UNKNOWN, awaiting reconciliation. None was resubmitted.")
    print("=" * 70)

    if args.kill_switch == "on":
        service.activate_kill_switch(caller, args.reason, now)
        print(f"\nEmergency stop ACTIVE: {args.reason}")
    elif args.kill_switch == "off":
        service.release_kill_switch(caller, args.reason, now)
        print(f"\nEmergency stop released: {args.reason}")

    acted = False
    if args.dry_run_order:
        do_dry_run(service, caller, args, now); acted = True
    if args.submit:
        do_submit(service, caller, args, now); acted = True
    if args.reconcile:
        do_reconcile(service, caller, args, now); acted = True
    if args.resolve_unknown:
        do_resolve(service, caller, args, now); acted = True
    if args.trace:
        do_trace(service, caller, args.trace); acted = True
    if args.orders:
        show_orders(service, caller); acted = True

    if args.status or not acted:
        show_safety(service, caller)
        show_brokers(service, caller, now)
        show_accounts(service, caller, now)

    if not args.dry_run:
        service.persist_all()

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
