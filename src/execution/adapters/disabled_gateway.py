"""
src/execution/adapters/disabled_gateway.py
-----------------------------------------------
The placeholder for venues that do not exist here (Phase 14, §31, §32).

WHY A REFUSING ADAPTER RATHER THAN NO ADAPTER
-------------------------------------------------
A broker that is registered but cannot trade is an ordinary
operational state: disabled by an operator, not yet configured, or
awaiting credentials. The registry, the dashboard, validation and
reconciliation all have to handle it, and the cheapest way to keep
that path exercised is a conforming gateway that refuses everything.

So the shape is here, and it refuses. `DisabledBrokerGateway` is a
complete, conforming `BrokerGateway` whose every trading method returns
`NOT_IMPLEMENTED` and whose read methods return empty. Routing,
capability checks, the UI and reconciliation can all be tested against
it, and none of them can accidentally trade through it.

WHAT MAKES IT SAFE RATHER THAN MERELY UNIMPLEMENTED
-------------------------------------------------------
Three things, and the third is the one that matters:

  1. `submit_order` refuses, always, with a reason.
  2. `get_capabilities` claims nothing, so validation refuses every
     order type before submission is even reached.
  3. Constructing one for a real-money environment raises. It cannot
     be turned into a live adapter by subclassing and flipping a flag.

NO CREDENTIALS, ANYWHERE
----------------------------
`connect()` takes no arguments and reads no environment variable, in
line with the rest of the execution layer. A stub with an empty
`api_key` field is an invitation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from src.domain.broker_models import (
    AccountSnapshot, BrokerCapability, BrokerConnectionState, BrokerHealth,
    BrokerInstrumentMapping, ExecutionEnvironment, ExecutionEvent,
    ExecutionOrder, ExecutionOrderState, ExecutionRejectCode, MarketStatus,
    PositionAccounting, PositionSnapshot,
)
from src.execution.gateway import BrokerGateway, BrokerOrderView, SubmissionAck


class DisabledBrokerGateway(BrokerGateway):
    """
    A conforming gateway that trades nothing.

    Used for a broker that is registered but cannot trade — switched
    off, not yet configured, or awaiting credentials. The name and the
    reason are carried so the dashboard can say why rather than showing
    an absence the reader has to interpret.
    """

    environment = ExecutionEnvironment.DEMO
    version = "disabled-gateway-v1"

    def __init__(self, broker_id: str, name: str = "",
                 reason: str = "no adapter is implemented for this venue",
                 environment: ExecutionEnvironment = ExecutionEnvironment.DEMO):
        if environment.is_real_money:
            raise ValueError(
                "a real-money gateway cannot be constructed in Phase 14, "
                "not even a disabled one — the environment itself is refused")
        self.broker_id = broker_id
        self.name = name or broker_id
        self.reason = reason
        self.environment = environment

    # ---------------- connection ----------------

    def connect(self) -> BrokerConnectionState:
        """
        Never connects. Reports DISCONNECTED rather than raising.

        Raising would make a registry that merely lists this broker
        fail, which would discourage listing it at all — and a venue
        the operator cannot see is worse than one they can see is off.
        """
        return BrokerConnectionState.DISCONNECTED

    def disconnect(self) -> None:
        return None

    def connection_state(self) -> BrokerConnectionState:
        return BrokerConnectionState.DISCONNECTED

    def health_check(self, now: datetime) -> BrokerHealth:
        return BrokerHealth(
            broker_id=self.broker_id, at=now,
            state=BrokerConnectionState.DISCONNECTED,
            detail=self.reason)

    # ---------------- reads: empty, never invented ----------------

    def get_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        """
        An empty snapshot, not a plausible one.

        Zeros here are honest: there is no account. Inventing a balance
        so the UI has something to render is exactly the fake-broker-data
        the spec forbids.
        """
        return AccountSnapshot(
            account_id=account_id, broker_id=self.broker_id, at=now,
            environment=self.environment,
            raw_broker_payload={"implemented": False, "reason": self.reason})

    def get_positions(self, account_id: str,
                      now: datetime) -> List[PositionSnapshot]:
        return []

    def get_open_orders(self, account_id: str) -> List[BrokerOrderView]:
        return []

    def get_order(self, broker_order_id: str) -> Optional[BrokerOrderView]:
        return None

    # ---------------- writes: always refused ----------------

    def submit_order(self, order: ExecutionOrder, now: datetime) -> SubmissionAck:
        return SubmissionAck(
            accepted=False, state=ExecutionOrderState.REJECTED,
            reject_code=ExecutionRejectCode.NOT_IMPLEMENTED,
            detail=f"{self.name}: {self.reason}", at=now)

    def cancel_order(self, broker_order_id: str, now: datetime) -> SubmissionAck:
        return SubmissionAck(
            accepted=False, state=ExecutionOrderState.UNKNOWN,
            reject_code=ExecutionRejectCode.NOT_IMPLEMENTED,
            detail=f"{self.name}: {self.reason}", at=now)

    # ---------------- events and capability ----------------

    def poll_events(self, now: datetime) -> List[ExecutionEvent]:
        return []

    def get_capabilities(self) -> BrokerCapability:
        """
        Claims nothing.

        Every flag False and every tuple empty, so capability
        validation refuses any order for this broker before submission
        is reached. An unimplemented adapter that claimed market orders
        would be stopped one layer later, and one layer later is one
        layer too many.
        """
        return BrokerCapability(
            broker_id=self.broker_id,
            asset_classes=(), times_in_force=(),
            position_accounting=PositionAccounting.NETTING,
            notes=self.reason)

    def resolve_instrument(self, instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        return None

    def market_status(self, instrument_id: str, now: datetime) -> MarketStatus:
        return MarketStatus.UNKNOWN


def planned_gateways() -> Dict[str, DisabledBrokerGateway]:
    """
    Venues named but not built. Currently: none.

    Phase 16 made the project Interactive-Brokers-only, so there is no
    second venue planned and nothing to list. The function remains
    because the registry, the dashboard and the CLI all consume it, and
    because `DisabledBrokerGateway` is still the right answer for a
    broker that is registered but cannot trade — an IBKR account an
    operator has switched off, for instance.

    Returning an empty mapping is the honest answer to "what else is
    coming": nothing is.
    """
    return {}
