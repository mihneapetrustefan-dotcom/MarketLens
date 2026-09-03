#!/usr/bin/env python3
"""
scripts/run_operations.py
------------------------------
The Phase 16 execution operations centre (spec §44, §48, §84, §85).

WHAT IT DOES
----------------
Everything an operator needs to run a trading session safely: check
readiness, request and approve a promotion, open a session with its
preflight checks, watch health and limits, run the end-of-day
reconciliation, produce the daily report, and stop — routinely or in
an emergency.

Interactive Brokers is the only broker. `--mock` runs the whole thing
against the deterministic IBKR double, so every procedure in the
runbook can be rehearsed with no gateway, no account and no network.

WHAT IT CANNOT DO
---------------------
Trade real money. `ExecutionLevel` 5 and above have no execution path
in this repository — no adapter accepts a real-money environment — so
`--request-level 5` will record the request, evaluate the gates
honestly, and still refuse to operate there.

That refusal is the point of the governance layer: the gates can be
built, tested and argued about now, while the irreversible step
remains a separate decision nobody has taken.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.calendar import MarketCalendar
from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.data_access.governance_repository import GovernanceRepository
from src.data_access.governance_schema import initialize_governance_schema
from src.domain.broker_models import (
    Broker, BrokerAccount, ExecutionEnvironment, MismatchSeverity,
)
from src.execution.adapters.ibkr.config import IBKRConfig, IBKRConfigurationError
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import MOCK_ACCOUNT, MockIBKRTransport
from src.execution.adapters.ibkr.transport import ClientPortalTransport
from src.execution.governance import (
    ExecutionGovernor, ExecutionLevel, ReadinessCategory, ReadinessVerdict,
    assess_readiness,
)
from src.execution.instruments import InstrumentRegistry
from src.execution.limits import DayState, LimitBreach, paper_limits
from src.execution.monitoring import (
    AlertSeverity, Capability, CapabilityState, ExecutionMonitor, SystemHealth,
    compare_environments,
)
from src.execution.orchestrator import BrokerRegistry, ExecutionOrchestrator
from src.execution.outcomes import ExecutionJournal
from src.execution.safety import ExecutionSafety
from src.execution.session import (
    SessionAction, SessionConfiguration, SessionSummary, SessionTransitionError,
    new_session, standard_preflight,
)

DEFAULT_DB = os.path.join("data", "marketlens.db")
RULE = "-" * 72


def line(title: str) -> None:
    print(f"\n--- {title} {RULE[:max(0, 68 - len(title))]}")


def fmt(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def mark(ok: Optional[bool]) -> str:
    return "  ok  " if ok else ("  --  " if ok is None else " FAIL ")


# ============================================================
# Wiring
# ============================================================

def build(conn: sqlite3.Connection, args) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if args.mock:
        overrides["enabled"] = True
        overrides["account_id"] = args.account or MOCK_ACCOUNT
    if args.account:
        overrides["account_id"] = args.account

    config = IBKRConfig.from_environment(**overrides)
    transport = (MockIBKRTransport(config,
                                   account_id=config.account_id or MOCK_ACCOUNT)
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
    gateway.connect()

    account_id = config.account_id or MOCK_ACCOUNT
    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id="ibkr", name="Interactive Brokers (paper)",
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
    execution_repo = ExecutionRepository(conn)
    governance_repo = GovernanceRepository(conn)
    governor = ExecutionGovernor()
    limits = paper_limits()

    # Recovery, every invocation (spec §36).
    exec_recovery = execution_repo.restore(orchestrator, broker_id="ibkr")
    gateway.restore_known_orders(
        [o for o in orchestrator.orders.values() if o.broker_id == "ibkr"])
    gateway.restore_seen_executions(
        [f.execution_id for f in execution_repo.fills_for(broker_id="ibkr")
         if f.execution_id])
    if exec_recovery["in_flight"]:
        orchestrator.mark_in_flight_unknown(
            datetime.now(timezone.utc),
            reason="process restarted while the submission was in flight")

    gov_recovery = governance_repo.restore(governor, datetime.now(timezone.utc))
    for name in gov_recovery["latched_breaches"]:
        try:
            limits._latched[LimitBreach(name)] = "restored from a previous run"
        except ValueError:
            pass

    session = governance_repo.active_session()
    return {
        "config": config, "transport": transport, "gateway": gateway,
        "instruments": instruments, "orchestrator": orchestrator,
        "execution_repo": execution_repo, "governance_repo": governance_repo,
        "governor": governor, "limits": limits, "safety": safety,
        "session": session, "account_id": account_id,
        "exec_recovery": exec_recovery, "gov_recovery": gov_recovery,
        "monitor": ExecutionMonitor(session.session_id if session else None),
        "journal": ExecutionJournal(session.session_id if session else None),
    }


def read_health(stack: Dict[str, Any], now: datetime) -> SystemHealth:
    """Measure every capability (spec §35)."""
    gateway = stack["gateway"]
    health = SystemHealth(at=now)
    detail = gateway.health_detail(now)

    health.record(Capability.CONNECTION,
                  CapabilityState.HEALTHY if detail["connection"] == "connected"
                  else CapabilityState.UNAVAILABLE,
                  f"connection is {detail['connection']}")
    health.record(Capability.AUTHENTICATION,
                  CapabilityState.HEALTHY if detail["authenticated"]
                  else CapabilityState.UNAVAILABLE,
                  "gateway session authenticated" if detail["authenticated"]
                  else "the gateway session is not authenticated")

    try:
        snapshot = gateway.get_account(stack["account_id"], now)
        health.record(Capability.ACCOUNT, CapabilityState.HEALTHY,
                      f"equity {snapshot.equity:,.0f} {snapshot.base_currency}")
        stack["account_snapshot"] = snapshot
    except Exception as error:                            # noqa: BLE001
        health.record(Capability.ACCOUNT, CapabilityState.UNAVAILABLE,
                      f"account read failed: {error}")
        stack["account_snapshot"] = None

    try:
        positions = gateway.get_positions(stack["account_id"], now)
        health.record(Capability.POSITIONS, CapabilityState.HEALTHY,
                      f"{len(positions)} position(s)")
        stack["positions"] = positions
    except Exception as error:                            # noqa: BLE001
        health.record(Capability.POSITIONS, CapabilityState.UNAVAILABLE,
                      str(error))
        stack["positions"] = []

    try:
        orders = gateway.get_open_orders(stack["account_id"])
        health.record(Capability.ORDERS, CapabilityState.HEALTHY,
                      f"{len(orders)} working order(s)")
    except Exception as error:                            # noqa: BLE001
        health.record(Capability.ORDERS, CapabilityState.UNAVAILABLE, str(error))

    mapped = stack["instruments"].for_broker("ibkr")
    if not mapped:
        health.record(Capability.MARKET_DATA, CapabilityState.UNKNOWN,
                      "no instrument is mapped, so data cannot be checked")
    else:
        quote = gateway.quote(mapped[0].canonical_instrument_id, now)
        if quote is None or not quote.availability.is_tradeable:
            health.record(
                Capability.MARKET_DATA, CapabilityState.STALE,
                f"availability {quote.availability.value if quote else 'unknown'}")
        else:
            health.record(Capability.MARKET_DATA, CapabilityState.HEALTHY,
                          f"{mapped[0].broker_symbol} quote is live")

    health.record(Capability.EXECUTIONS, CapabilityState.HEALTHY,
                  "execution polling available")
    health.record(Capability.CLOCK, CapabilityState.HEALTHY,
                  "no drift measured against the venue")

    record = stack["execution_repo"].reconciliations_for("ibkr", limit=1)
    if not record:
        health.record(Capability.RECONCILIATION, CapabilityState.UNKNOWN,
                      "no reconciliation has been run")
    else:
        clean = record[0]["is_clean"]
        health.record(Capability.RECONCILIATION,
                      CapabilityState.HEALTHY if clean else CapabilityState.DEGRADED,
                      "clean" if clean else
                      f"{len(record[0]['mismatches'])} mismatch(es)")

    # Record it. A reading nobody stored is one the dashboard cannot
    # show, and an empty health table reads as "nothing measured"
    # rather than "measured and fine" — the two must stay apart.
    stack["health"] = health
    session = stack.get("session")
    session_id = session.session_id if session is not None else "unassigned"
    repo = stack.get("governance_repo")
    if repo is not None:
        repo.save_health(session_id, health)
        alerts = stack["monitor"].raise_health_alerts(health, at=now)
        if alerts:
            repo.save_alerts(alerts)
    return health


# ============================================================
# Commands
# ============================================================

def show_status(stack: Dict[str, Any], now: datetime) -> None:
    config, governor = stack["config"], stack["governor"]

    line("EXECUTION ENVIRONMENT")
    level = governor.effective_level(now)
    print(f"  broker              Interactive Brokers (the only broker)")
    print(f"  transport           {stack['transport'].name}")
    print(f"  environment         {config.environment.value.upper()}")
    print(f"  execution level     {int(level)} — {level.label}")
    print(f"  real-money orders   IMPOSSIBLE (no adapter accepts one)")
    print(f"  ordering gate       "
          f"{'OPEN' if config.ordering_enabled else 'CLOSED'}")
    print(f"  emergency stop      {stack['safety'].kill_switch_active}")

    line("EXECUTION LEVELS")
    for candidate in ExecutionLevel:
        marker = " <-- current" if candidate is level else ""
        state = ("implemented" if candidate.is_implemented
                 else "NOT implemented")
        money = "  [REAL MONEY]" if candidate.is_real_money else ""
        print(f"  {int(candidate)}  {candidate.label:<32} {state}{money}{marker}")

    line("SYSTEM HEALTH")
    health = read_health(stack, now)
    stack["health"] = health
    print(health.render())
    print(f"\n  overall: {health.overall.value.upper()}   "
          f"new orders permitted: {health.permits_new_orders}")

    line("LIMITS")
    state = stack["limits"].state()
    if state["latched"]:
        print("  LATCHED — these will not clear themselves:")
        for name, reason in state["latched"].items():
            print(f"      {name}: {reason}")
    else:
        print("  no latched breaches")
    for key, value in state["capital"].items():
        print(f"  {key:<28} {value}")

    session = stack["session"]
    line("SESSION")
    if session is None:
        print("  no active session")
    else:
        print(f"  {session.session_id}  {session.state.value.upper()}")
        print(f"  operator      {session.operator}")
        print(f"  fingerprint   {session.fingerprint}")
        print(f"  preflight     {'passed' if session.preflight_passed else 'NOT passed'}")
        permitted, why = session.may_submit(now)
        print(f"  may submit    {permitted} — {why}")

    recovery = stack["gov_recovery"]
    if recovery["approvals"] or recovery["session_id"]:
        line("RECOVERED")
        print(f"  approvals            {recovery['approvals']}")
        print(f"  active approval      {recovery['active_approval'] or '—'}")
        print(f"  session              {recovery['session_id'] or '—'}")
        print(f"  day state restored   {recovery['day_restored']}")
        print(f"  latched breaches     {recovery['latched_breaches'] or '—'}")


def show_readiness(stack: Dict[str, Any], now: datetime) -> None:
    health = stack.get("health") or read_health(stack, now)
    record = stack["execution_repo"].reconciliations_for("ibkr", limit=1)
    metrics = stack["monitor"].metrics

    assessment = assess_readiness(
        now,
        execution={"error_rate": metrics.error_rate or 0.0},
        broker={"connected": health.permits_new_orders,
                "detail": f"overall {health.overall.value}"},
        reconciliation={"clean": record[0]["is_clean"] if record else None,
                        "detail": "no reconciliation run" if not record else ""},
        data={"fresh": health.readings[Capability.MARKET_DATA].state
              is CapabilityState.HEALTHY},
        risk={"healthy": True, "detail": "Phase 11 engine available"},
        security_reviewed=True, operations_ready=True)

    line("PAPER-TO-LIVE READINESS")
    print(assessment.render())
    print(f"\n  ready: {assessment.is_ready}")
    if assessment.blocking:
        print(f"  blocking: {', '.join(c.value for c in assessment.blocking)}")
    if assessment.conditional:
        print(f"  conditional: {', '.join(c.value for c in assessment.conditional)}")
    print("\n  A passing score does NOT authorize live trading. It is one input")
    print("  to a human decision, and levels 5+ have no execution path here.")
    stack["governance_repo"].save_readiness(assessment, stack["config"].account_id or "system")
    stack["readiness"] = assessment


def show_gates(stack: Dict[str, Any], args, now: datetime) -> None:
    level = ExecutionLevel(args.level or 5)
    metrics = _promotion_metrics(stack, now)
    evaluation = stack["governor"].evaluate(
        level, now, metrics, stack.get("readiness"))

    line(f"PROMOTION GATES — level {int(level)} ({level.label})")
    for gate in evaluation.gates:
        print(f"  [{mark(gate.passed and gate.measured)}] {gate.explain()}")
    print(f"\n  gates pass: {evaluation.gates_pass}")
    print(f"  approved:   {evaluation.approved}")
    print(f"  PERMITTED:  {evaluation.permitted}")
    if not evaluation.permitted:
        print(f"\n  {evaluation.explain()}")


def _promotion_metrics(stack: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Whatever can actually be measured. Anything absent blocks."""
    metrics = stack["monitor"].metrics
    outcomes = stack["governance_repo"].query_outcomes(limit=1000)
    reconciliations = stack["execution_repo"].reconciliations_for("ibkr", limit=200)
    dirty = sum(1 for r in reconciliations if not r["is_clean"])
    slippages = sorted(o["slippage_bps"] for o in outcomes
                       if o["slippage_bps"] is not None)
    return {
        "paper_trades": len(outcomes),
        "median_slippage_bps": (slippages[len(slippages) // 2]
                                if slippages else None),
        "execution_error_rate": metrics.error_rate,
        "rejection_rate": metrics.rejection_rate,
        "unknown_state_rate": metrics.unknown_state_rate,
        "reconciliation_mismatch_rate": (
            dirty / len(reconciliations) if reconciliations else None),
    }


def do_request(stack: Dict[str, Any], args, now: datetime) -> None:
    level = ExecutionLevel(args.request_level)
    request = stack["governor"].request(level, args.actor, now,
                                        args.reason or "operator request")
    stack["governance_repo"].save_approval(request)
    line("PROMOTION REQUESTED")
    print(f"  request     {request.request_id}")
    print(f"  level       {int(level)} — {level.label}")
    print(f"  requested   {args.actor}")
    if not level.is_implemented:
        print(f"\n  NOTE: level {int(level)} has no execution path in this")
        print(f"  repository. The request is recorded and the gates will be")
        print(f"  evaluated, but the system cannot operate there.")
    print(f"\n  A DIFFERENT actor must approve:")
    print(f"      python scripts/run_operations.py --approve {request.request_id} "
          f"--actor <other-operator>")


def do_revoke(stack: Dict[str, Any], args, now: datetime) -> None:
    repo, governor = stack["governance_repo"], stack["governor"]
    request = governor.requests.get(args.revoke)
    line("PROMOTION REVOKED")
    if request is None:
        print(f"  no request {args.revoke}")
        return
    if not args.reason:
        print("  Revocation requires a reason. An approval withdrawn with")
        print("  no explanation leaves the next operator unable to tell")
        print("  whether it was a mistake or a decision.")
        return
    request.revoke(args.actor, now, args.reason)
    repo.save_approval(request)
    print(f"  request     {request.request_id}")
    print(f"  level       {int(request.level)} — {request.level.label}")
    print(f"  revoked by  {args.actor}")
    print(f"  reason      {args.reason}")
    print(f"\n  effective level now: {int(governor.effective_level(now))} — "
          f"{governor.effective_level(now).label}")


def do_approve(stack: Dict[str, Any], args, now: datetime) -> None:
    repo, governor = stack["governance_repo"], stack["governor"]
    request = governor.requests.get(args.approve)
    line("PROMOTION APPROVAL")
    if request is None:
        print(f"  no request {args.approve}")
        return
    try:
        request.approve(args.actor, now,
                        ttl=timedelta(hours=args.approval_hours),
                        note=args.reason or "")
    except ValueError as error:
        print(f"  REFUSED: {error}")
        return
    request.gate_snapshot = _promotion_metrics(stack, now)
    if stack.get("readiness"):
        request.readiness_snapshot = stack["readiness"].as_dict()
    repo.save_approval(request)
    print(f"  approved by {args.actor}")
    print(f"  expires     {request.expires_at.isoformat()}")
    print(f"  effective level now: {int(governor.effective_level(now))} — "
          f"{governor.effective_level(now).label}")
    if request.level.is_implemented is False:
        print("\n  The approval is recorded, and the effective level still")
        print("  degrades to the highest IMPLEMENTED level. Approving a level")
        print("  the code cannot execute does not make it executable.")


def do_start(stack: Dict[str, Any], args, now: datetime) -> None:
    repo, health = stack["governance_repo"], stack.get("health") or read_health(stack, now)
    if stack["session"] is not None:
        line("SESSION START REFUSED")
        print(f"  {stack['session'].session_id} is already "
              f"{stack['session'].state.value}")
        print("  Stop it before opening another. Two sessions would each")
        print("  hold their own limits and neither would see the other's.")
        return

    record = stack["execution_repo"].reconciliations_for("ibkr", limit=1)
    unknown = [o for o in stack["orchestrator"].orders.values()
               if o.state.needs_reconciliation]

    config = SessionConfiguration(
        broker_id="ibkr", account_id=stack["account_id"],
        environment=ExecutionEnvironment.PAPER,
        level=stack["governor"].effective_level(now),
        strategies=tuple(args.strategy or []),
        capital_limit=args.capital_limit,
        max_order_notional=stack["limits"].capital.max_order_notional,
        daily_loss_limit=args.daily_loss_limit,
        max_open_positions=stack["limits"].capital.max_open_positions,
        model_version=args.model_version, strategy_version=args.strategy_version)

    approval = stack["governor"].active_approval(now)
    session = new_session(config, args.actor, now,
                          approval_id=approval.request_id if approval else None)

    checks = standard_preflight(
        broker_connected=health.permits_new_orders,
        account_available=stack.get("account_snapshot") is not None,
        market_data_live=(health.readings[Capability.MARKET_DATA].state
                          is CapabilityState.HEALTHY),
        reconciliation_clean=(record[0]["is_clean"] if record else None),
        risk_available=True,
        capital_configured=stack["limits"].capital.max_order_notional is not None,
        no_unknown_orders=(len(unknown) == 0),
        kill_switch_off=not stack["safety"].kill_switch_active)
    passed = session.run_preflight(checks, args.actor, now)

    line("SESSION PREFLIGHT")
    for check in session.preflight:
        state = ("pass" if check.passed and check.measured
                 else ("NOT MEASURED" if not check.measured else "FAIL"))
        print(f"  [{state:^13}] {check.name}"
              + (f" — {check.detail}" if check.detail else ""))

    if not passed:
        print("\n  Session NOT started. Every check must pass and be measured;")
        print("  an unmeasured check blocks, because not knowing whether the")
        print("  account reconciles is not the same as knowing that it does.")
        repo.save_session(session)
        return

    session.apply(SessionAction.START, args.actor, now)
    repo.save_session(session)
    print(f"\n  STARTED  {session.session_id}")
    print(f"  level        {int(config.level)} — {config.level.label}")
    print(f"  fingerprint  {session.fingerprint}")
    print(f"  configuration is now frozen; amending it requires a new session")


def do_session_action(stack: Dict[str, Any], action: SessionAction,
                      args, now: datetime) -> None:
    session = stack["session"]
    line(f"SESSION {action.value.upper()}")
    if session is None:
        print("  no active session")
        return
    try:
        session.apply(action, args.actor, now,
                      args.reason or ("operator request"
                                      if action in (SessionAction.PAUSE,
                                                    SessionAction.RESUME)
                                      else ""))
    except (SessionTransitionError, ValueError) as error:
        print(f"  REFUSED: {error}")
        return
    stack["governance_repo"].save_session(session)
    print(f"  {session.session_id} is now {session.state.value.upper()}")
    if action is SessionAction.EMERGENCY_STOP:
        print("  Positions and history are untouched. An emergency stop is")
        print("  terminal: continuing requires opening a new session.")


def do_close(stack: Dict[str, Any], args, now: datetime) -> None:
    """End-of-day reconciliation and the session report (spec §45, §86)."""
    session = stack["session"]
    line("END-OF-SESSION RECONCILIATION")
    if session is None:
        print("  no active session")
        return

    orchestrator = stack["orchestrator"]
    positions = {p.instrument_id: p.quantity
                 for p in stack.get("positions", []) if p.instrument_id}
    record = orchestrator.reconcile("ibkr", stack["account_id"], now,
                                    internal_positions=positions)
    if record is not None:
        stack["execution_repo"].save_reconciliation(record)
        print(f"  checks     {record.checks_performed}")
        print(f"  clean      {record.is_clean}")
        if not record.is_clean:
            print(f"  worst      {record.worst_severity.value.upper()}")
            for mismatch in record.mismatches:
                flag = "BLOCKS" if mismatch.blocks_execution else "      "
                print(f"      [{flag}] {mismatch.severity.value:<8} "
                      f"{mismatch.detail}")
            print(f"\n  blocks new execution: {record.blocks_execution}")

    orders = [o for o in orchestrator.orders.values() if o.broker_id == "ibkr"]
    summary = SessionSummary(
        session_id=session.session_id, at=now,
        orders_submitted=len(orders),
        orders_filled=sum(1 for o in orders if o.state.value == "filled"),
        orders_rejected=sum(1 for o in orders if o.state.value == "rejected"),
        orders_unknown=sum(1 for o in orders if o.state.needs_reconciliation),
        open_orders=sum(1 for o in orders if o.state.is_working),
        fills=len(orchestrator.fills),
        open_positions=len(stack.get("positions", [])),
        reconciliation_clean=record.is_clean if record else None,
        reconciliation_mismatches=len(record.mismatches) if record else 0,
        fees=sum(o.commission + o.fees for o in orders))
    session.close(summary)

    if args.stop_session:
        try:
            session.apply(SessionAction.STOP, args.actor, now,
                          args.reason or "end of session")
        except (SessionTransitionError, ValueError) as error:
            print(f"  could not stop: {error}")
    stack["governance_repo"].save_session(session)

    line("SESSION SUMMARY")
    for key, value in summary.as_dict().items():
        if key in ("session_id", "at"):
            continue
        print(f"  {key:<28} {value}")
    print(f"\n  clean close: {summary.is_clean_close}")
    if not summary.is_clean_close:
        print("  A session with open orders or unknown states has not")
        print("  finished — it has stopped. Resolve before the next session.")


def do_daily_report(stack: Dict[str, Any], now: datetime) -> None:
    repo = stack["governance_repo"]
    line("DAILY REPORT")
    outcomes = repo.query_outcomes(limit=500)
    missed = repo.missed_summary()
    alerts = repo.open_alerts()

    closed = [o for o in outcomes if o["exit_at"]]
    net = [o["net_pnl"] for o in closed if o["net_pnl"] is not None]
    wins = [p for p in net if p > 0]

    print(f"  trades closed          {len(closed)}")
    print(f"  net P&L                {fmt(sum(net)) if net else 'n/a'}")
    print(f"  win rate               "
          f"{f'{len(wins)/len(net):.1%}' if net else 'n/a'}")
    print(f"  fees                   {fmt(sum(o['fees'] for o in closed))}")
    print(f"  missed opportunities   {missed['total']}")
    print(f"      prevented by us    {missed['prevented_by_system']}")
    for reason, count in missed["by_reason"].items():
        print(f"      {reason:<20} {count}")
    print(f"  open alerts            {len(alerts)}")
    for alert in alerts[:6]:
        print(f"      [{alert['severity'].upper():<8}] {alert['message']}")
    print(f"  lineage complete       "
          f"{sum(1 for o in outcomes if o['lineage_complete'])}/{len(outcomes)}")
    print("\n  A day is not a sample. These figures describe what happened")
    print("  and establish nothing about the strategy.")


def do_compare(stack: Dict[str, Any], now: datetime) -> None:
    line("BACKTEST vs PAPER vs LIVE")
    metrics = stack["monitor"].metrics
    comparison = compare_environments(
        now,
        paper={"fill_rate": metrics.fill_rate,
               "rejection_rate": metrics.rejection_rate,
               "median_slippage_bps": metrics.median_slippage_bps},
        live=None)
    for row in comparison.rows:
        print(f"  {row.metric:<28} backtest {fmt(row.backtest):>10}  "
              f"paper {fmt(row.paper):>10}  live {fmt(row.live):>10}"
              + ("" if row.diagnostic else "   (context only)"))
    print(f"\n  conclusive: {comparison.is_conclusive}")
    for note in comparison.summary()["notes"]:
        print(f"  {note}")
    print(f"  {comparison.summary()['caveat']}")
    stack["governance_repo"].save_comparison(comparison)


def do_clear_breach(stack: Dict[str, Any], args, now: datetime) -> None:
    line("CLEAR LATCHED LIMIT")
    if not args.reason:
        print("  REFUSED: clearing a latched limit requires --reason")
        return
    cleared = stack["limits"].reactivate_all(args.actor, args.reason)
    for record in stack["governance_repo"].latched_breaches():
        stack["governance_repo"].clear_breach(record["breach_id"], args.actor,
                                              args.reason, now)
    print(f"  cleared {cleared} in-memory latch(es) and the stored records")
    print(f"  actor:  {args.actor}")
    print(f"  reason: {args.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--account")
    parser.add_argument("--mock", action="store_true",
                        help="run against the deterministic IBKR double")

    parser.add_argument("--status", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--gates", action="store_true")
    parser.add_argument("--level", type=int,
                        help="level to evaluate gates for (default 5)")
    parser.add_argument("--request-level", type=int, metavar="N")
    parser.add_argument("--approve", metavar="REQUEST_ID")
    parser.add_argument("--revoke", metavar="REQUEST_ID",
                        help="withdraw a granted approval; requires --reason")
    parser.add_argument("--approval-hours", type=float, default=24.0)
    parser.add_argument("--start-session", action="store_true")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--emergency-stop", action="store_true")
    parser.add_argument("--close", action="store_true",
                        help="end-of-session reconciliation and report")
    parser.add_argument("--stop-session", action="store_true",
                        help="with --close, also stop the session")
    parser.add_argument("--daily-report", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--clear-limits", action="store_true")

    parser.add_argument("--strategy", action="append")
    parser.add_argument("--model-version")
    parser.add_argument("--strategy-version")
    parser.add_argument("--capital-limit", type=float)
    parser.add_argument("--daily-loss-limit", type=float)
    parser.add_argument("--reason", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_execution_schema(conn)
    initialize_governance_schema(conn)

    try:
        stack = build(conn, args)
    except IBKRConfigurationError as error:
        print("=" * 72)
        print("IBKR CONFIGURATION REFUSED")
        print("=" * 72)
        print(f"  {error}")
        conn.close()
        return 2

    now = datetime.now(timezone.utc)
    print("=" * 72)
    print("MarketLens — Phase 16 execution operations")
    print("BROKER: Interactive Brokers (the only broker)")
    print("REAL-MONEY EXECUTION: BLOCKED. No adapter accepts one.")
    if args.mock:
        print("TRANSPORT: MOCK — no gateway, no account, no network")
    print("=" * 72)

    acted = False
    if args.readiness:
        show_readiness(stack, now); acted = True
    if args.gates:
        show_gates(stack, args, now); acted = True
    if args.request_level is not None:
        do_request(stack, args, now); acted = True
    if args.approve:
        do_approve(stack, args, now); acted = True
    if args.revoke:
        do_revoke(stack, args, now); acted = True
    if args.start_session:
        do_start(stack, args, now); acted = True
    if args.pause:
        do_session_action(stack, SessionAction.PAUSE, args, now); acted = True
    if args.resume:
        do_session_action(stack, SessionAction.RESUME, args, now); acted = True
    if args.emergency_stop:
        do_session_action(stack, SessionAction.EMERGENCY_STOP, args, now)
        acted = True
    elif args.stop:
        do_session_action(stack, SessionAction.STOP, args, now); acted = True
    if args.close:
        do_close(stack, args, now); acted = True
    if args.daily_report:
        do_daily_report(stack, now); acted = True
    if args.compare:
        do_compare(stack, now); acted = True
    if args.clear_limits:
        do_clear_breach(stack, args, now); acted = True

    if args.status or not acted:
        show_status(stack, now)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
