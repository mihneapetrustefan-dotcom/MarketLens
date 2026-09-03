"""
tests/execution/ibkr/test_gateway_conformance.py
-----------------------------------------------------
The generic broker-gateway contract, run against every adapter
(Phase 15, spec §59, §67, §68).

WHY ONE SUITE FOR THREE ADAPTERS
------------------------------------
Phase 14's claim is that the core does not care which broker it is
talking to. A test suite written separately for each adapter cannot
check that claim — it can only check that each adapter works, which is
a weaker statement.

So the contract is written once, as `GatewayContract`, and three
subclasses supply a different gateway. Paper, IBKR and the deliberately
refusing MT5 placeholder all run the same assertions. If a future MT5
adapter needs the assertions changed, the abstraction leaked.

WHAT THE CONTRACT DELIBERATELY DOES NOT ASSERT
--------------------------------------------------
Anything venue-specific: fill prices, latency, symbol spellings,
capability sets. Those differ legitimately. What it asserts is the
SHAPE — that every method returns canonical types, that no raw broker
object escapes, and that the invariants hold whichever venue is
underneath.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from src.domain.broker_models import (
    AccountSnapshot, BrokerCapability, BrokerConnectionState, BrokerHealth,
    CanonicalOrderType, ExecutionEnvironment, ExecutionOrderState,
    MarketStatus, PositionSnapshot,
)
from src.execution.adapters.disabled_gateway import planned_gateways
from src.execution.gateway import BrokerGateway, BrokerOrderView, SubmissionAck
from tests.execution.helpers import build_paper_stack
from tests.execution.ibkr.helpers import AT, build_ibkr


class GatewayContract:
    """
    Assertions every `BrokerGateway` must satisfy.

    Not a `TestCase` itself, so it is not collected on its own — the
    subclasses below supply a gateway and inherit every assertion.
    """

    #: Set by each subclass.
    gateway: BrokerGateway = None
    account_id: str = ""
    #: False for adapters that exist but refuse to trade.
    tradeable: bool = True

    # ---------------- identity ----------------

    def test_it_declares_a_broker_id_and_version(self):
        self.assertTrue(self.gateway.broker_id)
        self.assertNotEqual(self.gateway.version, "abstract")

    def test_it_never_claims_a_real_money_environment(self):
        self.assertFalse(self.gateway.environment.is_real_money)

    # ---------------- connection ----------------

    def test_connection_state_is_canonical(self):
        self.assertIsInstance(self.gateway.connection_state(),
                              BrokerConnectionState)

    def test_health_check_returns_canonical_health(self):
        health = self.gateway.health_check(AT)
        self.assertIsInstance(health, BrokerHealth)
        self.assertEqual(health.broker_id, self.gateway.broker_id)
        self.assertIsInstance(health.state, BrokerConnectionState)

    def test_connect_takes_no_credentials(self):
        """
        The property that keeps secrets out of call sites, logs and
        serialised requests. An adapter needing credentials reads them
        itself, inside connect.
        """
        import inspect
        signature = inspect.signature(self.gateway.connect)
        self.assertEqual(list(signature.parameters), [])

    def test_it_exposes_no_login_or_authenticate(self):
        for name in ("login", "authenticate", "set_credentials", "sign_in"):
            self.assertFalse(hasattr(self.gateway, name),
                             f"{self.gateway.broker_id} exposes {name}")

    # ---------------- account ----------------

    def test_account_is_a_canonical_snapshot(self):
        snapshot = self.gateway.get_account(self.account_id, AT)
        self.assertIsInstance(snapshot, AccountSnapshot)
        self.assertFalse(snapshot.environment.is_real_money)
        self.assertIsInstance(snapshot.spendable, float)

    def test_positions_are_canonical_snapshots(self):
        for position in self.gateway.get_positions(self.account_id, AT):
            self.assertIsInstance(position, PositionSnapshot)
            self.assertIsNotNone(position.side)

    # ---------------- orders ----------------

    def test_open_orders_are_canonical_views(self):
        for view in self.gateway.get_open_orders(self.account_id):
            self.assertIsInstance(view, BrokerOrderView)
            self.assertIsInstance(view.state, ExecutionOrderState)

    def test_get_order_returns_a_view_or_none_never_raises_on_unknown(self):
        """
        The call that resolves UNKNOWN. An adapter that raised for an
        id the venue does not know would make the resolution path
        indistinguishable from an unreachable venue.
        """
        self.assertIsNone(self.gateway.get_order("definitely-not-an-order"))

    def test_capabilities_are_canonical_and_self_consistent(self):
        capability = self.gateway.get_capabilities()
        self.assertIsInstance(capability, BrokerCapability)
        self.assertEqual(capability.broker_id, self.gateway.broker_id)
        for order_type in CanonicalOrderType:
            self.assertIsInstance(capability.supports_order_type(order_type), bool)

    def test_it_never_claims_an_unmapped_order_type(self):
        """
        A capability claimed but unmapped lets an order through
        validation to fail at the venue instead — one layer too late.
        """
        capability = self.gateway.get_capabilities()
        for order_type in (CanonicalOrderType.BRACKET, CanonicalOrderType.OCO,
                           CanonicalOrderType.TRAILING_STOP):
            self.assertFalse(capability.supports_order_type(order_type),
                             f"{self.gateway.broker_id} claims {order_type.value}")

    # ---------------- events and market data ----------------

    def test_poll_events_returns_a_list(self):
        self.assertIsInstance(self.gateway.poll_events(AT), list)

    def test_market_status_is_canonical(self):
        self.assertIsInstance(self.gateway.market_status("i-anything", AT),
                              MarketStatus)

    def test_an_unknown_instrument_is_not_reported_open(self):
        """UNKNOWN and CLOSED are both acceptable; OPEN is not."""
        status = self.gateway.market_status("i-does-not-exist", AT)
        self.assertIsNot(status, MarketStatus.OPEN)

    # ---------------- reconciliation ----------------

    def test_reconciliation_reads_return_canonical_types(self):
        self.assertIsInstance(
            self.gateway.reconcile_account(self.account_id, AT), AccountSnapshot)
        for view in self.gateway.reconcile_orders(self.account_id):
            self.assertIsInstance(view, BrokerOrderView)
        for position in self.gateway.reconcile_positions(self.account_id, AT):
            self.assertIsInstance(position, PositionSnapshot)

    # ---------------- modification ----------------

    def test_modification_is_refused_unless_declared(self):
        """
        The default refuses honestly. An adapter that can amend
        overrides it AND declares the capability — the two must agree.
        """
        if self.gateway.get_capabilities().supports_order_modification:
            self.skipTest("this adapter declares modification support")
        ack = self.gateway.modify_order("any-order", AT, quantity=1.0)
        self.assertIsInstance(ack, SubmissionAck)
        self.assertFalse(ack.accepted)


class TestPaperGatewayConformance(GatewayContract, unittest.TestCase):
    """Phase 13's executor, behind the Phase 14 interface."""

    def setUp(self):
        stack = build_paper_stack()
        self.gateway = stack["gateway"]
        self.account_id = "acct-1"


