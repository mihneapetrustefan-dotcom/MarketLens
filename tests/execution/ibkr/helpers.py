"""
tests/execution/ibkr/helpers.py
-----------------------------------
Fixtures for the Phase 15 IBKR tests.

NO REAL IBKR SESSION IS USED ANYWHERE IN THIS SUITE
-------------------------------------------------------
Every test here runs against `MockIBKRTransport`. That is deliberate
and it is stated rather than implied: a suite that needed a live
gateway, a funded paper account and a browser login would be
non-deterministic, unrunnable in CI, and impossible for a second
developer to execute.

What the mock cannot prove is that IBKR behaves the way the mock
does. That gap is real, it is named in the final report, and the
integration script in `scripts/run_ibkr.py` is what closes it against
an actual paper account.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from src.domain.broker_models import (
    Broker, BrokerAccount, CanonicalOrderSide, ExecutionEnvironment,
    ExecutionOrder, ExecutionOrderState,
)
from src.execution.adapters.ibkr.config import IBKRConfig, paper_config
from src.execution.adapters.ibkr.gateway import IBKRGateway
from src.execution.adapters.ibkr.mock_transport import (
    MOCK_ACCOUNT, MockContract, MockIBKRTransport,
)
from src.execution.instruments import InstrumentRegistry
from src.execution.orchestrator import (
    BrokerRegistry, ExecutionOrchestrator, IntentRequest,
)
from src.execution.safety import ExecutionSafety

AT = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
INSTRUMENT = "i-aapl"


class AlwaysOpenCalendar:
    """
    A calendar that reports every instrument open.

    The Phase 12 calendar is built from cached bars, and these tests
    have none — an instrument with no bars is correctly UNKNOWN, which
    the validator correctly treats as not tradeable. That is right
    behaviour and it would block every order here, so the session gate
    is stubbed while everything else stays real.
    """

    def has_data(self, instrument_id: str) -> bool:
        return True

    def is_open(self, instrument_id, day) -> bool:
        return True


class ClosedCalendar(AlwaysOpenCalendar):
    def is_open(self, instrument_id, day) -> bool:
        return False


def build_ibkr(ordering_enabled: bool = True, resolve: bool = True,
               calendar: Optional[Any] = None,
               **config_overrides) -> Dict[str, Any]:
    """
    A full stack: mock transport, IBKR gateway, Phase 14 orchestrator.

    Everything above the gateway is the real Phase 14 code — the
    orchestrator, the validator, the state machine, the reconciler.
    Only the venue is a double.
    """
    config = paper_config(account_id=MOCK_ACCOUNT,
                          ordering_enabled=ordering_enabled,
                          **config_overrides)
    transport = MockIBKRTransport(config)
    instruments = InstrumentRegistry()
    gateway = IBKRGateway(config, transport, instruments,
                          calendar=calendar or AlwaysOpenCalendar())
    gateway.connect()

    if resolve:
        gateway.resolve_contract(INSTRUMENT, "AAPL")

    registry = BrokerRegistry()
    registry.register(
        Broker(broker_id="ibkr", name="Interactive Brokers (paper)",
               environment=ExecutionEnvironment.PAPER,
               adapter="ibkr-gateway-v1"),
        gateway,
        [BrokerAccount(account_id=MOCK_ACCOUNT, broker_id="ibkr",
                       name="IBKR paper",
                       environment=ExecutionEnvironment.PAPER)])

    safety = ExecutionSafety()
    orchestrator = ExecutionOrchestrator(registry, instruments, safety)
    return {
        "config": config, "transport": transport, "gateway": gateway,
        "instruments": instruments, "registry": registry, "safety": safety,
        "orchestrator": orchestrator,
    }


def ibkr_request(**overrides) -> IntentRequest:
    base: Dict[str, Any] = dict(
        intent_id="int-1", broker_id="ibkr", account_id=MOCK_ACCOUNT,
        instrument_id=INSTRUMENT, side=CanonicalOrderSide.BUY, quantity=10.0,
        now=AT, reference_price=100.0, decision_price=100.0,
        risk_approved=True, strategy_id="strat-1", portfolio_id="pf-1",
        signal_id="sig-1")
    base.update(overrides)
    return IntentRequest(**base)


def submit(stack: Dict[str, Any], **overrides):
    """Place one order through the whole Phase 14 pipeline."""
    return stack["orchestrator"].execute(ibkr_request(**overrides))


def fill_through_gateway(stack: Dict[str, Any], order: ExecutionOrder,
                         quantity: float, price: float,
                         commission: float = 1.0,
                         execution_id: Optional[str] = None) -> int:
    """
    Fill at the venue, then pull the execution through the adapter and
    apply it the way the orchestrator would.
    """
    from src.execution.states import apply_fill_to_order

    stack["transport"].fill(order.broker_order_id, quantity, price,
                            commission=commission, execution_id=execution_id)
    fills = stack["gateway"].collect_fills({order.order_id: order})
    applied = 0
    for fill in fills:
        if apply_fill_to_order(order, fill.quantity, fill.price,
                               fill.commission, fill.fees):
            applied += 1
    stack["orchestrator"].record_fills(fills)
    return applied
