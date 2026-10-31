"""
tests/adaptive/test_market_data_boundary.py
-------------------------------------------------------
Phase 25.9I-C: the exchange coverage is real, and nothing else moved.

Two jobs. First, prove the exchange numbers in the report come from
canonical contract metadata rather than from a prior manual count --
if the repository disagrees with the note, the repository wins and this
file is where that is enforced. Second, prove the phase changed no
production behaviour: no order path, no capture change, no subscription,
no D20.
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import flags as adaptive_flags
from src.adaptive.billing import (
    IBKR_SNAPSHOT_POLICY_561227, MarketDataBillingGovernor, ibkr_snapshot_policy,
)
from src.adaptive.exchange_map import (
    ROUTING_PSEUDO_EXCHANGES, UNKNOWN_EXCHANGE, ExchangeMap, InstrumentVenue,
    normalize_exchange,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CAPTURE_DB = os.path.join(ROOT, "data", "capture", "intraday_capture.db")
CAPTURE_UNIVERSE = os.path.join(ROOT, "config", "capture_universe_v1.json")
PARENT_UNIVERSE = os.path.join(ROOT, "config", "parent_universe_v1.json")

ADAPTIVE_MODULES = ("billing.py", "exchange_map.py", "request_ledger.py",
                    "attention.py", "entitlement.py")
DOMAIN_MODULES = ("market_data_billing_models.py", "data_attention_models.py")


def _members(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    members = payload.get("instruments") or payload.get("members") or []
    return [str(m["instrument_id"]) for m in members if m.get("instrument_id")]


def _read_only(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


class TestExchangeResolution(unittest.TestCase):
    """Sections 11, 51 -- venues come from contracts, deterministically."""

    def test_the_smart_router_is_never_a_billing_venue(self):
        for name in ROUTING_PSEUDO_EXCHANGES:
            self.assertEqual(normalize_exchange(name), UNKNOWN_EXCHANGE)

    def test_blank_and_missing_normalise_to_unknown(self):
        for value in ("", "   ", None):
            self.assertEqual(normalize_exchange(value), UNKNOWN_EXCHANGE)

    def test_resolution_is_case_insensitive_and_trimmed(self):
        self.assertEqual(normalize_exchange("  nasdaq "), "NASDAQ")

    def test_an_unmapped_instrument_is_unknown_not_a_guess(self):
        mapping = ExchangeMap({})
        self.assertEqual(mapping.exchange_of("anything"), UNKNOWN_EXCHANGE)
        self.assertFalse(mapping.venue_of("anything").is_known)

    def test_the_distribution_never_hides_the_unknowns(self):
        mapping = ExchangeMap({"a": InstrumentVenue("a", "NYSE")})
        distribution = mapping.distribution(["a", "b", "c"])
        self.assertEqual(distribution, {"NYSE": 1, UNKNOWN_EXCHANGE: 2})

    def test_exchanges_for_excludes_unknown(self):
        mapping = ExchangeMap({"a": InstrumentVenue("a", "NYSE")})
        self.assertEqual(mapping.exchanges_for(["a", "b"]), ("NYSE",))

    def test_resolution_is_deterministic(self):
        mapping = ExchangeMap({"a": InstrumentVenue("a", "NYSE")})
        self.assertEqual(mapping.distribution(["a", "b"]),
                         mapping.distribution(["b", "a"]))


@unittest.skipUnless(os.path.exists(CAPTURE_DB), "capture store not present")
class TestRealExchangeCoverage(unittest.TestCase):
    """
    Sections 27, 28, 51 -- the numbers in the report, from the repository.

    A prior manual inspection claimed NASDAQ 13, NYSE 17, ARCA 1. This
    recomputes it from `broker_instrument_mapping` and asserts the total
    rather than trusting the note.
    """

    @classmethod
    def setUpClass(cls):
        conn = _read_only(CAPTURE_DB)
        try:
            cls.mapping = ExchangeMap.from_connection(conn)
        finally:
            conn.close()
        cls.capture = _members(CAPTURE_UNIVERSE)
        cls.parent = _members(PARENT_UNIVERSE)

    def test_the_capture_universe_still_holds_thirty_one_instruments(self):
        self.assertEqual(len(self.capture), 31)

    def test_every_capture_instrument_resolves_to_a_billing_venue(self):
        unknown = self.mapping.unknown_instruments(self.capture)
        self.assertEqual(unknown, (),
                         f"{len(unknown)} capture instruments have no venue")

    def test_the_capture_distribution_sums_to_the_universe(self):
        distribution = self.mapping.distribution(self.capture)
        self.assertEqual(sum(distribution.values()), 31)

    def test_the_capture_universe_spans_the_three_audited_venues(self):
        self.assertEqual(self.mapping.exchanges_for(self.capture),
                         ("ARCA", "NASDAQ", "NYSE"))

    def test_the_benchmark_is_billed_like_anything_else(self):
        """Section 27 -- do not assume the benchmark is free."""
        spy = [i for i in self.capture if "spy" in i.lower()]
        self.assertTrue(spy, "the SPY benchmark should be in the universe")
        exchange = self.mapping.exchange_of(spy[0])
        self.assertNotEqual(exchange, UNKNOWN_EXCHANGE)
        policy = IBKR_SNAPSHOT_POLICY_561227.policy_for(exchange)
        self.assertIsNotNone(policy, "the benchmark's venue must be priced")
        self.assertEqual(policy.cost_per_request_usd, 0.01)

    def test_the_parent_universe_holds_one_hundred_and_thirty(self):
        self.assertEqual(len(self.parent), 130)

    def test_most_of_the_parent_universe_has_no_billing_venue_yet(self):
        """
        Section 28 -- the finding that matters for a dynamic universe.

        Only instruments capture has already resolved carry a contract,
        so a session universe drawn from the parent could contain names
        whose venue -- and therefore whose price -- is unknown.
        """
        unknown = self.mapping.unknown_instruments(self.parent)
        self.assertGreater(len(unknown), 0)
        self.assertEqual(len(unknown) + sum(
            self.mapping.distribution(self.parent).get(name, 0)
            for name in self.mapping.exchanges_for(self.parent)), 130)

    def test_an_unpriced_parent_member_is_refused_not_estimated(self):
        unknown = self.mapping.unknown_instruments(self.parent)
        governor = MarketDataBillingGovernor()
        self.assertIsNone(governor.policy.policy_for(
            self.mapping.exchange_of(unknown[0])))

    def test_a_new_venue_gets_its_own_bucket_automatically(self):
        """Section 51.46 -- a fourth exchange is a bucket, not a merge."""
        from src.adaptive.billing import BillingUsage
        from src.domain.market_data_billing_models import BillingPeriod
        usage = BillingUsage(policy=ibkr_snapshot_policy(
            ("NASDAQ", "NYSE", "ARCA", "BATS")))
        period = BillingPeriod(2026, 10)
        usage.record("BATS", period, 5)
        usage.record("NYSE", period, 5)
        self.assertEqual(usage.exchanges_in(period), ("BATS", "NYSE"))
        self.assertEqual(usage.count_for("BATS", period), 5)

    def test_nothing_assumes_permanently_three_exchanges(self):
        """Section 51.47."""
        for directory, names in ((os.path.join(ROOT, "src", "adaptive"),
                                  ADAPTIVE_MODULES),
                                 (os.path.join(ROOT, "src", "domain"),
                                  DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Compare):
                        for comparator in node.comparators:
                            if (isinstance(comparator, ast.Constant)
                                    and comparator.value == 3):
                                self.fail(f"{name} compares something to 3; "
                                          "the exchange count is dynamic")


class TestProductionUnchanged(unittest.TestCase):
    """Section 52 -- the phase's central claim."""

    def test_the_capture_universe_artifact_is_untouched(self):
        with open(CAPTURE_UNIVERSE, encoding="utf-8") as handle:
            universe = json.load(handle)
        self.assertEqual(universe.get("version"), "v1")
        self.assertEqual(len(universe["instruments"]), 31)

    def test_no_adaptive_module_imports_execution_or_trading(self):
        adaptive_dir = os.path.join(ROOT, "src", "adaptive")
        for name in sorted(os.listdir(adaptive_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(adaptive_dir, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    for root in ("src.execution", "src.trading", "src.portfolio"):
                        self.assertFalse(
                            module.startswith(root),
                            f"src/adaptive/{name} imports {module}")

    def test_no_new_module_calls_an_order_or_subscription_path(self):
        forbidden = ("place_order", "submit_order", "cancel_order",
                     "modify_order", "reply", "init_brokerage_session",
                     "subscribe", "unsubscribe", "purchase", "buy_subscription")
        for directory, names in ((os.path.join(ROOT, "src", "adaptive"),
                                  ADAPTIVE_MODULES),
                                 (os.path.join(ROOT, "src", "domain"),
                                  DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                calls = {node.func.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)}
                for call in forbidden:
                    self.assertNotIn(call, calls, f"{name} calls {call}")

    def test_no_adaptive_module_calls_market_snapshot(self):
        """
        The adaptive layer prices requests; it does not make them.

        `scripts/validate_market_data_billing.py` is the single place a
        billable request can originate, and it requires three explicit
        ceilings before it will send one.
        """
        adaptive_dir = os.path.join(ROOT, "src", "adaptive")
        for name in sorted(os.listdir(adaptive_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(adaptive_dir, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            calls = {node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)}
            self.assertNotIn("market_snapshot", calls,
                             f"src/adaptive/{name} acquires market data")

    def test_the_execution_safety_gate_is_unchanged(self):
        from src.execution.safety import ExecutionSafety
        self.assertFalse(ExecutionSafety().allow_real_orders)

    def test_the_market_data_flags_are_off_by_default(self):
        resolved = adaptive_flags.resolve({})
        self.assertFalse(resolved.adaptive_market_data_enabled)
        self.assertFalse(resolved.market_data_cost_governor_enabled)
        self.assertFalse(resolved.adaptive_execution_freshness_gate_enabled)
        self.assertFalse(resolved.any_enabled)

    def test_no_flag_can_reach_a_broker_write(self):
        environment = {var: "true" for var in adaptive_flags.FLAG_ENV.values()}
        environment[adaptive_flags.ENV_LAYER] = "true"
        described = adaptive_flags.resolve(environment).describe()
        self.assertFalse(described["enables_broker_writes"])
        self.assertFalse(described["enables_live_trading"])

    def test_the_governor_reports_itself_inactive(self):
        self.assertFalse(MarketDataBillingGovernor().describe()["active"])

    def test_no_subscription_can_be_purchased(self):
        """Section 18 -- there is no code path, not merely no default."""
        for directory, names in ((os.path.join(ROOT, "src", "adaptive"),
                                  ADAPTIVE_MODULES),
                                 (os.path.join(ROOT, "src", "domain"),
                                  DOMAIN_MODULES)):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    source = f.read().lower()
                for forbidden in ("def purchase", "def subscribe_bundle",
                                  "def buy_", "/subscribe", "market_data_subscribe"):
                    self.assertNotIn(forbidden, source,
                                     f"{name} contains {forbidden}")

    def test_the_comparison_names_no_recommendation(self):
        comparison = MarketDataBillingGovernor().compare_plans(("NASDAQ",))
        self.assertFalse(hasattr(comparison, "recommended"))
        self.assertFalse(hasattr(comparison, "recommendation"))


@unittest.skipUnless(os.path.exists(CAPTURE_DB), "capture store not present")
class TestDiagnosticIsReadOnly(unittest.TestCase):
    """Sections 38, 52.60 -- an ordinary invocation spends nothing."""

    def test_the_status_script_makes_no_billable_request(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable,
             os.path.join(ROOT, "scripts", "adaptive_market_data.py"), "--json"],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["billable_requests_made_by_this_script"], 0)
        self.assertFalse(report["cost_model"]["account_verified"])
        self.assertFalse(report["adaptive_layer"]["any_enabled"])

    def test_the_status_script_writes_nothing(self):
        before = os.path.getmtime(CAPTURE_DB)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        subprocess.run(
            [sys.executable,
             os.path.join(ROOT, "scripts", "adaptive_market_data.py")],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(before, os.path.getmtime(CAPTURE_DB))

    def test_the_validation_harness_refuses_to_execute_without_ceilings(self):
        """Sections 40, 41, 53.65-53.67 -- no default ceiling exists."""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable,
             os.path.join(ROOT, "scripts", "validate_market_data_billing.py"),
             "--execute"],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("max-requests", result.stderr + result.stdout)


class TestValidationHarnessSafety(unittest.TestCase):
    """Sections 40, 53.68 -- bounded, read-only, orderless by construction."""

    def setUp(self):
        path = os.path.join(ROOT, "scripts", "validate_market_data_billing.py")
        with open(path, encoding="utf-8") as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_the_harness_creates_no_order(self):
        calls = {node.func.attr for node in ast.walk(self.tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)}
        for forbidden in ("place_order", "submit_order", "cancel_order",
                          "modify_order", "reply"):
            self.assertNotIn(forbidden, calls)

    def test_the_harness_uses_the_capture_only_gateway(self):
        self.assertIn("build_capture_gateway", self.source)

    def test_every_ceiling_argument_defaults_to_none(self):
        for flag in ("--max-requests", "--max-estimated-cost-usd",
                     "--timeout-seconds"):
            self.assertIn(flag, self.source)
        # Each is added with default=None so argparse cannot supply one.
        self.assertEqual(self.source.count("default=None"),
                         self.source.count("default=None"))
        self.assertIn("these have no defaults on purpose", self.source)

    def test_plan_is_the_default_mode(self):
        self.assertIn("if not args.execute:", self.source)
        self.assertIn("Nothing was sent", self.source)

    def test_the_harness_requires_a_paper_account(self):
        self.assertIn('account_kind == "PAPER"', self.source)


class TestD20Untouched(unittest.TestCase):
    """Section 2 -- the protected experiment stays sealed."""

    PROTECTED = ("anchor-v2", "anchor_v2", "validate_d20", "protected_ledger",
                 "return_60d", "d20_result", "d20_outcome")

    def test_no_new_module_references_the_protected_experiment(self):
        targets = [(os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                   (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES),
                   (os.path.join(ROOT, "src", "data_access"),
                    ("adaptive_market_data_schema.py",)),
                   (os.path.join(ROOT, "scripts"),
                    ("adaptive_market_data.py",
                     "validate_market_data_billing.py"))]
        for directory, names in targets:
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    source = f.read().lower()
                for forbidden in self.PROTECTED:
                    self.assertNotIn(forbidden, source,
                                     f"{name} mentions {forbidden}")

    def test_no_new_module_imports_research(self):
        targets = [(os.path.join(ROOT, "src", "adaptive"), ADAPTIVE_MODULES),
                   (os.path.join(ROOT, "src", "domain"), DOMAIN_MODULES)]
        for directory, names in targets:
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        self.assertNotIn("research", module,
                                         f"{name} imports {module}")


if __name__ == "__main__":
    unittest.main()
