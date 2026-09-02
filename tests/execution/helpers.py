"""
tests/execution/helpers.py
-------------------------------
Shared fixtures for the Phase 14 tests.

THE FAKE GATEWAY IS THE POINT
---------------------------------
Most of what Phase 14 defends against cannot be produced by a
well-behaved venue: a submission that times out after the venue
accepted, a fill delivered twice, events arriving in the wrong order, a
broker holding an order we have no record of.

The paper adapter cannot do any of those, because the paper executor is
synchronous and correct. So `FakeGateway` exists to misbehave on
command — every failure mode is a flag, and each adversarial test sets
the one it is about. It is a test double, not a simulator: it does not
model a market, it models a venue being difficult.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.backtest.accounting import PortfolioLedger
from src.backtest.calendar import MarketCalendar
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.backtest_models import CostModel, SlippageMethod, SlippageModel
from src.domain.broker_models import (
    AccountSnapshot, Broker, BrokerAccount, BrokerCapability,
    BrokerConnectionState, BrokerHealth, BrokerInstrumentMapping,
    CanonicalOrderSide, CanonicalOrderType, CanonicalTimeInForce,
    ExecutionEnvironment, ExecutionEvent, ExecutionEventType, ExecutionFill,
    ExecutionOrder, ExecutionOrderState, ExecutionRejectCode, MarketStatus,
    PositionAccounting, PositionSnapshot,
)
from src.execution.adapters.paper_gateway import PaperBrokerGateway
from src.execution.gateway import BrokerGateway, BrokerOrderView, SubmissionAck
from src.execution.instruments import InstrumentRegistry, default_equity_mapping
from src.execution.orchestrator import (
    BrokerRegistry, ExecutionOrchestrator, IntentRequest,
)
from src.execution.safety import ExecutionSafety, SafetySwitches
from src.execution.service import Caller, ExecutionService
from src.paper.executor import PaperExecutor
from tests.paper.helpers import END, flat_universe, make_connection

AT = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


def execution_connection() -> sqlite3.Connection:
    """A database carrying the paper schemas plus Phase 14's."""
    conn = make_connection()
    initialize_execution_schema(conn)
    return conn


# ============================================================
# The misbehaving venue
# ============================================================

