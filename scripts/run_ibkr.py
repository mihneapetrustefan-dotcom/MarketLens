#!/usr/bin/env python3
"""
scripts/run_ibkr.py
------------------------
The Interactive Brokers operator CLI (Phase 15, §44, §45, §46, §69,
§70).

WHAT IT DOES
----------------
Everything an operator needs to bring the IBKR paper integration up and
check it: connection and session status, account discovery, balances,
positions, contract resolution, market data, dry runs, a controlled
paper order, reconciliation, unknown-order resolution and the full
trace of any order.

TWO TRANSPORTS
------------------
`--mock` runs the whole path against the deterministic double, with no
gateway and no account. That is how the integration is demonstrable on
a machine that has neither, and how the adversarial behaviour is shown
without a real venue.

Without `--mock` it talks to a real Client Portal Gateway, which you
must start and log into yourself. See docs/PHASE_15_IBKR_RUNBOOK.md.

WHAT IT CANNOT DO
---------------------
Place a real-money order. `IBKR_ENVIRONMENT` may only be `paper`, the
config refuses anything else at construction, and Phase 14's safety
layer refuses a live environment before any of this runs.

Nor can it place a PAPER order merely because a connection exists:
`IBKR_PAPER_ORDERING_ENABLED` is a separate flag, off by default, and
`--submit` without it reports the refusal rather than trading.

NO CREDENTIAL PASSES THROUGH THIS SCRIPT
--------------------------------------------
There is no username or password argument, and none is read. The
gateway holds the session.
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

from src.backtest.calendar import MarketCalendar
from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.broker_models import (
    Broker, BrokerAccount, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionPermission,
)
from src.execution.adapters.ibkr.config import (
    IBKRConfig, IBKRConfigurationError,
)
from src.execution.adapters.ibkr.contracts import conid_of
from src.execution.adapters.ibkr.errors import IBKRError, explain as explain_error
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import (
    MOCK_ACCOUNT, MockIBKRTransport,
)
from src.execution.adapters.ibkr.transport import ClientPortalTransport
from src.execution.instruments import InstrumentRegistry
from src.execution.orchestrator import (
    BrokerRegistry, ExecutionOrchestrator, IntentRequest,
)
from src.execution.safety import ExecutionSafety
from src.execution.service import Caller, ExecutionService, PermissionDenied

DEFAULT_DB = os.path.join("data", "marketlens.db")
RULE = "-" * 70


def line(title: str) -> None:
    print(f"\n--- {title} {RULE[:max(0, 66 - len(title))]}")


def fmt(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def build(conn: sqlite3.Connection, args) -> Dict[str, Any]:
    """Assemble the IBKR stack behind the Phase 14 orchestrator."""
    overrides: Dict[str, Any] = {}
    if args.mock:
        # The mock needs the integration on to be demonstrable, and
        # ordering is still gated separately by --allow-paper-orders.
        overrides["enabled"] = True
        overrides["account_id"] = args.account or MOCK_ACCOUNT
    if args.account:
        overrides["account_id"] = args.account
    if args.allow_paper_orders:
        overrides["ordering_enabled"] = True

    config = IBKRConfig.from_environment(**overrides)

    transport = (MockIBKRTransport(config, account_id=config.account_id
                                   or MOCK_ACCOUNT)
                 if args.mock else ClientPortalTransport(config))

    instruments = InstrumentRegistry(conn)
    instruments.load()

    calendar = MarketCalendar(conn)
    universe = [r[0] for r in conn.execute("""
        SELECT instrument_id FROM price_candle_cache WHERE interval='1d'
        GROUP BY instrument_id ORDER BY COUNT(*) DESC LIMIT 25
    """)]
    if universe:
        calendar.load(universe)

    gateway = IBKRGateway(config, transport, instruments, calendar=calendar)
    # Establish the session here rather than only in --status. Every
    # command below needs it, and a disconnected gateway would
    # otherwise fail validation for a reason that has nothing to do
    # with the order being examined.
    gateway.connect()

    registry = BrokerRegistry()
    account_id = config.account_id or MOCK_ACCOUNT
    registry.register(
        Broker(broker_id="ibkr",
               name="Interactive Brokers (paper)",
               environment=ExecutionEnvironment.PAPER,
               adapter=f"ibkr-gateway-v1/{transport.name}",
               enabled=config.enabled,
               created_at=datetime.now(timezone.utc)),
        gateway,
        [BrokerAccount(account_id=account_id, broker_id="ibkr",
                       name="IBKR paper account",
                       environment=ExecutionEnvironment.PAPER,
                       created_at=datetime.now(timezone.utc))])

    safety = ExecutionSafety(actor=args.actor)
    orchestrator = ExecutionOrchestrator(registry, instruments, safety,
                                         actor=args.actor)
    repository = ExecutionRepository(conn)

    # Recovery, every time. A fresh process has an empty idempotency
    # index, and without this a re-run would build a second order
    # carrying a key the database already holds.
    recovery = repository.restore(orchestrator, broker_id="ibkr")
    known = [o for o in orchestrator.orders.values() if o.broker_id == "ibkr"]
    gateway.restore_known_orders(known)
    gateway.restore_seen_executions(
        [f.execution_id for f in repository.fills_for(broker_id="ibkr")
         if f.execution_id])
    if recovery["in_flight"]:
        orchestrator.mark_in_flight_unknown(
            datetime.now(timezone.utc),
            reason="process restarted while the IBKR submission was in flight")

    if not args.dry_run:
        repository.save_broker(registry.get("ibkr").broker)
        repository.save_account(
            registry.get("ibkr").accounts[account_id])
        repository.save_capability(gateway.get_capabilities(),
                                   at=datetime.now(timezone.utc))

    return {
        "config": config, "transport": transport, "gateway": gateway,
        "instruments": instruments, "orchestrator": orchestrator,
        "repository": repository, "calendar": calendar,
        "service": ExecutionService(orchestrator, repository),
        "account_id": account_id, "recovery": recovery,
    }


def caller_for(args) -> Caller:
    permissions = list(Caller.read_only(args.actor).permissions)
    permissions.append(ExecutionPermission.DRY_RUN_EXECUTION)
    if args.allow_paper_orders:
        permissions.append(ExecutionPermission.PAPER_EXECUTION)
    return Caller(name=args.actor, permissions=tuple(permissions))


# ============================================================
# Commands
# ============================================================

def show_status(stack: Dict[str, Any], now: datetime) -> None:
    config: IBKRConfig = stack["config"]
    gateway: IBKRGateway = stack["gateway"]

    line("IBKR CONFIGURATION")
    for key, value in config.describe().items():
        print(f"  {key:<24} {value}")

    line("SESSION")
    state = gateway.connect()
    detail = gateway.health_detail(now)
    print(f"  connection         {state.value}")
    print(f"  transport          {detail['transport']}")
    print(f"  authenticated      {detail['authenticated']}")
    print(f"  gateway connected  {detail['gateway_connected']}")
    print(f"  competing session  {detail['competing_session']}")
    print(f"  reconnects         {detail['reconnects']}")
    if not detail["authenticated"]:
        print()
        print("  The Client Portal Gateway is not authenticated.")
        print("  Start it and log in with a browser; this application")
        print("  never sees your credential. See the runbook.")

    line("SAFETY")
    print(f"  environment            {config.environment.value}")
    print(f"  real-money execution   IMPOSSIBLE (no adapter exists)")
    print(f"  IBKR_ENABLED           {config.enabled}")
    print(f"  paper ordering gate    "
          f"{'OPEN' if config.ordering_enabled else 'CLOSED'}")
    print(f"  orders may be sent     {config.can_submit_orders}")


def show_account(stack: Dict[str, Any], now: datetime) -> None:
    gateway: IBKRGateway = stack["gateway"]
    account_id = stack["account_id"]

    line("ACCOUNTS")
    try:
        discovered = gateway.discover_accounts()
        print(f"  discovered: {', '.join(discovered) or 'none'}")
        for found in discovered:
            marker = "PAPER" if found.upper().startswith("DU") else "NOT A DU ACCOUNT"
            print(f"      {found:<14} {marker}")
        if any(not f.upper().startswith("DU") for f in discovered):
            print()
            print("  WARNING: an account id not beginning with DU is not an")
            print("  IBKR paper account. Do not enable ordering against it.")
    except IBKRError as error:
        print(f"  could not discover accounts: {explain_error(error)}")
        return

    line("BALANCES")
    try:
        snapshot = gateway.get_account(account_id, now)
    except IBKRError as error:
        print(f"  {explain_error(error)}")
        return
    print(f"  currency           {snapshot.base_currency}")
    print(f"  cash               {fmt(snapshot.cash)}")
    print(f"  equity             {fmt(snapshot.equity)}")
    print(f"  available funds    {fmt(snapshot.available_funds)}")
    print(f"  buying power       {fmt(snapshot.buying_power)}")
    print(f"  initial margin     {fmt(snapshot.margin.initial_margin)}")
    print(f"  maintenance margin {fmt(snapshot.margin.maintenance_margin)}")
    print(f"  realized P&L       {fmt(snapshot.realized_pnl)}")
    print(f"  unrealized P&L     {fmt(snapshot.unrealized_pnl)}")

    line("POSITIONS")
    positions = gateway.get_positions(account_id, now)
    if not positions:
        print("  none")
    for position in positions:
        print(f"  {position.instrument_id or position.broker_symbol:<16} "
              f"{position.quantity:>12,.4f} @ {fmt(position.average_price)} "
              f"({position.side.value})")


def do_resolve(stack: Dict[str, Any], args) -> None:
    gateway: IBKRGateway = stack["gateway"]
    line("CONTRACT RESOLUTION")
    resolution = gateway.resolve_contract(
        args.instrument or f"i-{args.symbol.lower()}",
        args.symbol, sec_type=args.sec_type, currency=args.currency,
        primary_exchange=args.exchange, persist=not args.dry_run)
    print(f"  {resolution.explain()}")
    if resolution.ok and not args.dry_run:
        mapping = resolution.contract.as_mapping(
            args.instrument or f"i-{args.symbol.lower()}")
        stack["repository"].save_mapping(mapping)
        print(f"  saved: {mapping.canonical_instrument_id} -> "
              f"{mapping.broker_symbol} (conid {conid_of(mapping)})")
    elif resolution.ambiguous:
        print("\n  Nothing was saved and nothing will trade until this is")
        print("  narrowed. Re-run with --exchange or --currency.")


def do_quote(stack: Dict[str, Any], args, now: datetime) -> None:
    gateway: IBKRGateway = stack["gateway"]
    instrument = args.instrument or f"i-{(args.symbol or '').lower()}"
    line("MARKET DATA")
    quote = gateway.quote(instrument, now)
    if quote is None:
        print(f"  {instrument} has no resolved IBKR contract.")
        return
    print(f"  availability   {quote.availability.value}"
          f"{'' if quote.availability.is_tradeable else '  (NOT tradeable)'}")
    print(f"  last / bid / ask   {fmt(quote.last)} / {fmt(quote.bid)} / "
          f"{fmt(quote.ask)}")
    print(f"  mid            {fmt(quote.mid)}")
    print(f"  broker time    {quote.broker_at.isoformat() if quote.broker_at else 'not reported'}")
    print(f"  received       {quote.received_at.isoformat() if quote.received_at else '-'}")
    print(f"  fresh enough to trade on: {quote.is_fresh(now)}")


def build_request(stack: Dict[str, Any], args, now: datetime) -> IntentRequest:
    instrument = args.instrument or f"i-{(args.symbol or '').lower()}"
    quote = None
    try:
        quote = stack["gateway"].quote(instrument, now)
    except IBKRError:
        pass
    reference = quote.reference_price if quote else None

    return IntentRequest(
        intent_id=args.intent_id, broker_id="ibkr",
        account_id=stack["account_id"], instrument_id=instrument,
        side=CanonicalOrderSide(args.side), quantity=args.quantity, now=now,
        order_type=CanonicalOrderType(args.order_type) if args.order_type else None,
        time_in_force=CanonicalTimeInForce(args.time_in_force),
        limit_price=args.limit_price, stop_price=args.stop_price,
        policy=args.policy, reference_price=reference,
        decision_price=reference, intent_version=args.intent_version,
        strategy_id=args.strategy, portfolio_id=args.portfolio,
        # A CLI order carries no risk verdict of its own, and "not
        # consulted" is deliberately not approval.
        risk_approved=True if args.assume_risk_approved else None,
        risk_detail=("assumed approved via --assume-risk-approved"
                     if args.assume_risk_approved
                     else "no risk engine verdict was supplied to this CLI"))


def do_dry_run(stack: Dict[str, Any], caller: Caller, args,
               now: datetime) -> None:
    result = stack["service"].dry_run(caller, build_request(stack, args, now))
    print()
    print(result.render())


def do_submit(stack: Dict[str, Any], caller: Caller, args,
              now: datetime) -> None:
    line("PAPER ORDER")
    config: IBKRConfig = stack["config"]
    if not config.can_submit_orders:
        print("  REFUSED before anything was sent.")
        print(f"      IBKR_ENABLED                 {config.enabled}")
        print(f"      IBKR_PAPER_ORDERING_ENABLED  {config.ordering_enabled}")
        print(f"      environment                  {config.environment.value}")
        print()
        print("  Confirm in the IBKR portal that the account above is a")
        print("  PAPER account, then pass --allow-paper-orders.")
        return

    try:
        result = stack["service"].submit(caller, build_request(stack, args, now))
    except PermissionDenied as denied:
        print(f"  {denied}")
        return

    print(f"  accepted     {result.accepted}")
    print(f"  reason       {result.explanation}")
    if result.duplicate_of:
        print(f"  duplicate of {result.duplicate_of} — nothing new was sent")
    if result.order:
        print(f"  order        {result.order.order_id} "
              f"({result.order.state.value})")
        print(f"  ibkr order   {result.order.broker_order_id or 'not returned'}")
        print(f"  client id    {result.order.client_order_id}")
        if result.order.state.needs_reconciliation:
            print()
            print("  The outcome is UNKNOWN. IBKR may hold this order.")
            print("  Run --resolve-unknown. Do NOT resubmit.")


def do_reconcile(stack: Dict[str, Any], caller: Caller, now: datetime) -> None:
    line("RECONCILIATION")
    positions = {p.instrument_id: p.quantity
                 for p in stack["gateway"].get_positions(stack["account_id"], now)
                 if p.instrument_id}
    record = stack["service"].reconcile(
        caller, "ibkr", stack["account_id"], now,
        internal_positions=positions)
    if record is None:
        print("  no such broker")
        return
    print(f"  checks     {record.checks_performed}")
    print(f"  orders     {record.orders_compared}")
    print(f"  positions  {record.positions_compared}")
    print(f"  clean      {record.is_clean}")
    for mismatch in record.mismatches:
        print(f"      [{mismatch.kind.value}] {mismatch.detail}")


def do_resolve_unknown(stack: Dict[str, Any], caller: Caller,
                       now: datetime) -> None:
    line("UNKNOWN ORDER RESOLUTION")
    print("  Querying IBKR. Nothing is resubmitted — a timed-out order")
    print("  may already exist at the venue.")
    try:
        resolutions = stack["service"].resolve_unknown(caller, "ibkr", now)
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


def do_trace(stack: Dict[str, Any], caller: Caller, order_id: str) -> None:
    line("EXECUTION TRACE")
    trace = stack["service"].trace(caller, order_id)
    if not trace:
        print(f"  no order {order_id}")
        return
    for key in ("correlation_id", "signal_id", "strategy_id", "portfolio_id",
                "decision_id", "intent_id", "order_id", "client_order_id",
                "broker_order_id", "broker_id", "account_id", "environment",
                "state"):
        print(f"  {key:<18} {trace.get(key) or '—'}")
    print("  state history:")
    for step in trace["states"]:
        print(f"      {step['seq']}. {step['from'] or 'start'} -> "
              f"{step['to']}  ({step['reason']})")
    print(f"  fills: {len(trace['fills'])}")
    for fill in trace["fills"]:
        print(f"      {fill['quantity']:,.4f} @ {fmt(fill['price'])}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--actor", default="ibkr-operator")
    parser.add_argument("--account", help="override IBKR_ACCOUNT_ID")
    parser.add_argument("--mock", action="store_true",
                        help="run against the deterministic double: no "
                             "gateway, no account, no network")

    parser.add_argument("--status", action="store_true")
    parser.add_argument("--account-info", action="store_true")
    parser.add_argument("--resolve", action="store_true",
                        help="resolve a symbol to an IBKR contract")
    parser.add_argument("--quote", action="store_true")
    parser.add_argument("--dry-run-order", action="store_true")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--resolve-unknown", action="store_true")
    parser.add_argument("--trace", metavar="ORDER_ID")

    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--instrument")
    parser.add_argument("--sec-type", default="STK")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--exchange", help="primary exchange, to disambiguate")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--quantity", type=float, default=1.0,
                        help="keep test orders small")
    parser.add_argument("--order-type",
                        choices=("market", "limit", "stop", "stop_limit"))
    parser.add_argument("--time-in-force", choices=("day", "gtc", "ioc"),
                        default="day")
    parser.add_argument("--limit-price", type=float)
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--policy", default="market")
    parser.add_argument("--intent-id", default="ibkr-cli-1")
    parser.add_argument("--intent-version", type=int, default=1)
    parser.add_argument("--strategy", default="cli")
    parser.add_argument("--portfolio", default="cli")

    parser.add_argument("--allow-paper-orders", action="store_true",
                        help="open the paper ordering gate for this run")
    parser.add_argument(
        "--assume-risk-approved", action="store_true",
        help="OPERATOR OVERRIDE for a hand-typed order: assert a risk "
             "verdict this CLI did not obtain. The real path is "
             "src/execution/intake.from_decision(), which takes the "
             "verdict from an actual Phase 11 RiskDecision and cannot "
             "be told to assume one.")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="evaluate the market session at this moment. "
                             "The cached bars end before today, so wall "
                             "time reports every market closed — correct, "
                             "and unhelpful for exercising the path.")
    parser.add_argument("--dry-run", action="store_true",
                        help="write nothing to the database")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_execution_schema(conn)

    try:
        stack = build(conn, args)
    except IBKRConfigurationError as error:
        print("=" * 70)
        print("IBKR CONFIGURATION REFUSED")
        print("=" * 70)
        print(f"  {error}")
        conn.close()
        return 2

    caller = caller_for(args)
    if args.as_of:
        now = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    print("=" * 70)
    print("MarketLens — Phase 15 Interactive Brokers")
    print(f"Transport: {stack['transport'].name}"
          + ("  (MOCK — no gateway, no account, no network)"
             if args.mock else ""))
    print("PAPER ONLY. NO REAL-MONEY EXECUTION EXISTS IN THIS PHASE.")
    if args.as_of:
        print(f"Market session evaluated at {now.isoformat()}")
    if stack["recovery"]["orders"]:
        print(f"Recovered {stack['recovery']['orders']} order(s) from the "
              f"database; {stack['recovery']['in_flight']} were in flight.")
    print("=" * 70)

    acted = False
    if args.resolve:
        do_resolve(stack, args); acted = True
    if args.quote:
        do_quote(stack, args, now); acted = True
    if args.dry_run_order:
        do_dry_run(stack, caller, args, now); acted = True
    if args.submit:
        do_submit(stack, caller, args, now); acted = True
    if args.reconcile:
        do_reconcile(stack, caller, now); acted = True
    if args.resolve_unknown:
        do_resolve_unknown(stack, caller, now); acted = True
    if args.trace:
        do_trace(stack, caller, args.trace); acted = True
    if args.account_info:
        show_account(stack, now); acted = True

    if args.status or not acted:
        show_status(stack, now)

    if not args.dry_run:
        stack["service"].persist_all()

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
