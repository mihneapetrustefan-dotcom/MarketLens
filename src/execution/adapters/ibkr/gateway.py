"""
src/execution/adapters/ibkr/gateway.py
-------------------------------------------
The IBKR broker gateway (Phase 15, spec §3, §4, §9, §10, §19, §24,
§29, §30, §31, §46, §47, §49).

WHAT THIS IS
----------------
A `BrokerGateway` — the Phase 14 interface, implemented for Interactive
Brokers. It is the only class in the repository that knows both
dialects, and it is deliberately thin: contract resolution lives in
`contracts.py`, translation in `mapper.py`, the wire in
`transport.py`, and failure classification in `errors.py`.

The test of whether Phase 14's boundary was drawn correctly is that
this file needed no change to anything above it. It did not.

THE TWO GATES BEFORE ANY ORDER
----------------------------------
Connecting is not permission to trade (spec §46). `submit_order`
refuses unless BOTH `IBKR_ENABLED` and `IBKR_PAPER_ORDERING_ENABLED`
are set, and the environment is paper. An IBKR session existing is
never a reason for an order to exist.

TIMEOUTS ARE THE WHOLE GAME
-------------------------------
IBKR accepting an order and our not hearing about it is the failure
this adapter is shaped around. A timeout returns `SubmissionAck(
timed_out=True)`, which Phase 14 turns into `UNKNOWN` — never FAILED,
never retried. `get_order` is what resolves it, by asking IBKR, and it
matches on the client order id when the IBKR order id never arrived.

EVENTS ARE POLLED, NOT PUSHED
---------------------------------
IBKR's Client Portal offers a websocket, and this repository has no
persistent runtime to hold one. So `poll_events` diffs the venue's
current state against what we last saw and synthesises the events that
must have happened in between. That is honest for a batch scheduler,
and it is stated rather than dressed up as streaming — a websocket
transport would fill the same buffer, and nothing above would change.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.backtest.calendar import MarketCalendar
from src.domain.broker_models import (
    AccountSnapshot, BrokerCapability, BrokerConnectionState, BrokerHealth,
    BrokerInstrumentMapping, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionEvent,
    ExecutionEventType, ExecutionFill, ExecutionOrder, ExecutionOrderState,
    ExecutionRejectCode, MarketStatus, PositionAccounting, PositionSnapshot,
)
from src.execution.adapters.ibkr.config import IBKRConfig
from src.execution.adapters.ibkr.contracts import (
    ContractQuery, ContractResolver, conid_of,
)
from src.execution.adapters.ibkr.errors import (
    IBKRError, IBKRErrorCategory, explain as explain_error, scrub,
)
from src.execution.adapters.ibkr.mapper import (
    SNAPSHOT_FIELDS, account_from_ibkr, fill_from_execution, order_to_ibkr,
    order_view_from_ibkr, position_from_ibkr, quote_from_ibkr,
    state_from_ibkr,
)
from src.execution.adapters.ibkr.transport import AuthStatus, IBKRTransport
from src.execution.gateway import BrokerGateway, BrokerOrderView, SubmissionAck
from src.execution.instruments import InstrumentRegistry

#: How old a quote may be and still back an order. IBKR snapshots are
#: as live as the account's market-data permissions allow, which may
#: be delayed — so this is checked rather than assumed.
DEFAULT_QUOTE_MAX_AGE_SECONDS = 60.0


class MarketDataAvailability(str, Enum):
    """
    What market data this account actually has (spec §18).

    Modelled explicitly because an IBKR account does NOT automatically
    carry every subscription, and a delayed quote presented as live is
    the kind of error that only shows up in the fill price.
    """
    AVAILABLE = "available"
    DELAYED = "delayed"
    RESTRICTED = "restricted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def is_tradeable(self) -> bool:
        """
        Only genuinely live data backs an order.

        DELAYED is excluded deliberately. A delayed quote is fine for a
        dashboard and wrong for a limit price, and the difference is
        invisible in the number itself.
        """
        return self is MarketDataAvailability.AVAILABLE


@dataclass
class IBKRQuote:
    """One canonical quote, with both timestamps kept apart."""
    conid: str
    instrument_id: Optional[str] = None
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    volume: Optional[float] = None
    broker_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    availability: MarketDataAvailability = MarketDataAvailability.UNKNOWN

    def age_seconds(self, now: datetime) -> Optional[float]:
        reference = self.broker_at or self.received_at
        if reference is None:
            return None
        return (now - reference).total_seconds()

    def is_fresh(self, now: datetime,
                 max_age: float = DEFAULT_QUOTE_MAX_AGE_SECONDS) -> bool:
        age = self.age_seconds(now)
        if age is None:
            # Unknown age is not freshness. A quote whose age cannot be
            # established must not back an order.
            return False
        return 0 <= age <= max_age

    @property
    def reference_price(self) -> Optional[float]:
        """Mid where both sides exist, otherwise last. Never invented."""
        return self.mid if self.mid is not None else self.last


class IBKRGateway(BrokerGateway):
    """
    Interactive Brokers, behind the Phase 14 interface.

    Holds no credential: the Client Portal Gateway authenticates the
    human and this class inherits that session through the transport.
    `connect()` therefore takes no arguments, exactly as the Phase 14
    contract requires.
    """

    broker_id = "ibkr"
    environment = ExecutionEnvironment.PAPER
    version = "ibkr-gateway-v1"

    def __init__(self, config: IBKRConfig, transport: IBKRTransport,
                 registry: InstrumentRegistry,
                 calendar: Optional[MarketCalendar] = None,
                 quote_max_age_seconds: float = DEFAULT_QUOTE_MAX_AGE_SECONDS):
        # Refuse a live configuration before anything else happens.
        if config.environment.is_real_money:
            raise ValueError(
                "IBKRGateway cannot be constructed for a real-money "
                "environment in Phase 15")
        self.config = config
        self.transport = transport
        self.registry = registry
        self.calendar = calendar
        self.broker_id = config.broker_id
        self.quote_max_age_seconds = quote_max_age_seconds

        self.resolver = ContractResolver(transport, broker_id=self.broker_id)
        self._state = BrokerConnectionState.DISCONNECTED
        self._auth = AuthStatus()
        self._attempts = 0
        self._reconnects = 0
        self._last_heartbeat: Optional[datetime] = None
        self._last_error: Optional[IBKRError] = None
        self._latencies: List[float] = []

        #: Events synthesised since the last drain.
        self._pending: List[ExecutionEvent] = []
        self._seq = itertools.count(1)
        #: our order id -> IBKR order id
        self._broker_ids: Dict[str, str] = {}
        #: IBKR order id -> the last state we reported, so a poll only
        #: emits an event when something actually changed.
        self._last_seen: Dict[str, ExecutionOrderState] = {}
        #: execution ids already turned into fills
        self._seen_executions: set = set()
        self._quotes: Dict[str, IBKRQuote] = {}

    # ================================================================
    # Connection (spec §9, §10)
    # ================================================================

    def connect(self) -> BrokerConnectionState:
        """
        Establish the session, with bounded retries.

        Takes no credentials — the gateway holds them. What this does
        is ASK whether that gateway is authenticated, and translate the
        answer into a canonical connection state.
        """
        if not self.config.enabled:
            self._state = BrokerConnectionState.DISCONNECTED
            return self._state

        self._state = BrokerConnectionState.CONNECTING
        delay = self.config.backoff_seconds
        for attempt in range(1, self.config.max_retries + 1):
            self._attempts = attempt
            status = self.transport.is_authenticated()
            self._auth = status

            if status.competing:
                # Another session took the account. Retrying cannot
                # help and would fight the other session, so it stops.
                self._state = BrokerConnectionState.AUTH_FAILED
                return self._state
            if status.authenticated and status.connected:
                self._state = BrokerConnectionState.CONNECTED
                self._last_heartbeat = datetime.now(timezone.utc)
                if attempt > 1:
                    self._reconnects += 1
                return self._state
            if status.authenticated and not status.connected:
                self._state = BrokerConnectionState.DEGRADED
            else:
                self._state = BrokerConnectionState.AUTH_FAILED
                # An unauthenticated gateway needs a human at a browser.
                # Retrying is pointless and looks like a hang.
                return self._state

            delay = min(delay * 2, self.config.max_backoff_seconds)
            if not self.config.reconnect_enabled:
                break

        return self._state

    def disconnect(self) -> None:
        self.transport.close()
        self._state = BrokerConnectionState.DISCONNECTED

    def connection_state(self) -> BrokerConnectionState:
        return self._state

    def heartbeat(self) -> bool:
        """Keep the Client Portal session alive; it lapses when idle."""
        alive = self.transport.keepalive()
        if alive:
            self._last_heartbeat = datetime.now(timezone.utc)
            if self._state is not BrokerConnectionState.CONNECTED:
                self._state = BrokerConnectionState.CONNECTED
                self._reconnects += 1
        elif self._state is BrokerConnectionState.CONNECTED:
            self._state = BrokerConnectionState.DEGRADED
        return alive

    def health_check(self, now: datetime) -> BrokerHealth:
        status = self.transport.is_authenticated()
        self._auth = status
        if status.competing:
            state = BrokerConnectionState.AUTH_FAILED
            detail = "another session has taken this account"
        elif status.usable:
            state = BrokerConnectionState.CONNECTED
            detail = f"authenticated via {self.transport.name}"
        elif status.authenticated:
            state = BrokerConnectionState.DEGRADED
            detail = "authenticated but the gateway is not connected to IBKR"
        else:
            state = BrokerConnectionState.AUTH_FAILED
            detail = "the gateway session is not authenticated"

        if not self.config.enabled:
            state = BrokerConnectionState.DISCONNECTED
            detail = "IBKR is disabled by configuration"

        self._state = state
        latency = (sum(self._latencies) / len(self._latencies)
                   if self._latencies else None)
        return BrokerHealth(
            broker_id=self.broker_id, at=now, state=state,
            latency_ms=latency, detail=detail,
            consecutive_failures=0 if state.can_submit else self._attempts)

    def health_detail(self, now: datetime) -> Dict[str, Any]:
        """The fuller picture the dashboard and CLI show (spec §41)."""
        return {
            "broker_id": self.broker_id,
            "transport": self.transport.name,
            "connection": self._state.value,
            "authenticated": self._auth.authenticated,
            "gateway_connected": self._auth.connected,
            "competing_session": self._auth.competing,
            "last_heartbeat_at": (self._last_heartbeat.isoformat()
                                  if self._last_heartbeat else None),
            "connection_attempts": self._attempts,
            "reconnects": self._reconnects,
            "ordering_enabled": self.config.ordering_enabled,
            "can_submit_orders": self.config.can_submit_orders,
            "environment": self.config.environment.value,
            "live_execution": False,
            "last_error": (self._last_error.as_dict()
                           if self._last_error else None),
            "cached_contracts": len(self.resolver.cached),
            "known_orders": len(self._broker_ids),
        }

    # ================================================================
    # Account and positions (spec §11, §12, §13)
    # ================================================================

    def _account(self) -> str:
        account = self.config.account_id
        if account:
            return account
        discovered = self.transport.accounts()
        if not discovered:
            raise IBKRError(
                category=IBKRErrorCategory.PERMISSION_ERROR,
                message="IBKR reported no accounts for this session",
                endpoint="/iserver/accounts")
        return str(discovered[0].get("accountId") or discovered[0].get("id"))

    def discover_accounts(self) -> List[str]:
        return [str(a.get("accountId") or a.get("id"))
                for a in self.transport.accounts()]

    def get_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        summary = self.transport.account_summary(account_id or self._account())
        return account_from_ibkr(summary, account_id or self._account(),
                                 self.broker_id, now)

    def get_positions(self, account_id: str,
                      now: datetime) -> List[PositionSnapshot]:
        out: List[PositionSnapshot] = []
        for payload in self.transport.positions(account_id or self._account()):
            conid = str(payload.get("conid") or "")
            out.append(position_from_ibkr(
                payload, account_id, self.broker_id, now,
                instrument_id=self._instrument_for(conid)))
        return out

    def _instrument_for(self, conid: str) -> Optional[str]:
        """
        IBKR conid back to our canonical instrument id.

        The reverse direction matters as much as the forward one: a
        position or execution arrives naming a conid, and it has to be
        attributed to the right instrument before it can touch a book.
        """
        for mapping in self.registry.for_broker(self.broker_id):
            if conid_of(mapping) == conid:
                return mapping.canonical_instrument_id
        return None

    # ================================================================
    # Contracts (spec §14, §15, §16)
    # ================================================================

    def resolve_contract(self, instrument_id: str, symbol: str,
                         sec_type: str = "STK", currency: str = "USD",
                         primary_exchange: Optional[str] = None,
                         persist: bool = True):
        """
        Resolve and register one instrument, or report the ambiguity.

        On success the mapping is written into the Phase 14 registry,
        so everything downstream reads it the same way it reads a paper
        mapping — the core never learns that IBKR was involved.
        """
        resolution = self.resolver.resolve(ContractQuery(
            symbol=symbol, sec_type=sec_type, currency=currency,
            primary_exchange=primary_exchange))
        if resolution.ok and persist:
            self.registry.register(
                resolution.contract.as_mapping(instrument_id, self.broker_id))
        return resolution

    def resolve_instrument(self, instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        return self.registry.get(self.broker_id, instrument_id)

    def _conid_for(self, instrument_id: str) -> Optional[str]:
        return conid_of(self.registry.get(self.broker_id, instrument_id))

    # ================================================================
    # Market data (spec §17, §18, §39)
    # ================================================================

    def quote(self, instrument_id: str, now: datetime) -> Optional[IBKRQuote]:
        conid = self._conid_for(instrument_id)
        if conid is None:
            return None
        try:
            payloads = self.transport.market_snapshot(
                [conid], SNAPSHOT_FIELDS)
        except IBKRError as error:
            self._last_error = error
            availability = (
                MarketDataAvailability.RESTRICTED
                if error.category is IBKRErrorCategory.MARKET_DATA_ERROR
                else MarketDataAvailability.UNAVAILABLE)
            quote = IBKRQuote(conid=conid, instrument_id=instrument_id,
                              received_at=now, availability=availability)
            self._quotes[conid] = quote
            return quote

        if not payloads:
            quote = IBKRQuote(conid=conid, instrument_id=instrument_id,
                              received_at=now,
                              availability=MarketDataAvailability.UNAVAILABLE)
            self._quotes[conid] = quote
            return quote

        raw = quote_from_ibkr(payloads[0], now)
        quote = IBKRQuote(
            conid=conid, instrument_id=instrument_id, last=raw["last"],
            bid=raw["bid"], ask=raw["ask"], mid=raw["mid"],
            volume=raw["volume"], broker_at=raw["broker_at"],
            received_at=now,
            availability=(MarketDataAvailability.AVAILABLE
                          if raw["last"] is not None or raw["mid"] is not None
                          else MarketDataAvailability.UNAVAILABLE))
        self._quotes[conid] = quote
        return quote

    def market_status(self, instrument_id: str, now: datetime) -> MarketStatus:
        """
        Session state from the canonical calendar (spec §38).

        Reuses the Phase 12 calendar rather than hardcoding US hours,
        and returns UNKNOWN when it cannot answer — which is different
        from CLOSED, and is treated as not-tradeable by the Phase 14
        validator either way.
        """
        if self._conid_for(instrument_id) is None:
            # No resolved IBKR contract. We do not know this venue's
            # session for an instrument this venue cannot identify, and
            # claiming OPEN would let it past the session gate to fail
            # later on a missing contract instead.
            return MarketStatus.UNKNOWN
        if self.calendar is None:
            return MarketStatus.UNKNOWN
        if not self.calendar.has_data(instrument_id):
            return MarketStatus.UNKNOWN
        return (MarketStatus.OPEN
                if self.calendar.is_open(instrument_id, now.date())
                else MarketStatus.CLOSED)

    # ================================================================
    # Orders (spec §19, §20, §24, §29, §30, §31)
    # ================================================================

    def get_capabilities(self) -> BrokerCapability:
        """
        Only what this adapter actually implements.

        Bracket, OCO and trailing stops are IBKR features that this
        adapter does not map, so they are declared False — a capability
        claimed but unmapped would let an order through validation to
        fail at the venue instead.
        """
        return BrokerCapability(
            broker_id=self.broker_id,
            supports_market_orders=True,
            supports_limit_orders=True,
            supports_stop_orders=True,
            supports_stop_limit_orders=True,
            supports_trailing_stop=False,
            supports_bracket_orders=False,
            supports_oco=False,
            supports_order_modification=False,
            supports_partial_fills=True,
            supports_fractional_quantity=False,
            supports_shorting=True,
            supports_margin=True,
            supports_extended_hours=False,
            supports_streaming=False,
            supports_realtime_quotes=True,
            asset_classes=("stock", "etf"),
            times_in_force=(CanonicalTimeInForce.DAY, CanonicalTimeInForce.GTC,
                            CanonicalTimeInForce.IOC),
            position_accounting=PositionAccounting.NETTING,
            rate_limit_per_minute=self.config.max_requests_per_minute,
            notes=(f"Interactive Brokers via {self.transport.name}; "
                   f"paper only, ordering "
                   f"{'enabled' if self.config.ordering_enabled else 'DISABLED'}"))

    def submit_order(self, order: ExecutionOrder, now: datetime) -> SubmissionAck:
        """
        Send an order to IBKR paper (spec §19, §46).

        Two gates first, then the conid, then the venue. A timeout
        produces `timed_out=True` and nothing else — Phase 14 turns
        that into UNKNOWN and never retries it.
        """
        if not self.config.enabled:
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=ExecutionRejectCode.BROKER_DISABLED,
                detail="IBKR is disabled by configuration", at=now)

        if not self.config.can_submit_orders:
            # The manual safety gate of spec §46. A connection existing
            # is not a reason for an order to exist.
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=ExecutionRejectCode.EXECUTION_DISABLED,
                detail=("IBKR paper ordering is not enabled. Set "
                        "IBKR_PAPER_ORDERING_ENABLED=true after confirming "
                        "the account is a paper account."),
                at=now)

        if not self._state.can_submit:
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=ExecutionRejectCode.BROKER_DISCONNECTED,
                detail=f"IBKR connection is {self._state.value}", at=now)

        conid = self._conid_for(order.instrument_id)
        if conid is None:
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
                detail=(f"{order.instrument_id} has no resolved IBKR contract. "
                        f"Resolve it before trading it."),
                at=now)

        account_id = order.account_id or self._account()
        payload = order_to_ibkr(order, conid, account_id)

        try:
            response = self.transport.place_order(account_id, payload)
        except IBKRError as error:
            self._last_error = error
            if error.category is IBKRErrorCategory.TIMEOUT:
                # The dangerous one. IBKR may hold this order.
                return SubmissionAck(
                    accepted=False, state=ExecutionOrderState.UNKNOWN,
                    timed_out=True, at=now,
                    detail=explain_error(error),
                    raw_broker_payload={"category": error.category.value})
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=error.reject_code, detail=explain_error(error),
                at=now, raw_broker_payload=error.as_dict())

        # IBKR often answers a submission with a QUESTION rather than an
        # acknowledgement. Left unanswered, the order never arrives.
        response = self._answer_confirmations(response, now)

        status = str(response.get("order_status") or response.get("status") or "")
        broker_order_id = str(response.get("order_id")
                              or response.get("orderId") or "") or None
        if broker_order_id:
            self._broker_ids[order.order_id] = broker_order_id

        if status.lower() in ("rejected", "cancelled", "canceled"):
            return SubmissionAck(
                accepted=False, state=ExecutionOrderState.REJECTED,
                broker_order_id=broker_order_id,
                reject_code=ExecutionRejectCode.ADAPTER_ERROR,
                detail=str(response.get("text") or f"IBKR returned {status}"),
                at=now, raw_broker_payload=scrub(response))

        if not broker_order_id:
            # Accepted-looking but unidentifiable. Treating this as
            # success would leave an order we could never query.
            return SubmissionAck(
                accepted=False, state=ExecutionOrderState.UNKNOWN,
                timed_out=True, at=now,
                detail="IBKR accepted the order but returned no order id",
                raw_broker_payload=scrub(response))

        return SubmissionAck(
            accepted=True,
            state=(state_from_ibkr(status) if status
                   else ExecutionOrderState.ACKNOWLEDGED),
            broker_order_id=broker_order_id, at=now,
            raw_broker_payload=scrub(response))

    def _answer_confirmations(self, response: Dict[str, Any], now: datetime,
                              limit: int = 3) -> Dict[str, Any]:
        """
        Answer IBKR's order-confirmation questions.

        Bounded at three, because a venue that keeps asking is a loop
        rather than a conversation. Every question and answer is
        recorded on the ack's raw payload, so a confirmed order shows
        what was confirmed.
        """
        answered = 0
        while answered < limit and isinstance(response, dict) and response.get("id"):
            reply_id = str(response["id"])
            answered += 1
            try:
                response = self.transport.reply(reply_id, confirmed=True)
            except (IBKRError, NotImplementedError) as error:
                self._last_error = (error if isinstance(error, IBKRError)
                                    else None)
                return {"order_status": "Rejected",
                        "text": f"IBKR asked for confirmation and this "
                                f"transport cannot answer: {error}"}
        return response if isinstance(response, dict) else {}

    def cancel_order(self, broker_order_id: str, now: datetime) -> SubmissionAck:
        """
        Cancel, mapping every IBKR outcome (spec §31).

        "Already filled" and "not found" are distinct answers and both
        are real. Neither is an error to swallow: the first means the
        position exists, the second that reconciliation should look.
        """
        try:
            response = self.transport.cancel_order(self._account(),
                                                   broker_order_id)
        except IBKRError as error:
            self._last_error = error
            if error.ibkr_code == 10148:
                return SubmissionAck(
                    accepted=False, state=ExecutionOrderState.FILLED,
                    broker_order_id=broker_order_id,
                    reject_code=ExecutionRejectCode.ADAPTER_ERROR,
                    detail="the order was already filled and cannot be cancelled",
                    at=now)
            if error.ibkr_code == 10147:
                return SubmissionAck(
                    accepted=False, state=ExecutionOrderState.UNKNOWN,
                    broker_order_id=broker_order_id,
                    reject_code=ExecutionRejectCode.ADAPTER_ERROR,
                    detail="IBKR has no record of this order to cancel",
                    at=now)
            return SubmissionAck(
                accepted=False, state=ExecutionOrderState.UNKNOWN,
                broker_order_id=broker_order_id,
                reject_code=error.reject_code, detail=explain_error(error),
                at=now)

        return SubmissionAck(
            accepted=True, state=ExecutionOrderState.CANCEL_REQUESTED,
            broker_order_id=broker_order_id, at=now,
            detail=str(response.get("msg") or ""),
            raw_broker_payload=scrub(response))

    def get_open_orders(self, account_id: str) -> List[BrokerOrderView]:
        views: List[BrokerOrderView] = []
        for payload in self.transport.live_orders():
            conid = str(payload.get("conid") or "")
            views.append(order_view_from_ibkr(
                payload, instrument_id=self._instrument_for(conid)))
        return views

    def get_order(self, broker_order_id: str) -> Optional[BrokerOrderView]:
        """
        Ask IBKR about one order — the call that resolves UNKNOWN.

        Falls back to scanning live orders by client order id, because
        the case this exists for is precisely the one where no IBKR
        order id ever came back.
        """
        try:
            payload = self.transport.order_status(broker_order_id)
        except IBKRError as error:
            self._last_error = error
            raise

        if payload is None:
            match = self._find_by_client_id(broker_order_id)
            if match is not None:
                return match
            return None

        conid = str(payload.get("conid") or "")
        return order_view_from_ibkr(
            payload, instrument_id=self._instrument_for(conid))

    def _find_by_client_id(self, client_order_id: str) -> Optional[BrokerOrderView]:
        """
        Find an order by the id WE assigned.

        After a timeout there may be no IBKR order id at all, and the
        client order id — derived from the idempotency key, so
        identical on a retry — is the only handle that survives.
        """
        for view in self.get_open_orders(self._account()):
            if view.client_order_id and view.client_order_id == client_order_id:
                return view
        return None

    # ================================================================
    # Events (spec §47, §48, §49)
    # ================================================================

    def poll_events(self, now: datetime) -> List[ExecutionEvent]:
        """
        Diff the venue against what we last saw and emit the difference.

        Polling because this project has no persistent runtime to hold
        a websocket. Emitting only CHANGES is what keeps a poll from
        producing the same event on every tick — which would look like
        a duplicate to everything downstream and be silently discarded,
        making real changes invisible too.
        """
        drained, self._pending = self._pending, []

        try:
            for view in self.get_open_orders(self._account()):
                previous = self._last_seen.get(view.broker_order_id)
                if previous is view.state:
                    continue
                self._last_seen[view.broker_order_id] = view.state
                drained.append(self._event_for(view, now))
        except IBKRError as error:
            self._last_error = error

        return drained

    def _event_for(self, view: BrokerOrderView,
                   now: datetime) -> ExecutionEvent:
        kind = {
            ExecutionOrderState.ACKNOWLEDGED: ExecutionEventType.ORDER_ACKNOWLEDGED,
            ExecutionOrderState.WORKING: ExecutionEventType.ORDER_UPDATED,
            ExecutionOrderState.PARTIALLY_FILLED: ExecutionEventType.ORDER_PARTIALLY_FILLED,
            ExecutionOrderState.FILLED: ExecutionEventType.ORDER_FILLED,
            ExecutionOrderState.CANCELLED: ExecutionEventType.ORDER_CANCELLED,
            ExecutionOrderState.REJECTED: ExecutionEventType.ORDER_REJECTED,
            ExecutionOrderState.EXPIRED: ExecutionEventType.ORDER_EXPIRED,
        }.get(view.state, ExecutionEventType.ORDER_UPDATED)

        our_order_id = next(
            (ours for ours, theirs in self._broker_ids.items()
             if theirs == view.broker_order_id), None)

        return ExecutionEvent(
            event_id=f"ibkr-ev-{next(self._seq):08d}",
            event_type=kind, at=view.at or now, received_at=now,
            source=self.broker_id, broker_id=self.broker_id,
            order_id=our_order_id, broker_order_id=view.broker_order_id,
            instrument_id=view.instrument_id,
            payload={"status": view.state.value,
                     "filled": view.filled_quantity})

    def collect_fills(self, orders: Dict[str, ExecutionOrder],
                      days: int = 1) -> List[ExecutionFill]:
        """
        Turn IBKR executions into canonical fills (spec §26, §36).

        Deduplicated on IBKR's own execution id, never on the visible
        fields: a venue filling 100 as two 50s at one price produces
        two executions identical in everything but that id, and
        discarding one would lose a real fill.
        """
        produced: List[ExecutionFill] = []
        try:
            executions = self.transport.executions(days)
        except IBKRError as error:
            self._last_error = error
            return produced

        by_broker_id = {theirs: ours for ours, theirs in self._broker_ids.items()}

        for payload in executions:
            execution_id = str(payload.get("execution_id")
                               or payload.get("execid") or "")
            if not execution_id or execution_id in self._seen_executions:
                continue

            broker_order_id = str(payload.get("orderId") or "")
            our_order_id = by_broker_id.get(broker_order_id)
            if our_order_id is None:
                # An execution for an order we have no record of. Not
                # something to invent an order for — reconciliation
                # reports it as an unknown broker order.
                continue
            order = orders.get(our_order_id)
            if order is None:
                continue

            self._seen_executions.add(execution_id)
            conid = str(payload.get("conid") or "")
            produced.append(fill_from_execution(
                payload, order, instrument_id=self._instrument_for(conid)))

        return produced

    # ================================================================
    # Reconciliation support (spec §33-§37)
    # ================================================================

    def reconcile_orders(self, account_id: str) -> List[BrokerOrderView]:
        """
        Everything IBKR knows about, not only what is working.

        An order that just went terminal at IBKR would otherwise look
        simply missing, which is a different and far more alarming
        finding than "it finished".
        """
        return self.get_open_orders(account_id or self._account())

    def restore_known_orders(self, orders: Sequence[ExecutionOrder]) -> int:
        """
        Rebuild the id map after a restart (spec §37, §62).

        Without this the adapter cannot attribute an IBKR execution to
        our order, so every fill after a restart would look like it
        belonged to an order we never placed.
        """
        restored = 0
        for order in orders:
            if order.broker_id != self.broker_id or not order.broker_order_id:
                continue
            self._broker_ids[order.order_id] = order.broker_order_id
            self._last_seen[order.broker_order_id] = order.state
            restored += 1
        return restored

    def restore_seen_executions(self, execution_ids: Sequence[str]) -> int:
        """
        Restore the execution dedup set after a restart.

        Reconnecting is exactly what makes IBKR replay recent
        executions, so this is the redelivery that actually happens.
        """
        before = len(self._seen_executions)
        self._seen_executions.update(i for i in execution_ids if i)
        return len(self._seen_executions) - before
