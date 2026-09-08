"""
src/trading/stack.py
--------------------------
Assembling the execution stack the loop drives.

WHY THIS IS A LIBRARY AND NOT A THIRD COPY IN A SCRIPT
---------------------------------------------------------
`scripts/run_execution.py` and `scripts/run_ibkr.py` each hand-roll
this assembly — registry, gateway, safety, orchestrator, repository,
restore. That is TD-06 in the debt register: business logic living in
scripts. Adding a third copy for the loop would make the drift
three-way, so the assembly lives here and the new script calls it.

The two existing scripts are deliberately left alone. They work, they
are tested, and rewriting a working CLI to prove a point is the kind
of change the brief forbids.

RECOVERY IS NOT OPTIONAL
----------------------------
`build_stack` always calls `ExecutionRepository.restore()`. A fresh
process has an EMPTY idempotency index, and without the restore the
first submission of a re-run would mint a new order carrying a key the
database already holds — the duplicate §10 forbids, produced by the
very mechanism meant to prevent it.

In-flight orders are marked UNKNOWN rather than assumed dead. An order
that was handed to a venue and whose outcome we never saw is the one
case where guessing is unforgivable: guessing "not sent" resubmits and
doubles the position, guessing "filled" invents a holding.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.backtest.calendar import MarketCalendar
from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.broker_models import (
    Broker, BrokerAccount, BrokerConnectionState, ExecutionEnvironment,
    ExecutionPermission,
)
from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import MOCK_ACCOUNT, MockIBKRTransport
from src.execution.adapters.ibkr.transport import ClientPortalTransport
from src.execution.instruments import InstrumentRegistry
from src.execution.orchestrator import BrokerRegistry, ExecutionOrchestrator
from src.execution.safety import ExecutionSafety
from src.execution.service import Caller, ExecutionService

#: The only broker id this project has. Named once so a typo is an
#: import error rather than an unknown-broker rejection at run time.
BROKER_ID = "ibkr"


@dataclass
class ExecutionStack:
    """Everything the loop needs to reach a venue, assembled once."""
    config: IBKRConfig
    transport: Any
    gateway: IBKRGateway
    instruments: InstrumentRegistry
    calendar: MarketCalendar
    orchestrator: ExecutionOrchestrator
    repository: ExecutionRepository
    service: ExecutionService
    safety: ExecutionSafety
    account_id: str
    broker_id: str = BROKER_ID
    recovery: Dict[str, Any] = field(default_factory=dict)
    connected: bool = False
    connect_detail: str = ""

    @property
    def may_submit(self) -> bool:
        """
        Whether an order could actually be sent.

        Three independent conditions, all of which must hold: the
        integration is enabled, ordering is separately enabled, and the
        environment is not real money. `IBKRConfig.can_submit_orders`
        is the single place that rule lives.
        """
        return bool(self.config.can_submit_orders) and self.connected


def build_stack(conn: sqlite3.Connection, *,
                actor: str,
                mock: bool = False,
                account_id: Optional[str] = None,
                allow_paper_orders: bool = False,
                universe_limit: int = 25,
                persist: bool = True) -> ExecutionStack:
    """
    Build the IBKR paper stack and restore its state.

    `mock=True` runs the whole path against the deterministic double —
    no gateway, no account, no network. That is how the loop is
    testable and demonstrable on a machine that has neither, and it is
    the ONLY difference: every other layer is the same object the real
    path uses.
    """
    initialize_execution_schema(conn)

    overrides: Dict[str, Any] = {}
    if mock:
        overrides["enabled"] = True
        overrides["account_id"] = account_id or MOCK_ACCOUNT
    if account_id:
        overrides["account_id"] = account_id
    if allow_paper_orders:
        overrides["ordering_enabled"] = True

    config = IBKRConfig.from_environment(**overrides)

    transport = (MockIBKRTransport(config,
                                   account_id=config.account_id or MOCK_ACCOUNT)
                 if mock else ClientPortalTransport(config))

    instruments = InstrumentRegistry(conn)
    instruments.load()

    calendar = MarketCalendar(conn)
    universe = [r[0] for r in conn.execute("""
        SELECT instrument_id FROM price_candle_cache WHERE interval='1d'
        GROUP BY instrument_id ORDER BY COUNT(*) DESC LIMIT ?
    """, (universe_limit,))]
    if universe:
        calendar.load(universe)

    gateway = IBKRGateway(config, transport, instruments, calendar=calendar)

    connected = False
    detail = ""
    try:
        state = gateway.connect()
        # `can_submit` rather than "is it roughly up": DEGRADED means
        # the link can carry a submission whose acknowledgement never
        # arrives, and Phase 14 already decided that is not good enough
        # for new exposure. Reusing its predicate keeps one rule.
        connected = bool(state.can_submit)
        detail = state.value
    except Exception as error:                              # noqa: BLE001
        # Caught here and reported, not swallowed: a gateway that will
        # not connect is an operational condition the cycle must record
        # and block on, and an exception escaping into the loop would
        # lose the reason.
        detail = f"connect failed: {error}"

    resolved_account = config.account_id or (MOCK_ACCOUNT if mock else "")
    now = datetime.now(timezone.utc)

    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id=BROKER_ID,
               name="Interactive Brokers (paper)",
               environment=ExecutionEnvironment.PAPER,
               adapter=f"ibkr-gateway-v1/{transport.name}",
               enabled=config.enabled, created_at=now),
        gateway,
        [BrokerAccount(account_id=resolved_account, broker_id=BROKER_ID,
                       name="IBKR paper account",
                       environment=ExecutionEnvironment.PAPER,
                       created_at=now)] if resolved_account else [])

    safety = ExecutionSafety(actor=actor)
    orchestrator = ExecutionOrchestrator(registry, instruments, safety,
                                         actor=actor)
    repository = ExecutionRepository(conn)

    recovery = repository.restore(orchestrator, broker_id=BROKER_ID)
    known = [o for o in orchestrator.orders.values()
             if o.broker_id == BROKER_ID]
    gateway.restore_known_orders(known)
    gateway.restore_seen_executions(
        [f.execution_id for f in repository.fills_for(broker_id=BROKER_ID)
         if f.execution_id])
    if recovery.get("in_flight"):
        orchestrator.mark_in_flight_unknown(
            now, reason="the process restarted while a submission was in flight")

    if persist and resolved_account:
        repository.save_broker(registry.get(BROKER_ID).broker)
        repository.save_account(registry.get(BROKER_ID).accounts[resolved_account])
        repository.save_capability(gateway.get_capabilities(), at=now)

    return ExecutionStack(
        config=config, transport=transport, gateway=gateway,
        instruments=instruments, calendar=calendar,
        orchestrator=orchestrator, repository=repository,
        service=ExecutionService(orchestrator, repository), safety=safety,
        account_id=resolved_account, recovery=recovery,
        connected=connected, connect_detail=detail)


def loop_caller(actor: str, allow_paper_orders: bool) -> Caller:
    """
    The permissions the loop runs under.

    PAPER_EXECUTION is granted only when the operator asked for it, and
    there is no branch anywhere that adds LIVE_EXECUTION — Phase 14's
    `permission_for` would demand it for a live environment, and no
    code path here can produce one.
    """
    permissions = list(Caller.read_only(actor).permissions)
    permissions.append(ExecutionPermission.DRY_RUN_EXECUTION)
    if allow_paper_orders:
        permissions.append(ExecutionPermission.PAPER_EXECUTION)
    return Caller(name=actor, permissions=tuple(permissions))
