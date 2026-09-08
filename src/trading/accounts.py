"""
src/trading/accounts.py
-----------------------------
Canonical account state, and the health verdict built from it (§4, §27).

WHAT "CANONICAL" MEANS HERE
-------------------------------
Not "our best guess". Every figure carries a `source`, and the only
source that may be reported as fact is BROKER. Spec §17 says plainly:
*do not mix estimated and authoritative numbers without labelling*, so
the label is a required field on `CanonicalAccountState` rather than a
convention anybody has to remember.

An account the gateway could not answer for produces a row with source
UNAVAILABLE and every figure None. It is written rather than skipped,
because "we asked and could not find out" is different from "we never
asked", and §25 blocks trading on the first while saying nothing about
the second.

WHY HEALTH IS ASSEMBLED HERE
--------------------------------
§27 lists eleven things to check and asks for one verdict. Building
that verdict beside the account read keeps the two from disagreeing:
the same gateway call that produced the balances produced the
connection state, and re-asking would allow a window in which the
balances came from a broker the health report calls disconnected.

THE WORST READING WINS
--------------------------
`HealthReport.overall` is a max over severity, not an average. One
blocked component blocks the loop. That is what fail-closed means when
it is written as an aggregation rule, and it is why a component nobody
measured shows in `unmeasured()` rather than counting as healthy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import (
    AccountSnapshot, BrokerConnectionState, ExecutionOrderState,
    PositionSnapshot,
)
from src.domain.trading_loop_models import (
    AccountStateSource, BlockReason, CanonicalAccountState, ComponentReading,
    HealthReport, LoopHealth, ModeResolution, require_utc,
)

#: Account state older than this is treated as unusable. Fifteen
#: minutes rather than a day: an account balance is the input to the
#: buying-power check, and a stale one permits an order the account
#: cannot afford.
DEFAULT_MAX_ACCOUNT_AGE_SECONDS = 900.0

#: Market data older than this blocks trading (§25). Expressed in days
#: because this repository's price cache is daily bars, and a rule in
#: seconds would block every cycle that runs before the day's close.
DEFAULT_MAX_PRICE_AGE_DAYS = 5.0


class AccountUnavailable(Exception):
    """
    Raised only where a caller genuinely cannot continue.

    The normal path returns an UNAVAILABLE state instead, so the cycle
    can record what it found and block cleanly rather than unwinding
    through an exception and losing the evidence.
    """


def read_account_state(gateway: Any, broker_id: str, account_id: str,
                       cycle_id: str, now: datetime,
                       ) -> Tuple[CanonicalAccountState,
                                  List[PositionSnapshot],
                                  List[Any]]:
    """
    One read of everything the broker knows about the account.

    Returns `(state, positions, open_orders)`. The three are returned
    together because they must describe the same instant: reading them
    in three separate calls at three separate times produces a picture
    that never existed, and reconciliation would then chase a
    discrepancy that is an artefact of the reading.

    Adapter failures are caught HERE and only here, and turned into an
    UNAVAILABLE state with the error in `detail`. Everywhere else in
    this package a bare `except Exception` would be the bug Phase 24
    hit twice: a programming error converted into an empty result.
    """
    require_utc(now, "now")

    connection = "unknown"
    try:
        state = gateway.connection_state()
        connection = getattr(state, "value", str(state))
    except Exception as error:                              # noqa: BLE001
        connection = f"unreadable: {error}"

    snapshot: Optional[AccountSnapshot] = None
    positions: List[PositionSnapshot] = []
    orders: List[Any] = []
    detail = ""

    try:
        snapshot = gateway.get_account(account_id, now)
    except Exception as error:                              # noqa: BLE001
        detail = f"account query failed: {error}"

    if snapshot is not None:
        try:
            positions = list(gateway.get_positions(account_id, now))
        except Exception as error:                          # noqa: BLE001
            detail = (detail + "; " if detail else "") + \
                f"position query failed: {error}"
        try:
            orders = list(gateway.get_open_orders(account_id))
        except Exception as error:                          # noqa: BLE001
            detail = (detail + "; " if detail else "") + \
                f"open-order query failed: {error}"

    if snapshot is None:
        return (CanonicalAccountState(
            cycle_id=cycle_id, broker_id=broker_id, account_id=account_id,
            source=AccountStateSource.UNAVAILABLE, observed_at=now,
            connection_state=connection, detail=detail or
            "the gateway returned no account snapshot"),
            [], [])

    pending = sum(1 for order in orders
                  if not getattr(getattr(order, "state", None), "is_terminal",
                                 False))

    return (CanonicalAccountState(
        cycle_id=cycle_id, broker_id=broker_id, account_id=account_id,
        source=AccountStateSource.BROKER,
        observed_at=snapshot.at or now,
        base_currency=snapshot.base_currency,
        cash=snapshot.cash, equity=snapshot.equity,
        buying_power=snapshot.buying_power,
        available_funds=snapshot.available_funds,
        margin_used=snapshot.margin.margin_used,
        margin_available=snapshot.margin.margin_available,
        realized_pnl=snapshot.realized_pnl,
        unrealized_pnl=snapshot.unrealized_pnl,
        open_positions=len(positions), open_orders=len(orders),
        pending_orders=pending, connection_state=connection,
        synchronized_at=now, detail=detail),
        positions, orders)


def assess_health(cycle_id: str, now: datetime, *,
                  mode: ModeResolution,
                  kill_switch: bool,
                  account: Optional[CanonicalAccountState],
                  connection_healthy: Optional[bool],
                  market_data_age_days: Optional[float],
                  signals_available: Optional[int],
                  portfolio_known: Optional[bool],
                  risk_known: Optional[bool],
                  execution_ready: Optional[bool],
                  reconciliation_clean: Optional[bool],
                  database_ok: bool = True,
                  scheduler_age_seconds: Optional[float] = None,
                  max_account_age_seconds: float = DEFAULT_MAX_ACCOUNT_AGE_SECONDS,
                  max_price_age_days: float = DEFAULT_MAX_PRICE_AGE_DAYS,
                  ) -> HealthReport:
    """
    Every component §27 names, read once, into one verdict.

    Each parameter is Optional and None means UNMEASURED, which reads
    as DEGRADED rather than HEALTHY. That asymmetry is the point: a
    component nobody could check is not a component that passed, and
    Phase 24 spent a whole phase learning that lesson on a scorecard.
    """
    require_utc(now, "now")
    report = HealthReport(cycle_id=cycle_id, assessed_at=now)

    # -- mode and kill switch --------------------------------------
    if mode.may_trade:
        report.add("trading_mode", LoopHealth.HEALTHY, "paper")
    else:
        report.add("trading_mode", LoopHealth.BLOCKED,
                   mode.reason or f"mode is {mode.mode.value}")

    report.add("kill_switch",
               LoopHealth.BLOCKED if kill_switch else LoopHealth.HEALTHY,
               "active" if kill_switch else "clear")

    # -- broker ----------------------------------------------------
    if connection_healthy is None:
        report.add("broker_connection", LoopHealth.DEGRADED,
                   "the gateway was not asked")
    elif connection_healthy:
        report.add("broker_connection", LoopHealth.HEALTHY, "connected")
    else:
        report.add("broker_connection", LoopHealth.BLOCKED,
                   "the gateway is not usable")

    # -- account ---------------------------------------------------
    if account is None:
        report.add("account_state", LoopHealth.BLOCKED,
                   "no account state was read")
    elif not account.is_known:
        report.add("account_state", LoopHealth.BLOCKED,
                   account.detail or f"source is {account.source.value}")
    else:
        age = account.age_seconds(now)
        if age is not None and age > max_account_age_seconds:
            report.add("account_state", LoopHealth.BLOCKED,
                       f"the account state is {age:.0f}s old, past the "
                       f"{max_account_age_seconds:.0f}s limit",
                       observed_at=account.observed_at, age_seconds=age)
        else:
            report.add("account_state", LoopHealth.HEALTHY,
                       f"equity {account.equity:,.2f} {account.base_currency}",
                       observed_at=account.observed_at, age_seconds=age)

    # -- market data -----------------------------------------------
    if market_data_age_days is None:
        report.add("market_data", LoopHealth.BLOCKED,
                   "no market data is available at the anchor")
    elif market_data_age_days > max_price_age_days:
        report.add("market_data", LoopHealth.BLOCKED,
                   f"the newest bar is {market_data_age_days:.1f} days old, "
                   f"past the {max_price_age_days:.0f}-day limit",
                   age_seconds=market_data_age_days * 86400.0)
    else:
        report.add("market_data", LoopHealth.HEALTHY,
                   f"the newest bar is {market_data_age_days:.1f} days old",
                   age_seconds=market_data_age_days * 86400.0)

    # -- signals ---------------------------------------------------
    if signals_available is None:
        report.add("signals", LoopHealth.DEGRADED, "the signal table was not read")
    elif signals_available == 0:
        # Not a block. No signal is a legitimate state of the world and
        # the correct response is to place no orders, which is what the
        # rest of the cycle does anyway.
        report.add("signals", LoopHealth.HEALTHY, "no signal is live")
    else:
        report.add("signals", LoopHealth.HEALTHY,
                   f"{signals_available} live")

    _boolean(report, "portfolio", portfolio_known,
             blocked="the portfolio state is unknown")
    _boolean(report, "risk", risk_known,
             blocked="the risk state is unknown")
    _boolean(report, "execution", execution_ready,
             blocked="the execution stack is not ready")

    if reconciliation_clean is None:
        report.add("reconciliation", LoopHealth.DEGRADED,
                   "reconciliation has not run in this cycle")
    elif reconciliation_clean:
        report.add("reconciliation", LoopHealth.HEALTHY, "no discrepancy")
    else:
        report.add("reconciliation", LoopHealth.BLOCKED,
                   "unresolved discrepancies stand")

    report.add("database",
               LoopHealth.HEALTHY if database_ok else LoopHealth.BLOCKED,
               "ok" if database_ok else "the database is not writable")

    if scheduler_age_seconds is None:
        report.add("scheduler", LoopHealth.DEGRADED,
                   "no previous cycle to measure against")
    elif scheduler_age_seconds > 4 * 86400.0:
        report.add("scheduler", LoopHealth.DEGRADED,
                   f"the last cycle ran {scheduler_age_seconds / 86400.0:.1f} "
                   f"days ago", age_seconds=scheduler_age_seconds)
    else:
        report.add("scheduler", LoopHealth.HEALTHY,
                   f"the last cycle ran "
                   f"{scheduler_age_seconds / 3600.0:.1f}h ago",
                   age_seconds=scheduler_age_seconds)

    return report


def _boolean(report: HealthReport, component: str, value: Optional[bool],
             *, blocked: str) -> ComponentReading:
    if value is None:
        return report.add(component, LoopHealth.DEGRADED, "not measured")
    if value:
        return report.add(component, LoopHealth.HEALTHY, "ok")
    return report.add(component, LoopHealth.BLOCKED, blocked)


#: Which block reason a failed component implies. A dict rather than a
#: chain of ifs so that a component added to §27 without a block reason
#: is a KeyError at test time instead of a silent generic block.
COMPONENT_BLOCKS: Dict[str, BlockReason] = {
    "trading_mode": BlockReason.MODE_NOT_PERMITTED,
    "kill_switch": BlockReason.KILL_SWITCH,
    "broker_connection": BlockReason.BROKER_DISCONNECTED,
    "account_state": BlockReason.ACCOUNT_STATE_UNKNOWN,
    "market_data": BlockReason.STALE_MARKET_DATA,
    "signals": BlockReason.STALE_SIGNAL,
    "portfolio": BlockReason.PORTFOLIO_STATE_UNKNOWN,
    "risk": BlockReason.RISK_STATE_UNKNOWN,
    "execution": BlockReason.BROKER_UNHEALTHY,
    "reconciliation": BlockReason.RECONCILIATION_UNRESOLVED,
    "database": BlockReason.PORTFOLIO_STATE_UNKNOWN,
    "scheduler": BlockReason.SESSION_NOT_OPEN,
}


def blocks_from(report: HealthReport) -> List[Tuple[BlockReason, str]]:
    """Turn blocking readings into the reasons the cycle records."""
    out: List[Tuple[BlockReason, str]] = []
    for reading in report.blocking:
        reason = COMPONENT_BLOCKS.get(reading.component)
        if reason is None:
            raise KeyError(
                f"component {reading.component!r} can block but has no entry "
                f"in COMPONENT_BLOCKS; add one rather than letting it fall "
                f"through to a generic reason")
        out.append((reason, f"{reading.component}: {reading.detail}"))
    return out