class FakeGateway(BrokerGateway):
    """
    A venue that fails on command.

    Every flag corresponds to one adversarial scenario. Defaults are
    all well-behaved, so a test that sets nothing gets a cooperative
    broker and only the test's own subject is unusual.
    """

    environment = ExecutionEnvironment.PAPER
    version = "fake-gateway-v1"

    def __init__(self, broker_id: str = "fake",
                 environment: ExecutionEnvironment = ExecutionEnvironment.PAPER):
        self.broker_id = broker_id
        self.environment = environment
        self._state = BrokerConnectionState.CONNECTED

        # --- failure switches -----------------------------------
        self.timeout_on_submit = False
        self.raise_on_submit = False
        self.reject_on_submit: Optional[ExecutionRejectCode] = None
        self.raise_on_get_order = False
        self.unknown_to_broker = False       # get_order returns None
        self.market = MarketStatus.OPEN
        self.assign_broker_ids = True

        # --- observable state -----------------------------------
        self.submitted: List[ExecutionOrder] = []
        self.submit_calls = 0
        self.broker_orders: Dict[str, BrokerOrderView] = {}
        self.pending_events: List[ExecutionEvent] = []
        self.positions: List[PositionSnapshot] = []
        self.account = AccountSnapshot(
            account_id="fake-account", broker_id=broker_id, at=AT,
            cash=100_000.0, equity=100_000.0, buying_power=100_000.0)
        self.capability = BrokerCapability(
            broker_id=broker_id,
            supports_market_orders=True, supports_limit_orders=True,
            supports_stop_orders=True, supports_stop_limit_orders=True,
            supports_partial_fills=True, supports_fractional_quantity=True,
            supports_shorting=True,
            asset_classes=("stock",),
            times_in_force=(CanonicalTimeInForce.DAY, CanonicalTimeInForce.GTC),
            position_accounting=PositionAccounting.NETTING)
        self._seq = 0

    # ---------------- connection ----------------

    def connect(self) -> BrokerConnectionState:
        self._state = BrokerConnectionState.CONNECTED
        return self._state

    def disconnect(self) -> None:
        self._state = BrokerConnectionState.DISCONNECTED

    def set_state(self, state: BrokerConnectionState) -> None:
        self._state = state

    def connection_state(self) -> BrokerConnectionState:
        return self._state

    def health_check(self, now: datetime) -> BrokerHealth:
        return BrokerHealth(broker_id=self.broker_id, at=now, state=self._state)

    # ---------------- account ----------------

    def get_account(self, account_id: str, now: datetime) -> AccountSnapshot:
        return self.account

    def get_positions(self, account_id: str,
                      now: datetime) -> List[PositionSnapshot]:
        return list(self.positions)

    # ---------------- orders ----------------

    def get_open_orders(self, account_id: str) -> List[BrokerOrderView]:
        return [v for v in self.broker_orders.values() if v.state.is_working]

    def reconcile_orders(self, account_id: str) -> List[BrokerOrderView]:
        """
        Every order the venue knows of, not only the open ones.

        A real reconciliation endpoint returns recently-terminal orders
        too, and it has to: an order that just went terminal at the
        venue would otherwise look simply missing, which is a
        different and much more alarming finding.
        """
        return list(self.broker_orders.values())

    def get_order(self, broker_order_id: str) -> Optional[BrokerOrderView]:
        if self.raise_on_get_order:
            raise ConnectionError("broker query failed")
        if self.unknown_to_broker:
            return None
        return self.broker_orders.get(broker_order_id)

    def submit_order(self, order: ExecutionOrder, now: datetime) -> SubmissionAck:
        self.submit_calls += 1
        if self.raise_on_submit:
            raise ConnectionError("connection reset during submit")

        if self.timeout_on_submit:
            # The dangerous case: the venue ACCEPTED, we never heard.
            broker_id = f"bo-{len(self.broker_orders) + 1:04d}"
            self.broker_orders[broker_id] = self._view(order, broker_id,
                                                       ExecutionOrderState.WORKING)
            return SubmissionAck(accepted=False,
                                 state=ExecutionOrderState.UNKNOWN,
                                 timed_out=True, at=now)

        if self.reject_on_submit is not None:
            return SubmissionAck(accepted=False,
                                 state=ExecutionOrderState.REJECTED,
                                 reject_code=self.reject_on_submit,
                                 detail="fake rejection", at=now)

        self.submitted.append(order)
        broker_id = (f"bo-{len(self.broker_orders) + 1:04d}"
                     if self.assign_broker_ids else None)
        if broker_id:
            self.broker_orders[broker_id] = self._view(
                order, broker_id, ExecutionOrderState.WORKING)
        return SubmissionAck(accepted=True,
                             state=ExecutionOrderState.ACKNOWLEDGED,
                             broker_order_id=broker_id, at=now)

    def cancel_order(self, broker_order_id: str, now: datetime) -> SubmissionAck:
        view = self.broker_orders.get(broker_order_id)
        if view is None:
            return SubmissionAck(accepted=False,
                                 state=ExecutionOrderState.UNKNOWN,
                                 reject_code=ExecutionRejectCode.ADAPTER_ERROR,
                                 detail="no such order", at=now)
        view.state = ExecutionOrderState.CANCELLED
        return SubmissionAck(accepted=True, state=ExecutionOrderState.CANCELLED,
                             broker_order_id=broker_order_id, at=now)

    def _view(self, order: ExecutionOrder, broker_order_id: str,
              state: ExecutionOrderState) -> BrokerOrderView:
        return BrokerOrderView(
            broker_order_id=broker_order_id,
            instrument_id=order.instrument_id,
            broker_symbol=order.broker_symbol or order.instrument_id,
            side=order.side, quantity=order.quantity, state=state,
            client_order_id=order.client_order_id, at=order.intent_at)

    # ---------------- events ----------------

    def emit(self, event_type: ExecutionEventType, order: ExecutionOrder,
             at: Optional[datetime] = None, event_id: Optional[str] = None,
             **payload) -> ExecutionEvent:
        """Queue an event for the next poll. Tests control the order."""
        self._seq += 1
        event = ExecutionEvent(
            event_id=event_id or f"fe-{self._seq:06d}",
            event_type=event_type, at=at or AT, broker_id=self.broker_id,
            account_id=order.account_id, order_id=order.order_id,
            broker_order_id=order.broker_order_id,
            instrument_id=order.instrument_id,
            correlation_id=order.correlation_id, sequence=self._seq,
            payload=payload)
        self.pending_events.append(event)
        return event

    def queue(self, event: ExecutionEvent) -> ExecutionEvent:
        """Queue an event built elsewhere, e.g. a deliberate redelivery."""
        self.pending_events.append(event)
        return event

    def poll_events(self, now: datetime) -> List[ExecutionEvent]:
        drained, self.pending_events = self.pending_events, []
        return drained

    # ---------------- capability and instruments ----------------

    def get_capabilities(self) -> BrokerCapability:
        return self.capability

    def resolve_instrument(self, instrument_id: str) -> Optional[BrokerInstrumentMapping]:
        return None

    def market_status(self, instrument_id: str, now: datetime) -> MarketStatus:
        return self.market