class TestIBKRGatewayConformance(GatewayContract, unittest.TestCase):
    """Phase 15's adapter, against the deterministic mock transport."""

    def setUp(self):
        stack = build_ibkr()
        self.gateway = stack["gateway"]
        self.account_id = stack["config"].account_id


class TestDisabledGatewayConformance(GatewayContract, unittest.TestCase):
    """
    The MT5 placeholder — an adapter that conforms and refuses.

    Included in the conformance run on purpose: it is the shape Phase
    16 will fill in, and running the contract against it now proves the
    registry, the validator and reconciliation all handle a venue that
    exists and cannot trade.
    """

    tradeable = False

    def setUp(self):
        self.gateway = planned_gateways()["mt5"]
        self.account_id = "any-account"


class TestTheAdaptersDifferOnlyWhereTheyShould(unittest.TestCase):
    """Spec §67, §68: the core must not need to know which venue."""

    def setUp(self):
        self.paper = build_paper_stack()["gateway"]
        self.ibkr = build_ibkr()["gateway"]

    def test_they_are_interchangeable_types(self):
        for gateway in (self.paper, self.ibkr):
            self.assertIsInstance(gateway, BrokerGateway)

    def test_they_declare_different_capabilities(self):
        """
        Not a defect — the point. IBKR trades whole shares and paper
        trades fractions, and the capability model is how the core
        learns that without knowing why.
        """
        paper = self.paper.get_capabilities()
        ibkr = self.ibkr.get_capabilities()
        self.assertTrue(paper.supports_fractional_quantity)
        self.assertFalse(ibkr.supports_fractional_quantity)
        self.assertNotEqual(paper.notes, ibkr.notes)

    def test_no_ibkr_vocabulary_appears_in_the_core(self):
        """
        The structural check. If IBKR names leak above the adapter,
        Phase 16 becomes a rewrite instead of an addition.
        """
        import ast
        import pathlib

        banned = ("ibkr", "conid", "tws", "client_portal", "interactive brokers")
        root = pathlib.Path(__file__).resolve().parents[3] / "src"
        core = [
            root / "execution" / "orchestrator.py",
            root / "execution" / "validation.py",
            root / "execution" / "states.py",
            root / "execution" / "events.py",
            root / "execution" / "policy.py",
            root / "execution" / "reconciliation.py",
            root / "execution" / "gateway.py",
            root / "execution" / "service.py",
            root / "execution" / "instruments.py",
            root / "domain" / "broker_models.py",
            root / "portfolio" / "service.py",
            root / "portfolio" / "risk_engine.py",
            root / "signals" / "engine.py",
        ]
        for path in core:
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Strip docstrings: prose may legitimately mention IBKR as
            # a future venue. Executable code may not.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = node.body
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        node.body = body[1:] or [ast.Pass()]
            code = ast.unparse(tree).lower()
            for word in banned:
                self.assertNotIn(word, code,
                                 f"{path.name} contains IBKR vocabulary: {word}")


if __name__ == "__main__":
    unittest.main()
