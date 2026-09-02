"""
src/execution/gateway.py
-----------------------------
The broker-neutral gateway interface (Phase 14, spec §7, §3).

THE BOUNDARY
----------------
This is the line the whole phase exists to draw. Above it: strategy,
signals, portfolio, risk, order intents — none of which may know which
broker they are talking to. Below it: one adapter per venue, each free
to be as broker-shaped as that venue requires.

Every method here takes and returns canonical types from
`src.domain.broker_models`. No MT5 symbol, no IBKR Contract, no raw
status string, no SDK object crosses this interface in either
direction. An adapter that leaks one has broken the boundary even if
the code runs.

WHY SUBMISSION AND FILL ARE SEPARATE CALLS
----------------------------------------------
`submit_order` returns an acknowledgement, not a fill. Real brokers are
asynchronous: the order is accepted, works for a while, and fills later
in one or several pieces that arrive as events. An interface that
returned fills from submission would be modelling a simulator, and
every adapter written against it would have to lie.

The paper adapter fills synchronously against a bar. It still reports
through the same event path, so the orchestrator above it cannot tell
the difference — which is the property that lets a strategy move from
paper to a real venue without changing.

WHAT AN ADAPTER MAY ASSUME ABOUT CREDENTIALS
------------------------------------------------
Nothing, in this phase. There is no credential parameter anywhere in
this interface, and `connect()` takes none. A future adapter reads its
own secrets from the environment at connect time, exactly as the
existing collectors do, and never accepts them as arguments that could
be logged, persisted or serialised into a dashboard payload.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.domain.broker_models import (
    AccountSnapshot, BrokerCapability, BrokerConnectionState, BrokerHealth,
    BrokerInstrumentMapping, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionEvent, ExecutionFill,
    ExecutionOrder, ExecutionOrderState, ExecutionRejectCode, MarketStatus,
    PositionSnapshot,
)


@dataclass
class SubmissionAck:
    """
    What a broker says when it is handed an order.

    `state` is canonical, never the venue's own word. `broker_order_id`
    may be None even on success: some venues acknowledge first and
    assign an id later, and pretending otherwise would make the id look
    reliable when it is not.

    `timed_out` is the field that matters most. When it is True the
    submission may or may not have reached the venue, and the caller
    must route to UNKNOWN rather than deciding.
    """
    accepted: bool
    state: ExecutionOrderState
    broker_order_id: Optional[str] = None
    reject_code: Optional[ExecutionRejectCode] = None
    detail: str = ""
    timed_out: bool = False
    at: Optional[datetime] = None
    raw_broker_payload: Dict[str, Any] = None

    def __post_init__(self):
        if self.raw_broker_payload is None:
            self.raw_broker_payload = {}
        if self.timed_out and self.accepted:
            raise ValueError(
                "a timed-out submission cannot report acceptance — the whole "
                "point is that the outcome is unknown")


@dataclass
class BrokerOrderView:
    """
    An order as the BROKER currently describes it.

    Deliberately a different type from `ExecutionOrder`. Reconciliation
    compares our record against the venue's, and giving both the same
    type invites code that assigns one to the other without noticing
    which side it trusted.
    """
    broker_order_id: str
    instrument_id: Optional[str]
    broker_symbol: str
    side: CanonicalOrderSide
    quantity: float
    filled_quantity: float = 0.0
    average_fill_price: Optional[float] = None
    state: ExecutionOrderState = ExecutionOrderState.UNKNOWN
    order_type: CanonicalOrderType = CanonicalOrderType.MARKET
    time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    client_order_id: Optional[str] = None
    at: Optional[datetime] = None
    raw_broker_payload: Dict[str, Any] = None

    def __post_init__(self):
        if self.raw_broker_payload is None:
            self.raw_broker_payload = {}


class BrokerGateway(ABC):
    """
    One venue, behind a canonical interface.

    Implementations live in `src/execution/adapters/`. Nothing outside
    that package may import a broker SDK.
    """

    #: Stable id, used for routing and for instrument mappings.
    broker_id: str = "abstract"
    #: Which environment this adapter operates in.
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    #: Adapter version, recorded on every order it handles.
    version: str = "abstract"

    # ---------------- connection ----------------

    @abstractmethod
    def connect(self) -> BrokerConnectionState:
        """
        Establish the session. Takes no credentials, by design.

        An adapter that needs secrets reads them itself, from the
        environment, at this moment — so they never appear in a call
        site, a log line, or a serialised request.
        """

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def connection_state(self) -> BrokerConnectionState:
        ...

    @abstractmethod
    def health_check(self, now: datetime) -> BrokerHealth:
        ...

    # ---------------- account ----------------

    @abstractmethod
    def get_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        ...

    @abstractmethod
    def get_positions(self, account_id: str,
                      now: datetime) -> List[PositionSnapshot]:
        ...

    # ---------------- orders ----------------

    @abstractmethod
    def get_open_orders(self, account_id: str) -> List[BrokerOrderView]:
        ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> Optional[BrokerOrderView]:
        """
        Ask the venue about one order.

        This is the call that resolves an UNKNOWN state, so an adapter
        must implement it even when the venue makes it awkward. Without
        it the only way out of UNKNOWN is a guess.
        """

    @abstractmethod
    def submit_order(self, order: ExecutionOrder, now: datetime) -> SubmissionAck:
        """
        Hand an order to the venue. Returns an acknowledgement, not fills.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str, now: datetime) -> SubmissionAck:
        ...

    def modify_order(self, broker_order_id: str, now: datetime,
                     quantity: Optional[float] = None,
                     limit_price: Optional[float] = None,
                     stop_price: Optional[float] = None) -> SubmissionAck:
        """
        Amend a working order.

        Not abstract, because most venues in this project's future do
        not support it and forcing every adapter to write a stub that
        raises would be noise. The default refuses honestly, and an
        adapter that can amend overrides it AND declares
        `supports_order_modification`.
        """
        return SubmissionAck(
            accepted=False, state=ExecutionOrderState.WORKING,
            reject_code=ExecutionRejectCode.NOT_IMPLEMENTED,
            detail=f"{self.broker_id} does not implement order modification",
            at=now)

    # ---------------- events ----------------

    @abstractmethod
    def poll_events(self, now: datetime) -> List[ExecutionEvent]:
        """
        Drain whatever the venue has reported since the last call.

        Polling rather than callbacks, because this project has no
        persistent runtime to receive a push. A streaming adapter
        buffers internally and drains here, so the orchestrator's
        contract is the same either way.
        """

    def subscribe_order_events(self, handler) -> None:
        """
        Register a push handler, where a venue supports one.

        Default is a no-op: an adapter without streaming is not broken,
        it simply reports through `poll_events`. Declared so a
        streaming adapter has somewhere to put it rather than inventing
        its own entry point.
        """
        return None

    def subscribe_account_events(self, handler) -> None:
        return None

    def subscribe_market_data(self, instrument_ids: Sequence[str], handler) -> None:
        return None

    # ---------------- instruments and capability ----------------

    @abstractmethod
    def get_capabilities(self) -> BrokerCapability:
        ...

    @abstractmethod
    def resolve_instrument(self, instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        """Canonical id to this venue's symbol, or None when unmapped."""

    @abstractmethod
    def market_status(self, instrument_id: str, now: datetime) -> MarketStatus:
        ...

    def get_trading_hours(self, instrument_id: str) -> str:
        """Human-readable session description, when the adapter knows one."""
        mapping = self.resolve_instrument(instrument_id)
        return mapping.trading_hours if mapping else ""

    # ---------------- reconciliation support ----------------

    def reconcile_orders(self, account_id: str) -> List[BrokerOrderView]:
        """
        Everything the venue believes is open. Defaults to open orders.

        Separate from `get_open_orders` because some venues expose a
        richer reconciliation endpoint, and an adapter that has one
        should be able to use it here without changing the ordinary
        read path.
        """
        return self.get_open_orders(account_id)

    def reconcile_positions(self, account_id: str,
                            now: datetime) -> List[PositionSnapshot]:
        return self.get_positions(account_id, now)

    def reconcile_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        return self.get_account(account_id, now)