# ============================================================
# Wiring
# ============================================================

def fake_fill(order: ExecutionOrder, quantity: float, price: float,
              fill_id: str = "ff-1", at: Optional[datetime] = None,
              key: Optional[str] = None) -> ExecutionFill:
    return ExecutionFill(
        fill_id=fill_id, order_id=order.order_id, broker_id=order.broker_id,
        account_id=order.account_id, instrument_id=order.instrument_id,
        side=order.side, quantity=quantity, price=price, filled_at=at or AT,
        execution_id=fill_id, broker_order_id=order.broker_order_id,
        idempotency_key=key or fill_id, correlation_id=order.correlation_id)


def build_fake_stack(broker_id: str = "fake",
                     instrument_id: str = "i-aaa",
                     safety: Optional[ExecutionSafety] = None):
    """An orchestrator wired to a `FakeGateway`, with one mapped instrument."""
    instruments = InstrumentRegistry()
    instruments.register(default_equity_mapping(
        instrument_id, broker_id, "AAA", minimum_quantity=1.0,
        quantity_increment=1.0))

    gateway = FakeGateway(broker_id)
    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id=broker_id, name="Fake", adapter="fake-gateway-v1",
               environment=ExecutionEnvironment.PAPER),
        gateway,
        [BrokerAccount(account_id="fake-account", broker_id=broker_id,
                       name="Fake account",
                       environment=ExecutionEnvironment.PAPER)])

    safety = safety or ExecutionSafety()
    orchestrator = ExecutionOrchestrator(registry, instruments, safety)
    return orchestrator, gateway, instruments, safety


def build_paper_stack(conn: Optional[sqlite3.Connection] = None,
                      capital: float = 100_000.0):
    """
    An orchestrator wired to the REAL Phase 13 paper executor.

    Used wherever a test needs genuine fills rather than a double —
    the integration tests that prove Phase 13 still works through the
    Phase 14 boundary.
    """
    conn = conn or execution_connection()
    universe = flat_universe(conn, price=100.0, volume=100_000.0)
    instrument = universe[0]

    calendar = MarketCalendar(conn)
    calendar.load(universe)
    ledger = PortfolioLedger(capital, run_id="test")

    executor = PaperExecutor(
        calendar, ledger,
        CostModel(version="cost-v1", commission_bps=2.0),
        SlippageModel(version="slip-v1", method=SlippageMethod.FIXED_BPS,
                      base_bps=5.0),
        account_id="acct-1", session_id="test", max_participation=0.10)

    instruments = InstrumentRegistry()
    instruments.register(default_equity_mapping(instrument, "paper", "FLAT"))

    gateway = PaperBrokerGateway(executor, calendar, instruments,
                                 account_id="acct-1", broker_id="paper")
    gateway.connect()
    gateway.set_market_context(prices={instrument: 100.0},
                               available_cash=capital)

    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id="paper", name="Paper", adapter="paper-gateway-v1",
               environment=ExecutionEnvironment.PAPER),
        gateway,
        [BrokerAccount(account_id="acct-1", broker_id="paper",
                       name="Paper account",
                       environment=ExecutionEnvironment.PAPER)])

    safety = ExecutionSafety()
    orchestrator = ExecutionOrchestrator(registry, instruments, safety)
    bars = calendar.bars(instrument)
    return {
        "conn": conn, "orchestrator": orchestrator, "gateway": gateway,
        "instruments": instruments, "safety": safety, "ledger": ledger,
        "executor": executor, "calendar": calendar, "instrument": instrument,
        "bars": bars,
    }


def request(instrument_id: str = "i-aaa", broker_id: str = "fake",
            account_id: str = "fake-account", **overrides) -> IntentRequest:
    base: Dict[str, Any] = dict(
        intent_id="int-1", broker_id=broker_id, account_id=account_id,
        instrument_id=instrument_id, side=CanonicalOrderSide.BUY,
        quantity=100.0, now=AT, reference_price=100.0, decision_price=100.0,
        risk_approved=True, strategy_id="strat-1", portfolio_id="pf-1",
        signal_id="sig-1")
    base.update(overrides)
    return IntentRequest(**base)
