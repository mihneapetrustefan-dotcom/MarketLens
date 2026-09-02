"""
tests/execution/test_safety_and_boundary.py
------------------------------------------------
The live-execution boundary (Phase 14, spec §28, §29, §32, §46, §47,
§64, §71).

WHAT THESE TESTS DEFEND
---------------------------
The claim that no code path in this phase can place a real-money order.
That claim is only worth as much as the tests behind it, so these are
written to fail if any single layer were removed:

  - the domain types refuse to construct a live broker or account
  - the safety gate refuses LIVE before it checks anything else
  - `allow_real_orders` is a property with no setter
  - no permission grants live execution, even when held
  - no adapter capable of a live order exists
  - the repository refuses to store a live-execution control

Several of these are redundant with each other on purpose. Redundancy
is the design: a single check is one edit away from being wrong.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.execution_repository import ExecutionRepository
from src.data_access.execution_schema import initialize_execution_schema
from src.domain.broker_models import (
    Broker, BrokerAccount, ExecutionEnvironment, ExecutionPermission,
    ExecutionRejectCode,
)
from src.execution.adapters.disabled_gateway import (
    DisabledBrokerGateway, planned_gateways,
)
from src.execution.safety import (
    ExecutionSafety, RealMoneyExecutionDisabled, SafetySwitches,
)
from src.execution.service import Caller, ExecutionService, PermissionDenied
from tests.execution.helpers import AT, build_fake_stack, request


class TestTheLiveBoundary(unittest.TestCase):
    """Spec §71, questions 1 and 5. Each layer, separately."""

    def setUp(self):
        self.safety = ExecutionSafety()

    def test_allow_real_orders_is_false_and_has_no_setter(self):
        self.assertFalse(self.safety.allow_real_orders)
        with self.assertRaises(AttributeError):
            self.safety.allow_real_orders = True

    def test_setting_the_environment_variable_changes_nothing(self):
        """
        The variable is read only so the system can SAY someone set it.
        The worst outcome would be an operator setting it, seeing
        nothing happen, and assuming it worked.
        """
        os.environ["MARKETLENS_ALLOW_REAL_ORDERS"] = "true"
        try:
            self.assertTrue(self.safety.real_orders_requested_by_environment())
            self.assertFalse(self.safety.allow_real_orders)
            self.assertFalse(
                self.safety.check(ExecutionEnvironment.LIVE).permitted)
        finally:
            del os.environ["MARKETLENS_ALLOW_REAL_ORDERS"]

    def test_the_safety_state_reports_the_variable_being_set(self):
        os.environ["MARKETLENS_ALLOW_REAL_ORDERS"] = "1"
        try:
            state = self.safety.state()
            self.assertTrue(state["real_orders_env_set"])
            self.assertFalse(state["allow_real_orders"])
        finally:
            del os.environ["MARKETLENS_ALLOW_REAL_ORDERS"]

    def test_live_is_refused_before_every_other_check(self):
        """
        Even with execution disabled AND the kill switch on, the
        reported reason for LIVE is the real-money block — because
        that is the reason that will not change tomorrow.
        """
        safety = ExecutionSafety(SafetySwitches(
            execution_enabled=False, emergency_stop=True))
        verdict = safety.check(ExecutionEnvironment.LIVE)
        self.assertFalse(verdict.permitted)
        self.assertIs(verdict.code, ExecutionRejectCode.REAL_MONEY_BLOCKED)

    def test_asserting_against_live_raises_rather_than_returning(self):
        """A caller cannot ignore a result that is never returned."""
        with self.assertRaises(RealMoneyExecutionDisabled):
            self.safety.assert_not_real_money(ExecutionEnvironment.LIVE)
        for environment in (ExecutionEnvironment.SIMULATION,
                            ExecutionEnvironment.PAPER,
                            ExecutionEnvironment.DEMO):
            self.safety.assert_not_real_money(environment)

    def test_a_live_broker_cannot_be_constructed_as_implemented(self):
        with self.assertRaises(ValueError):
            Broker(broker_id="x", name="X",
                   environment=ExecutionEnvironment.LIVE, implemented=True)

    def test_a_live_account_cannot_be_constructed_at_all(self):
        with self.assertRaises(ValueError):
            BrokerAccount(account_id="a", broker_id="x", name="A",
                          environment=ExecutionEnvironment.LIVE)

    def test_a_live_gateway_cannot_be_constructed_even_disabled(self):
        with self.assertRaises(ValueError):
            DisabledBrokerGateway("x", environment=ExecutionEnvironment.LIVE)

    def test_no_permission_grants_live_execution(self):
        """Holding a permission for a capability that does not exist."""
        every = tuple(ExecutionPermission)
        verdict = self.safety.check_permission(
            every, ExecutionPermission.LIVE_EXECUTION_ADMIN)
        self.assertFalse(verdict.permitted)
        self.assertIs(verdict.code, ExecutionRejectCode.REAL_MONEY_BLOCKED)

    def test_the_repository_refuses_a_live_execution_control(self):
        conn = sqlite3.connect(":memory:")
        initialize_execution_schema(conn)
        with self.assertRaises(ValueError):
            ExecutionRepository(conn).save_control(
                "live_execution_enabled", True, AT)
        conn.close()

    def test_only_simulation_and_paper_report_as_implemented(self):
        implemented = {e for e in ExecutionEnvironment if e.is_implemented}
        self.assertEqual(implemented, {ExecutionEnvironment.SIMULATION,
                                       ExecutionEnvironment.PAPER})
        self.assertTrue(ExecutionEnvironment.LIVE.is_real_money)
        self.assertFalse(ExecutionEnvironment.DEMO.is_real_money)


class TestPlannedVenues(unittest.TestCase):
    """Spec §56, §57: MT5 and IBKR are named and absent."""

    def setUp(self):
        self.gateways = planned_gateways()

    def test_mt5_and_ibkr_are_listed_but_unimplemented(self):
        self.assertEqual(set(self.gateways), {"mt5", "ibkr"})
        for gateway in self.gateways.values():
            self.assertIn("no adapter is implemented", gateway.reason)
            # And each names the phase that will build it, so the
            # absence reads as scheduled rather than as an oversight.
            self.assertIn("Phase", gateway.reason)

    def test_they_claim_no_capability_at_all(self):
        for broker_id, gateway in self.gateways.items():
            capability = gateway.get_capabilities()
            self.assertEqual(capability.asset_classes, (), broker_id)
            self.assertEqual(capability.times_in_force, (), broker_id)
            self.assertEqual(capability.as_dict()["order_types"], [], broker_id)

    def test_every_submission_is_refused(self):
        from src.domain.broker_models import (
            CanonicalOrderSide, ExecutionOrder,
        )
        order = ExecutionOrder(
            order_id="o", intent_id="i", broker_id="mt5", account_id="a",
            instrument_id="ins", side=CanonicalOrderSide.BUY, quantity=1.0)
        ack = self.gateways["mt5"].submit_order(order, AT)
        self.assertFalse(ack.accepted)
        self.assertIs(ack.reject_code, ExecutionRejectCode.NOT_IMPLEMENTED)

    def test_they_never_report_a_connection(self):
        for gateway in self.gateways.values():
            self.assertFalse(gateway.connect().can_submit)
            self.assertFalse(gateway.health_check(AT).is_usable)

    def test_account_reads_return_empty_not_invented_numbers(self):
        snapshot = self.gateways["ibkr"].get_account("a", AT)
        self.assertEqual(snapshot.cash, 0.0)
        self.assertEqual(snapshot.equity, 0.0)
        self.assertFalse(snapshot.raw_broker_payload["implemented"])
        self.assertEqual(self.gateways["ibkr"].get_positions("a", AT), [])


class TestSafetySwitches(unittest.TestCase):
    """Spec §28: layered controls, outermost reason reported."""

    def test_the_broadest_applicable_reason_is_the_one_reported(self):
        safety = ExecutionSafety(SafetySwitches(
            execution_enabled=False, brokers={"paper": False}))
        verdict = safety.check(ExecutionEnvironment.PAPER, broker_id="paper")
        self.assertIs(verdict.code, ExecutionRejectCode.EXECUTION_DISABLED)

    def test_each_layer_can_stop_execution_on_its_own(self):
        cases = [
            (dict(execution_enabled=False), {},
             ExecutionRejectCode.EXECUTION_DISABLED),
            (dict(paper_execution_enabled=False), {},
             ExecutionRejectCode.ENVIRONMENT_DISABLED),
            (dict(brokers={"b": False}), dict(broker_id="b"),
             ExecutionRejectCode.BROKER_DISABLED),
            (dict(accounts={"a": False}), dict(account_id="a"),
             ExecutionRejectCode.ACCOUNT_DISABLED),
            (dict(strategies={"s": False}), dict(strategy_id="s"),
             ExecutionRejectCode.STRATEGY_DISABLED),
            (dict(portfolios={"p": False}), dict(portfolio_id="p"),
             ExecutionRejectCode.PORTFOLIO_DISABLED),
        ]
        for switches, routing, expected in cases:
            safety = ExecutionSafety(SafetySwitches(**switches))
            verdict = safety.check(ExecutionEnvironment.PAPER, **routing)
            self.assertIs(verdict.code, expected, expected.value)

    def test_an_unlisted_entity_is_enabled(self):
        """Absent means enabled; only an explicit False disables."""
        safety = ExecutionSafety(SafetySwitches(brokers={"other": False}))
        self.assertTrue(safety.check(ExecutionEnvironment.PAPER,
                                     broker_id="paper").permitted)

    def test_demo_is_disabled_by_default(self):
        safety = ExecutionSafety()
        verdict = safety.check(ExecutionEnvironment.DEMO)
        self.assertFalse(verdict.permitted)
        self.assertIn("no demo adapter", verdict.explanation)

    def test_there_is_no_live_switch_to_set(self):
        self.assertFalse(hasattr(SafetySwitches(), "live_execution_enabled"))


class TestPermissions(unittest.TestCase):
    """Spec §47: execution access is narrower than read access."""

    def setUp(self):
        self.orchestrator, self.gateway, _, self.safety = build_fake_stack()
        self.service = ExecutionService(self.orchestrator)

    def test_the_default_caller_is_read_only(self):
        caller = Caller()
        self.assertFalse(caller.holds(ExecutionPermission.PAPER_EXECUTION))
        self.assertTrue(caller.holds(ExecutionPermission.VIEW_EXECUTION))

    def test_a_viewer_cannot_submit(self):
        with self.assertRaises(PermissionDenied) as caught:
            self.service.submit(Caller.read_only(), request())
        self.assertIs(caught.exception.code, ExecutionRejectCode.NOT_PERMITTED)
        self.assertEqual(self.gateway.submit_calls, 0)

    def test_a_viewer_cannot_dry_run_either(self):
        """
        A dry run reveals buying power and mappings. It sends nothing,
        but it is still more than a read.
        """
        with self.assertRaises(PermissionDenied):
            self.service.dry_run(Caller.read_only(), request())

    def test_a_paper_trader_can_submit_to_paper(self):
        self.assertTrue(
            self.service.submit(Caller.paper_trader(), request()).accepted)

    def test_a_paper_trader_cannot_submit_to_demo(self):
        caller = Caller.paper_trader()
        self.assertFalse(caller.holds(ExecutionPermission.DEMO_EXECUTION))

    def test_stopping_needs_a_lower_bar_than_trading(self):
        """
        Stopping is always safer than continuing, so the permission to
        halt must never be harder to obtain than the permission to
        trade.
        """
        caller = Caller.paper_trader()
        self.service.activate_kill_switch(caller, "halt", AT)
        self.assertTrue(self.safety.kill_switch_active)

    def test_a_viewer_can_still_read_state_under_a_kill_switch(self):
        self.safety.activate_kill_switch("halt", at=AT, actor="t")
        viewer = Caller.read_only()
        self.assertTrue(self.service.list_brokers(viewer))
        self.assertTrue(self.service.safety_state(viewer)["emergency_stop"])


class TestNoSecretsAnywhere(unittest.TestCase):
    """Spec §27: credentials cannot be stored, logged or serialised."""

    def test_no_execution_table_has_a_credential_column(self):
        conn = sqlite3.connect(":memory:")
        initialize_execution_schema(conn)
        banned = ("password", "secret", "token", "api_key", "apikey",
                  "credential", "private_key", "certificate", "passphrase")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for row in conn.execute(f"PRAGMA table_info({table})"):
                name = row[1].lower()
                for word in banned:
                    self.assertNotIn(word, name, f"{table}.{row[1]}")
        conn.close()

    def test_the_gateway_interface_takes_no_credentials(self):
        """
        `connect()` takes no arguments by design. An adapter reads its
        own secrets from the environment, so they never appear in a
        call site, a log line or a serialised request.
        """
        import inspect
        from src.execution.gateway import BrokerGateway
        signature = inspect.signature(BrokerGateway.connect)
        self.assertEqual(list(signature.parameters), ["self"])

    def test_the_gateway_interface_has_no_login_method(self):
        from src.execution.gateway import BrokerGateway
        for name in ("login", "authenticate", "set_credentials", "sign_in"):
            self.assertFalse(hasattr(BrokerGateway, name), name)

    def test_broker_records_carry_no_endpoint_or_credential(self):
        broker = Broker(broker_id="b", name="B")
        for name in ("endpoint", "url", "host", "api_key", "token",
                     "password", "secret"):
            self.assertFalse(hasattr(broker, name), name)


if __name__ == "__main__":
    unittest.main()
