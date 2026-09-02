"""
src/execution/adapters/paper_gateway.py
--------------------------------------------
The paper broker adapter (Phase 14, spec §30).

WHAT THIS IS
----------------
The first real implementation of `BrokerGateway`, and the proof that
the interface is usable rather than aspirational. It wraps the Phase 13
`PaperExecutor` unchanged: no execution logic is reimplemented here,
because a second copy of fill or cost logic is exactly how paper and
simulated results start disagreeing for reasons nobody can trace.

The adapter's whole job is translation:

    canonical ExecutionOrder  ->  Phase 13 PaperOrder
    Phase 13 PaperFill        ->  canonical ExecutionFill
    Phase 13 PaperOrderState  ->  canonical ExecutionOrderState
    Phase 13 rejection reason ->  canonical ExecutionRejectCode

WHY THE STATE MAPS ARE EXPLICIT
-----------------------------------
Both enums spell several members identically, and it would be shorter
to map them by name. That shortcut is a trap: the two vocabularies are
allowed to diverge — Phase 13 will never have SUBMITTING, ACKNOWLEDGED
or UNKNOWN, and a future rename on either side would silently produce
wrong states rather than an error. Explicit dictionaries fail loudly.

WHAT IS SIMULATED AND SAID SO
---------------------------------
Paper fills happen synchronously against a cached bar. A real venue
would acknowledge, work, and fill later. The adapter still reports
through `poll_events`, so the orchestrator above cannot tell the
difference — but nothing here claims the latency, the queue position or
the partial-fill behaviour of a real venue, and `get_capabilities`
declares only what the paper executor genuinely does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.backtest.calendar import MarketCalendar
from src.domain.broker_models import (
    AccountSnapshot, BrokerCapability, BrokerConnectionState, BrokerHealth,
    BrokerInstrumentMapping, CanonicalOrderSide, CanonicalOrderType,
    CanonicalTimeInForce, ExecutionEnvironment, ExecutionEvent,
    ExecutionEventType, ExecutionFill, ExecutionOrder, ExecutionOrderState,
    ExecutionRejectCode, MarginSnapshot, MarketStatus, PositionAccounting,
    PositionSnapshot,
)
from src.domain.paper_models import (
    HealthState, OrderSide, PaperFill, PaperOrder, PaperOrderState,
    PaperOrderType, PaperRejectReason, TimeInForce,
)
from src.execution.gateway import BrokerGateway, BrokerOrderView, SubmissionAck
from src.execution.instruments import InstrumentRegistry
from src.paper.executor import PaperExecutor

#: Phase 13 order state -> canonical state. Written out rather than
#: derived from the spelling, so a rename on either side is a KeyError
#: at import time instead of a wrong state at runtime.
PAPER_TO_CANONICAL: Dict[PaperOrderState, ExecutionOrderState] = {
    PaperOrderState.CREATED: ExecutionOrderState.CREATED,
    PaperOrderState.VALIDATING: ExecutionOrderState.VALIDATING,
    PaperOrderState.ACCEPTED: ExecutionOrderState.ACKNOWLEDGED,
    PaperOrderState.SUBMITTED: ExecutionOrderState.SUBMITTED,
    PaperOrderState.PARTIALLY_FILLED: ExecutionOrderState.PARTIALLY_FILLED,
    PaperOrderState.FILLED: ExecutionOrderState.FILLED,
    PaperOrderState.CANCEL_REQUESTED: ExecutionOrderState.CANCEL_REQUESTED,
    PaperOrderState.CANCELLED: ExecutionOrderState.CANCELLED,
    PaperOrderState.REJECTED: ExecutionOrderState.REJECTED,
    PaperOrderState.EXPIRED: ExecutionOrderState.EXPIRED,
}

#: Phase 13 rejection reason -> canonical code.
PAPER_REJECT_TO_CANONICAL: Dict[PaperRejectReason, ExecutionRejectCode] = {
    PaperRejectReason.MARKET_CLOSED: ExecutionRejectCode.MARKET_CLOSED,
    PaperRejectReason.STALE_DATA: ExecutionRejectCode.STALE_DATA,
    PaperRejectReason.NO_PRICE: ExecutionRejectCode.NO_PRICE,
    PaperRejectReason.INSUFFICIENT_CASH: ExecutionRejectCode.INSUFFICIENT_BUYING_POWER,
    PaperRejectReason.INVALID_QUANTITY: ExecutionRejectCode.INVALID_QUANTITY,
    PaperRejectReason.INVALID_PRICE: ExecutionRejectCode.INVALID_PRICE,
    PaperRejectReason.UNKNOWN_INSTRUMENT: ExecutionRejectCode.NO_INSTRUMENT_MAPPING,
    PaperRejectReason.RISK_REJECTED: ExecutionRejectCode.RISK_REJECTED,
    PaperRejectReason.SHORTING_DISABLED: ExecutionRejectCode.SHORTING_NOT_SUPPORTED,
    PaperRejectReason.RATE_LIMITED: ExecutionRejectCode.RATE_LIMITED,
    PaperRejectReason.CIRCUIT_BREAKER: ExecutionRejectCode.EMERGENCY_STOP,
    PaperRejectReason.ACCOUNT_NOT_ACTIVE: ExecutionRejectCode.ACCOUNT_DISABLED,
    PaperRejectReason.LIQUIDITY_CAP: ExecutionRejectCode.INVALID_QUANTITY,
    PaperRejectReason.DUPLICATE: ExecutionRejectCode.DUPLICATE_INTENT,
    PaperRejectReason.SAFE_MODE: ExecutionRejectCode.EMERGENCY_STOP,
}

CANONICAL_TO_PAPER_TYPE: Dict[CanonicalOrderType, PaperOrderType] = {
    CanonicalOrderType.MARKET: PaperOrderType.MARKET,
    CanonicalOrderType.LIMIT: PaperOrderType.LIMIT,
    CanonicalOrderType.STOP: PaperOrderType.STOP,
    CanonicalOrderType.STOP_LIMIT: PaperOrderType.STOP_LIMIT,
}

#: Note the absence of FOK. Phase 13 has no fill-or-kill, so the paper
#: adapter must not claim one — the capability declaration below omits
#: it too, and validation refuses it before an order is built. A map
#: entry that silently downgraded FOK to IOC would execute something
#: other than what was asked for.
CANONICAL_TO_PAPER_TIF: Dict[CanonicalTimeInForce, TimeInForce] = {
    CanonicalTimeInForce.DAY: TimeInForce.DAY,
    CanonicalTimeInForce.GTC: TimeInForce.GTC,
    CanonicalTimeInForce.IOC: TimeInForce.IOC,
}

HEALTH_TO_CONNECTION: Dict[HealthState, BrokerConnectionState] = {
    HealthState.HEALTHY: BrokerConnectionState.CONNECTED,
    HealthState.DEGRADED: BrokerConnectionState.DEGRADED,
    HealthState.STALE: BrokerConnectionState.DEGRADED,
    HealthState.FAILED: BrokerConnectionState.ERROR,
    HealthState.PAUSED: BrokerConnectionState.DISCONNECTED,
}


class PaperBrokerGateway(BrokerGateway):
    """
    `BrokerGateway` over the Phase 13 paper executor.

    Holds no credential and opens no socket, because its venue is this
    repository's own cached bars.
    """

    broker_id = "paper"
    environment = ExecutionEnvironment.PAPER
    version = "paper-gateway-v1"

    def __init__(self, executor: PaperExecutor, calendar: MarketCalendar,
                 registry: InstrumentRegistry,
                 account_id: str = "paper-account",
                 broker_id: str = "paper"):
        self.executor = executor
        self.calendar = calendar
        self.registry = registry
        self.account_id = account_id
        self.broker_id = broker_id
        self._state = BrokerConnectionState.DISCONNECTED
        #: Events produced since the last drain. The paper executor is
        #: synchronous, so this is always short — but the orchestrator
        #: reads it the same way it would read a streaming buffer.
        self._pending: List[ExecutionEvent] = []
        self._seq = 0
        #: canonical order id -> the PaperOrder it produced
        self._orders: Dict[str, PaperOrder] = {}
        #: Marks, freshness and spendable cash for the current moment.
        #: All three are supplied by the caller rather than re-derived,
        #: because the tick above has already gathered them and a second
        #: lookup could disagree with the first.
        self._prices: Dict[str, Optional[float]] = {}
        self._freshness = None
        self._available_cash: Optional[float] = None

    # ---------------- connection ----------------

    def connect(self) -> BrokerConnectionState:
        self.executor.connect()
        self._state = BrokerConnectionState.CONNECTED
        self._emit(ExecutionEventType.BROKER_CONNECTED, None,
                   detail="paper executor ready; no external venue involved")
        return self._state

    def disconnect(self) -> None:
        self.executor.disconnect()
        self._state = BrokerConnectionState.DISCONNECTED
        self._emit(ExecutionEventType.BROKER_DISCONNECTED, None)

    def connection_state(self) -> BrokerConnectionState:
        return self._state

    def health_check(self, now: datetime) -> BrokerHealth:
        health = self.executor.health(now)
        state = HEALTH_TO_CONNECTION.get(health, BrokerConnectionState.ERROR)
        if self._state is BrokerConnectionState.DISCONNECTED:
            state = BrokerConnectionState.DISCONNECTED
        return BrokerHealth(
            broker_id=self.broker_id, at=now, state=state,
            latency_ms=0.0,
            detail=f"paper executor reports {health.value}")

    # ---------------- prices ----------------

    def set_market_context(self, prices: Optional[Dict[str, Optional[float]]] = None,
                           freshness=None,
                           available_cash: Optional[float] = None) -> None:
        """
        Supply the marks, data freshness and spendable cash for this moment.

        The paper executor needs all three to decide whether an order
        may be accepted, and the tick above has already computed them.
        Re-deriving them here would mean two lookups per tick that
        could disagree — and a freshness reading that disagreed with
        the one the session reported would be worse than none.
        """
        if prices is not None:
            self._prices = dict(prices)
        if freshness is not None:
            self._freshness = freshness
        if available_cash is not None:
            self._available_cash = float(available_cash)

    def set_prices(self, prices: Dict[str, Optional[float]]) -> None:
        """Marks only. Kept for callers that have nothing else to give."""
        self.set_market_context(prices=prices)

    # ---------------- account ----------------

    def get_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        view = self.executor.get_account(self._prices)
        return AccountSnapshot(
            account_id=account_id, broker_id=self.broker_id, at=now,
            base_currency=view.base_currency, cash=view.cash,
            equity=view.equity, available_funds=view.cash,
            buying_power=view.buying_power, portfolio_value=view.equity,
            unrealized_pnl=None,
            margin=MarginSnapshot(),
            environment=self.environment,
            raw_broker_payload={"is_paper": True,
                                "gross_exposure": view.gross_exposure,
                                "net_exposure": view.net_exposure})

    def get_positions(self, account_id: str,
                      now: datetime) -> List[PositionSnapshot]:
        out: List[PositionSnapshot] = []
        for view in self.executor.get_positions(self._prices):
            mapping = self.registry.get(self.broker_id, view.instrument_id)
            out.append(PositionSnapshot(
                account_id=account_id, broker_id=self.broker_id,
                instrument_id=view.instrument_id, quantity=view.quantity,
                average_price=view.average_price, at=now,
                market_price=(view.market_value / view.quantity
                              if view.market_value is not None and view.quantity
                              else None),
                unrealized_pnl=view.unrealized_pnl,
                broker_symbol=mapping.broker_symbol if mapping else "",
                raw_broker_payload={"is_paper": True}))
        return out

    # ---------------- orders ----------------

    def get_open_orders(self, account_id: str) -> List[BrokerOrderView]:
        return [self._to_view(o) for o in self.executor.get_orders(open_only=True)]

    def get_order(self, broker_order_id: str) -> Optional[BrokerOrderView]:
        for order in self.executor.get_orders():
            if order.order_id == broker_order_id:
                return self._to_view(order)
        return None

    def submit_order(self, order: ExecutionOrder, now: datetime) -> SubmissionAck:
        """
        Hand the order to the paper executor.

        Returns an acknowledgement only. Fills, if the bar produces
        them, arrive through `poll_events` like any venue's would.
        """
        if not self._state.can_submit:
            return SubmissionAck(
                accepted=False, state=order.state,
                reject_code=ExecutionRejectCode.BROKER_DISCONNECTED,
                detail=f"paper gateway is {self._state.value}", at=now)

        paper_order = self._to_paper_order(order, now)
        placed = self.executor.place_order(
            paper_order, now, freshness=self._freshness,
            available_cash=self._available_cash)
        self._orders[order.order_id] = placed

        if placed.state is PaperOrderState.REJECTED:
            code = PAPER_REJECT_TO_CANONICAL.get(
                placed.reject_reason, ExecutionRejectCode.ADAPTER_ERROR)
            self._emit(ExecutionEventType.ORDER_REJECTED, order,
                       detail=placed.reject_detail, code=code.value)
            return SubmissionAck(
                accepted=False, state=ExecutionOrderState.REJECTED,
                broker_order_id=placed.order_id, reject_code=code,
                detail=placed.reject_detail, at=now)

        self._emit(ExecutionEventType.ORDER_SUBMITTED, order)
        self._emit(ExecutionEventType.ORDER_ACKNOWLEDGED, order,
                   broker_order_id=placed.order_id)
        return SubmissionAck(
            accepted=True, state=ExecutionOrderState.ACKNOWLEDGED,
            broker_order_id=placed.order_id, at=now,
            raw_broker_payload={"paper_state": placed.state.value})

    def cancel_order(self, broker_order_id: str, now: datetime) -> SubmissionAck:
        cancelled = self.executor.cancel_order(broker_order_id, now)
        if cancelled is None:
            return SubmissionAck(
                accepted=False, state=ExecutionOrderState.UNKNOWN,
                reject_code=ExecutionRejectCode.ADAPTER_ERROR,
                detail=f"no paper order {broker_order_id}", at=now)
        self._emit(ExecutionEventType.ORDER_CANCELLED, None,
                   broker_order_id=broker_order_id)
        return SubmissionAck(
            accepted=True,
            state=PAPER_TO_CANONICAL.get(cancelled.state,
                                         ExecutionOrderState.CANCELLED),
            broker_order_id=broker_order_id, at=now)

    def try_fill(self, order: ExecutionOrder, now: datetime) -> List[ExecutionFill]:
        """
        Ask the paper executor to fill against the current bar.

        Deliberately NOT part of `BrokerGateway`. A real venue fills on
        its own schedule and nobody asks it to, so putting this on the
        canonical interface would let orchestration code assume a
        pull-to-fill model that no real adapter can honour. The
        orchestrator reaches for it only after checking the gateway
        advertises it.

        Phase 13's `try_fill` APPLIES the fills it produces — it
        updates the paper order and the ledger before returning. So
        this method must not apply them again. Calling `apply_fill`
        here as well was the first bug this adapter had, and the only
        reason it did not double-count a position is that Phase 13
        deduplicates by fill key and refused the second application.
        The guard worked; the code above it was still wrong.
        """
        paper_order = self._orders.get(order.order_id)
        if paper_order is None or not paper_order.state.is_working:
            return []

        produced: List[ExecutionFill] = []
        for fill in self.executor.try_fill(
                paper_order, now, available_cash=self._available_cash):
            canonical = self._to_canonical_fill(fill, order)
            produced.append(canonical)
            event_type = (ExecutionEventType.ORDER_FILLED
                          if paper_order.state is PaperOrderState.FILLED
                          else ExecutionEventType.ORDER_PARTIALLY_FILLED)
            self._emit(event_type, order, broker_order_id=paper_order.order_id,
                       fill_id=canonical.fill_id,
                       quantity=canonical.quantity, price=canonical.price)
        return produced

    def expire_stale_orders(self, now: datetime) -> List[ExecutionEvent]:
        events: List[ExecutionEvent] = []
        for expired in self.executor.expire_stale_orders(now):
            events.append(self._emit(
                ExecutionEventType.ORDER_EXPIRED, None,
                broker_order_id=expired.order_id))
        return events

    # ---------------- events ----------------

    def poll_events(self, now: datetime) -> List[ExecutionEvent]:
        drained, self._pending = self._pending, []
        return drained

    # ---------------- capability and instruments ----------------

    def get_capabilities(self) -> BrokerCapability:
        """
        Only what the paper executor genuinely does.

        Bracket, OCO, trailing stops and order modification are all
        False: the executor implements none of them, and declaring a
        capability the adapter cannot honour would let an order through
        validation to fail at the venue instead.
        """
        return BrokerCapability(
            broker_id=self.broker_id,
            supports_market_orders=True,
            supports_limit_orders=True,
            supports_stop_orders=True,
            supports_stop_limit_orders=True,
            supports_partial_fills=True,
            supports_fractional_quantity=True,
            supports_shorting=self.executor.allow_shorting,
            supports_realtime_quotes=False,
            asset_classes=("stock", "etf", "crypto", "bvb"),
            times_in_force=(CanonicalTimeInForce.DAY, CanonicalTimeInForce.GTC,
                            CanonicalTimeInForce.IOC),
            position_accounting=PositionAccounting.NETTING,
            rate_limit_per_minute=None,
            notes="simulated fills against cached daily bars; no external venue")

    def resolve_instrument(self, instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        return self.registry.get(self.broker_id, instrument_id)

    def market_status(self, instrument_id: str, now: datetime) -> MarketStatus:
        """
        Derived from the Phase 12 calendar, and no finer.

        Daily bars can say whether a session exists on a date. They
        cannot distinguish pre-market from regular hours or detect a
        halt, so those values are never returned here — a guess would
        be worse than UNKNOWN.
        """
        if not self.calendar.has_data(instrument_id):
            return MarketStatus.UNKNOWN
        return (MarketStatus.OPEN
                if self.calendar.is_open(instrument_id, now.date())
                else MarketStatus.CLOSED)

    # ---------------- recovery ----------------

    def restore_orders(self, orders: Sequence[ExecutionOrder]) -> int:
        """
        Repopulate the paper venue book after a restart.

        The paper "venue" is an in-process executor: it dies with the
        process, while a real broker keeps its own state across our
        restarts. Without this, every restored order would look to
        reconciliation like an order the venue had never heard of —
        a mismatch caused entirely by the adapter forgetting, not by
        any real disagreement.

        Only working orders are restored into the executor, because
        only those can still receive a fill. Terminal ones stay in the
        orchestrator book, where reconciliation can still see them.
        """
        restored = 0
        for order in orders:
            if not order.state.is_working:
                continue
            paper_order = self._to_paper_order(order, order.intent_at
                                               or order.submitted_at)
            paper_order.state = PaperOrderState.ACCEPTED
            paper_order.filled_quantity = order.filled_quantity
            paper_order.average_fill_price = order.average_fill_price
            self.executor._orders[paper_order.order_id] = paper_order
            if paper_order.idempotency_key:
                self.executor._by_idempotency[paper_order.idempotency_key] =                     paper_order.order_id
            self._orders[order.order_id] = paper_order
            restored += 1
        return restored

    # ---------------- translation ----------------

    def _to_paper_order(self, order: ExecutionOrder,
                        now: datetime) -> PaperOrder:
        return PaperOrder(
            order_id=order.order_id,
            session_id=order.correlation_id or order.order_id,
            account_id=self.account_id,
            instrument_id=order.instrument_id,
            side=OrderSide.BUY if order.side is CanonicalOrderSide.BUY else OrderSide.SELL,
            quantity=order.quantity,
            order_type=CANONICAL_TO_PAPER_TYPE.get(order.order_type,
                                                   PaperOrderType.MARKET),
            time_in_force=CANONICAL_TO_PAPER_TIF.get(order.time_in_force,
                                                     TimeInForce.DAY),
            limit_price=order.limit_price, stop_price=order.stop_price,
            idempotency_key=order.idempotency_key,
            signal_id=order.signal_id, decision_id=order.decision_id,
            intent_id=order.intent_id, strategy_id=order.strategy_id,
            model_version=order.model_version,
            information_cutoff=order.intent_at, decided_at=order.intent_at,
            created_at=now, expires_at=order.expires_at)

    def _to_canonical_fill(self, fill: PaperFill,
                           order: ExecutionOrder) -> ExecutionFill:
        return ExecutionFill(
            fill_id=fill.fill_id, order_id=order.order_id,
            broker_id=self.broker_id, account_id=order.account_id,
            instrument_id=fill.instrument_id,
            side=(CanonicalOrderSide.BUY if fill.side is OrderSide.BUY
                  else CanonicalOrderSide.SELL),
            quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at,
            execution_id=fill.fill_id, broker_order_id=fill.order_id,
            commission=fill.commission, fees=0.0,
            reference_price=fill.reference_price,
            idempotency_key=fill.idempotency_key,
            correlation_id=order.correlation_id,
            raw_broker_payload={
                "slippage_cost": fill.slippage_cost,
                "is_partial": fill.is_partial,
                "intrabar_ambiguous": fill.intrabar_ambiguous,
                "venue": fill.venue.value,
                "execution_model_version": fill.execution_model_version})

    def _to_view(self, order: PaperOrder) -> BrokerOrderView:
        mapping = self.registry.get(self.broker_id, order.instrument_id)
        return BrokerOrderView(
            broker_order_id=order.order_id,
            instrument_id=order.instrument_id,
            broker_symbol=mapping.broker_symbol if mapping else order.instrument_id,
            side=(CanonicalOrderSide.BUY if order.side is OrderSide.BUY
                  else CanonicalOrderSide.SELL),
            quantity=order.quantity, filled_quantity=order.filled_quantity,
            average_fill_price=order.average_fill_price,
            state=PAPER_TO_CANONICAL.get(order.state, ExecutionOrderState.UNKNOWN),
            limit_price=order.limit_price, stop_price=order.stop_price,
            client_order_id=order.idempotency_key,
            at=order.created_at,
            raw_broker_payload={"paper_state": order.state.value})

    def _emit(self, event_type: ExecutionEventType,
              order: Optional[ExecutionOrder], **payload) -> ExecutionEvent:
        self._seq += 1
        event = ExecutionEvent(
            event_id=f"ev-{self.broker_id}-{self._seq:08d}",
            event_type=event_type, broker_id=self.broker_id,
            account_id=order.account_id if order else self.account_id,
            order_id=order.order_id if order else None,
            broker_order_id=payload.pop("broker_order_id", None),
            fill_id=payload.pop("fill_id", None),
            instrument_id=order.instrument_id if order else None,
            correlation_id=order.correlation_id if order else "",
            sequence=self._seq, payload=payload)
        self._pending.append(event)
        return event
