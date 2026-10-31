#!/usr/bin/env python3
"""
scripts/audit_trading_readiness.py
-----------------------------------------------------------
Phase 25.9E — can the automated trading path safely reach the broker
boundary, and what stops it today?

    python scripts/audit_trading_readiness.py --db data/marketlens.db
    python scripts/audit_trading_readiness.py --negative-controls

READ-ONLY, AND IT CANNOT SUBMIT
-----------------------------------
The database is opened `mode=ro` and copied into memory; every reading
runs on the copy. It never contacts IBKR and imports nothing that sends
an order. The negative controls run the real loop against the mock
venue in pre-submission mode, behind the gateway that raises on any
venue write, and assert the venue was never asked to place an order.

WHAT IT REPORTS
-------------------
SESSION RUNNER, MARKET DATA, PRICE FRESHNESS, BAR STATE, FEATURE
REFRESH, MODEL GATE, SIGNAL GATE, PORTFOLIO, RISK, ACCOUNT,
RECONCILIATION, EXECUTION, IDEMPOTENCY, TRADING MODE, IBKR SESSION,
RUNNER OWNERSHIP -- each with what was measured, not what should be.

A STOP IS NOT A FAILURE
---------------------------
"No deployable model" is the expected state and is reported as the
current stop point, not as a fault. Exit 0 means the audit ran and its
controls hold; the readiness verdict is printed, not encoded in the exit
code. Exit 2 means a negative control did not fire.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")


# ======================================================================
# Read-only access
# ======================================================================

def open_copy(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    source = sqlite3.connect(uri, uri=True)
    try:
        copy = sqlite3.connect(":memory:")
        source.backup(copy)
    finally:
        source.close()
    return copy


def _exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def _count(conn, table: str, where: str = "", params: tuple = ()) -> Optional[int]:
    if not _exists(conn, table):
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]


def _table(conn, table: str) -> str:
    rows = _count(conn, table)
    return "ABSENT" if rows is None else ("PRESENT, EMPTY" if rows == 0
                                          else f"{rows} row(s)")


def _workflows_invoking(name: str) -> List[str]:
    folder = os.path.join(ROOT, ".github", "workflows")
    found = []
    for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        with open(os.path.join(folder, entry), encoding="utf-8") as handle:
            text = handle.read()
        if name in text:
            active = any(line.strip().startswith("- cron:")
                         for line in text.splitlines())
            found.append(f"{entry} ({'scheduled' if active else 'manual only'})")
    return found


# ======================================================================
# Readings
# ======================================================================

def readings(conn: sqlite3.Connection, now: datetime) -> Dict[str, Dict[str, Any]]:
    from src.marketdata.prices import operational_price
    from src.trading.mode import TradingModeStore
    out: Dict[str, Dict[str, Any]] = {}

    def put(name, status, detail, **extra):
        out[name] = {"status": status, "detail": detail, **extra}

    workflows = _workflows_invoking("run_session.py")
    put("SESSION RUNNER", "CODE EXISTS; NOT SCHEDULED" if not any(
        "scheduled" in w for w in workflows) else "SCHEDULED",
        "no workflow invokes scripts/run_session.py" if not workflows
        else "; ".join(workflows),
        leases=_table(conn, "session_runner_leases"))

    state_rows = _count(conn, "market_data_state")
    fresh = {"fresh": 0, "aging": 0, "stale": 0, "unavailable": 0, "invalid": 0}
    newest = None
    if state_rows:
        for (instrument_id,) in conn.execute("SELECT instrument_id FROM market_data_state"):
            quote = operational_price(conn, instrument_id, now)
            fresh[quote.freshness.value] = fresh.get(quote.freshness.value, 0) + 1
            if quote.as_of and (newest is None or quote.as_of > newest):
                newest = quote.as_of
    put("MARKET DATA", "ABSENT" if state_rows is None else
        ("NEVER RUN" if state_rows == 0 else "PRESENT"),
        f"market_data_state {_table(conn, 'market_data_state')}; "
        f"broker_instrument_mapping {_table(conn, 'broker_instrument_mapping')}",
        latest_quote=newest.isoformat() if newest else None)
    put("PRICE FRESHNESS", "USABLE" if fresh["fresh"] + fresh["aging"] else "NONE USABLE",
        json.dumps(fresh))

    bars = _count(conn, "market_data_bars")
    incomplete = _count(conn, "market_data_bars", "WHERE is_complete = 0")
    latest_bar = (conn.execute("SELECT MAX(bar_start) FROM market_data_bars").fetchone()[0]
                  if bars else None)
    put("BAR STATE", "ABSENT" if bars is None else ("EMPTY" if bars == 0 else "PRESENT"),
        f"{bars or 0} bar(s), {incomplete or 0} persisted incomplete", latest_bar=latest_bar)

    put("FEATURE REFRESH", "PARTIAL",
        "the runner's features stage counts instruments with completed bars; "
        "no intraday feature is computed (Phase 25.8 limitation, unchanged)")

    deployable, evaluated, detail = 0, 0, ""
    try:
        from src.modeling.selection import candidates
        from src.domain.model_models import ModelStatus
        verdicts = candidates(conn)
        evaluated = len(verdicts)
        deployable = sum(1 for v in verdicts if v.status == ModelStatus.ACTIVE)
    except Exception as error:                              # noqa: BLE001
        detail = f"gate unreadable: {error}"
    put("MODEL GATE", "DEPLOYABLE MODEL" if deployable else "NO DEPLOYABLE MODEL",
        detail or f"{deployable} active of {evaluated} trained")

    live, top = 0, None
    if _exists(conn, "signals"):
        live, top = conn.execute(
            "SELECT COUNT(*), MAX(confidence) FROM signals").fetchone()
    floor = None
    try:
        from src.data_access.portfolio_schema import initialize_portfolio_schema
        from src.portfolio.service import PortfolioService
        from src.domain.portfolio_models import ConstraintScope
        initialize_portfolio_schema(conn)          # the in-memory copy only
        service = PortfolioService(conn)
        constraint = service.constraints.load_or_default(
            service.constraint_version).first(ConstraintScope.MIN_SIGNAL_CONFIDENCE)
        floor = constraint.min_value if constraint else None
    except Exception:                                       # noqa: BLE001
        pass
    put("SIGNAL GATE", "NOT READY" if not deployable or (top or 0) < (floor or 0)
        else "READY", f"{live} signal(s), max confidence {top}, floor {floor}")

    put("PORTFOLIO", _table(conn, "portfolio_decisions")
        if _exists(conn, "portfolio_decisions") else "ABSENT",
        "loop-evaluated decisions (Phase 11 tables)")
    put("RISK", _table(conn, "risk_decisions") if _exists(conn, "risk_decisions")
        else "ABSENT", "risk decisions written by the loop")

    latest_account = None
    if _exists(conn, "loop_account_states"):
        latest_account = conn.execute(
            "SELECT source, observed_at FROM loop_account_states "
            "ORDER BY observed_at DESC LIMIT 1").fetchone()
    put("ACCOUNT", "NEVER READ" if not latest_account else latest_account[0].upper(),
        f"latest {latest_account[1]}" if latest_account else
        "no account state has been recorded against this database")

    baseline = None
    if _exists(conn, "reconciliation_baselines"):
        baseline = conn.execute(
            "SELECT source, actor, recorded_at FROM reconciliation_baselines "
            "ORDER BY recorded_at DESC LIMIT 1").fetchone()
    put("RECONCILIATION", "NO BASELINE" if not baseline else "BASELINE",
        f"{baseline}" if baseline else
        "no reconciliation has agreed yet; first contact records one or blocks")

    unknown = _count(conn, "execution_orders",
                     "WHERE state IN ('submitting','submitted','unknown',"
                     "'reconciliation_required')")
    put("EXECUTION", "ABSENT" if unknown is None else
        ("CLEAN" if unknown == 0 else "IN-FLIGHT/UNKNOWN ORDERS"),
        f"{unknown or 0} order(s) awaiting a broker answer")

    duplicates = None
    if _exists(conn, "execution_orders"):
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT client_order_id FROM execution_orders "
            "WHERE client_order_id != '' GROUP BY client_order_id "
            "HAVING COUNT(*) > 1)").fetchone()[0]
    put("IDEMPOTENCY", "ABSENT" if duplicates is None else
        ("PASS" if duplicates == 0 else "DUPLICATE CLIENT ORDER IDS"),
        f"{duplicates or 0} duplicated client order id(s)")

    mode = TradingModeStore(conn).resolve(now)
    put("TRADING MODE", mode.mode.value.upper(), mode.reason or "")
    put("IBKR SESSION", "NOT CONTACTED", "this audit is offline by design")

    holders = []
    if _exists(conn, "session_runner_leases"):
        holders = conn.execute(
            "SELECT scope, owner, expires_at FROM session_runner_leases "
            "WHERE released_at IS NULL AND expires_at > ?",
            (now.isoformat(),)).fetchall()
    put("RUNNER OWNERSHIP", "HELD" if holders else "FREE", json.dumps(holders))
    return out


def stop_point(read: Dict[str, Dict[str, Any]]) -> str:
    """The first thing, in pipeline order, that stops an order today."""
    if read["MARKET DATA"]["status"] in ("ABSENT", "NEVER RUN"):
        return ("MARKET DATA: the operational layer has never run against this "
                "database (no contract mappings, no quotes)")
    if read["PRICE FRESHNESS"]["status"] != "USABLE":
        return "PRICE FRESHNESS: no usable operational quote"
    if read["MODEL GATE"]["status"] != "DEPLOYABLE MODEL":
        return "MODEL GATE: no deployable model"
    if read["SIGNAL GATE"]["status"] != "READY":
        return "SIGNAL GATE: no signal clears the confidence floor"
    if read["TRADING MODE"]["status"] != "PAPER":
        return "TRADING MODE: not PAPER"
    return "none found offline; a live pre-submission cycle is the next check"


# ======================================================================
# Negative controls
# ======================================================================

def _scenario(inject: Optional[Callable] = None, *, deployable: bool = True,
              quote_age: float = 10.0, positions=None, confidence: float = 0.75):
    """One real pre-submission cycle against the mock venue."""
    from unittest import mock
    from src.data_access.market_data_schema import initialize_market_data_schema
    from src.domain.market_data_models import MarketDataAvailability, OperationalQuote
    from src.execution.adapters.submission_guard import PreSubmissionGateway
    from src.marketdata.repository import MarketDataRepository
    from src.trading import pricing
    from tests.trading.helpers import (NOW, a_live_signal, build_loop, enable_paper,
                                       make_connection, store_signals, universe)

    conn = make_connection()
    universe(conn)
    store_signals(conn, [a_live_signal(confidence=confidence)])
    enable_paper(conn)
    initialize_market_data_schema(conn)
    stamp = NOW - timedelta(seconds=quote_age)
    MarketDataRepository(conn).upsert_quotes([OperationalQuote(
        instrument_id="i-aapl", last=120.0, bid=119.99, ask=120.01, mid=120.0,
        availability=MarketDataAvailability.AVAILABLE, broker_at=stamp,
        received_at=stamp)], NOW)
    loop = build_loop(conn, experimental=False, pre_submission_only=True,
                      price_source=pricing.OPERATIONAL, positions=positions)
    loop.stack.gateway = PreSubmissionGateway(loop.stack.gateway)
    loop.stack.orchestrator.registry.get("ibkr").gateway = loop.stack.gateway
    if inject:
        inject(loop, conn, NOW)
    with mock.patch.object(type(loop), "_model_governance",
                           lambda self: ({"tm-fixture-1": deployable}, {}, "")):
        result = loop.run_cycle(NOW)
    return loop, result, conn


def negative_controls() -> List[Dict[str, Any]]:
    from src.execution.adapters.submission_guard import BrokerSubmissionForbidden
    from src.trading import leases, readiness

    outcomes = []

    def record(name, passed, detail=""):
        outcomes.append({"control": name, "passed": bool(passed), "detail": detail})

    def cycle(name, expect, **kwargs):
        try:
            loop, result, conn = _scenario(**kwargs)
        except Exception as error:                          # noqa: BLE001
            record(name, False, f"raised {error}")
            return
        verdict = result.readiness
        sent = loop.stack.transport.place_calls
        record(name, expect(verdict) and sent == 0,
               f"{verdict.verdict}/{verdict.classification}, place_calls={sent}")
        conn.close()

    ready = lambda v: v.verdict == readiness.READY_TO_SUBMIT and not v.order_authorized
    not_ready = lambda v: v.verdict != readiness.READY_TO_SUBMIT

    cycle("clean fixture reaches READY_TO_SUBMIT and sends nothing", ready)
    cycle("stale price blocks", not_ready, quote_age=3600)
    cycle("future price blocks", not_ready, quote_age=-600)

    def missing_account(loop, conn, now):
        def refuse(*a, **k):
            raise RuntimeError("account endpoint unavailable")
        loop.stack.gateway.inner.get_account = refuse
    cycle("missing account blocks", not_ready, inject=missing_account)

    def stale_account(loop, conn, now):
        original = loop.stack.gateway.inner.get_account

        def old(account_id, at):
            snapshot = original(account_id, at)
            snapshot.at = at - timedelta(hours=2)
            return snapshot
        loop.stack.gateway.inner.get_account = old
    cycle("stale account blocks", not_ready, inject=stale_account)

    cycle("risk rejection sends nothing", not_ready, confidence=0.30)
    cycle("reconciliation mismatch blocks", not_ready, positions={"i-aapl": 10.0})

    def session_loss(loop, conn, now):
        loop.stack.transport.connected = False
        loop.stack.gateway.heartbeat()
    cycle("session loss blocks", not_ready, inject=session_loss)
    cycle("no deployable model is a normal no-trade",
          lambda v: v.classification == readiness.NORMAL_NO_TRADE, deployable=False)

    try:
        loop, first, conn = _scenario()
        second = loop.run_cycle(first.anchor + timedelta(seconds=5))
        record("duplicate cycle is refused",
               any(b.reason.value == "cycle_already_running" for b in second.blocks)
               and loop.stack.transport.place_calls == 0)
        leases.acquire(conn, "ibkr:X", "runner-a", first.anchor)
        try:
            leases.acquire(conn, "ibkr:X", "runner-b", first.anchor)
            record("second runner is refused", False)
        except leases.LeaseRefused:
            record("second runner is refused", True)
        conn.close()
    except Exception as error:                              # noqa: BLE001
        record("duplicate cycle / second runner", False, str(error))

    try:
        from src.domain.trading_loop_models import TradingMode, TradingModeRefused
        from src.trading.mode import TradingModeStore
        loop, _r, conn = _scenario()
        try:
            TradingModeStore(conn).set_mode(TradingMode.LIVE, actor="x", reason="y",
                                            at=_r.anchor)
            record("forbidden LIVE mode is refused", False)
        except TradingModeRefused:
            record("forbidden LIVE mode is refused", True)
        try:
            loop.stack.gateway.submit_order(object(), _r.anchor)
            record("broker-submit trap fires", False)
        except BrokerSubmissionForbidden as trap:
            record("broker-submit trap fires",
                   trap.code == "PHASE_25_9E_BROKER_SUBMISSION_FORBIDDEN"
                   and loop.stack.transport.place_calls == 0)
        conn.close()
    except Exception as error:                              # noqa: BLE001
        record("LIVE / trap controls", False, str(error))
    return outcomes


# ======================================================================
# CLI
# ======================================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--negative-controls", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.negative_controls:
        outcomes = negative_controls()
        for item in outcomes:
            print("%-4s %-58s %s" % ("PASS" if item["passed"] else "FAIL",
                                     item["control"], item["detail"]))
        failed = [o for o in outcomes if not o["passed"]]
        print("negative controls: %d/%d passed" % (len(outcomes) - len(failed),
                                                    len(outcomes)))
        return 2 if failed else 0

    read = readings(open_copy(args.db), now)
    point = stop_point(read)
    if args.json:
        print(json.dumps({"readings": read, "stop_point": point}, indent=2, default=str))
        return 0
    for name, value in read.items():
        print("%-18s %-28s %s" % (name, value["status"], value["detail"]))
    print("\nCURRENT STOP POINT: " + point)
    print("ORDER SUBMISSION:   impossible from this command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
