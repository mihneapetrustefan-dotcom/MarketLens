"""
tests/adaptive/test_freshness_and_legacy.py
--------------------------------------------------------
Phase 25.9I-A: delayed data may think, not act -- and nothing moved.

The second half of this file is the part that matters most: it proves
the phase changed no production behaviour. Contracts that are wrong can
be fixed later; a capture session silently rerouted, or a broker write
introduced by a foundation phase, cannot.
"""

import ast
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import flags as adaptive_flags
from src.adaptive.freshness import (
    ANALYSIS_DECISIONS, DecisionKind, EXECUTION_SENSITIVE_DECISIONS,
    ExecutionFreshnessPolicy,
)
from src.domain.adaptive_models import MarketDataState
from src.domain.market_data_models import MarketDataAvailability
from src.domain.paper_models import DataFreshness

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class TestExecutionFreshnessPolicy(unittest.TestCase):
    """§3.D -- the rule the last four capture sessions were judged by."""

    def setUp(self):
        self.policy = ExecutionFreshnessPolicy()

    def test_delayed_data_may_screen_and_rank(self):
        for decision in (DecisionKind.SCREEN, DecisionKind.RANK,
                         DecisionKind.DETECT_OPPORTUNITY,
                         DecisionKind.MONITOR_LONG_HORIZON):
            verdict = self.policy.permits(
                decision, freshness=DataFreshness.AGING,
                availability=MarketDataAvailability.DELAYED, age_seconds=900.0)
            self.assertTrue(verdict, f"{decision.value} should accept delayed data")

    def test_delayed_data_may_not_execute(self):
        for decision in sorted(EXECUTION_SENSITIVE_DECISIONS, key=lambda d: d.value):
            verdict = self.policy.permits(
                decision, freshness=DataFreshness.AGING,
                availability=MarketDataAvailability.DELAYED, age_seconds=900.0)
            self.assertFalse(verdict, f"{decision.value} must refuse delayed data")
            self.assertIn("delayed", verdict.reason)

    def test_every_execution_sensitive_decision_requires_hot_data(self):
        for decision in EXECUTION_SENSITIVE_DECISIONS:
            requirement = self.policy.requirement_for(decision)
            self.assertIs(requirement.state, MarketDataState.HOT)
            self.assertFalse(requirement.permits_delayed)

    def test_analysis_decisions_require_nothing_hot(self):
        for decision in ANALYSIS_DECISIONS:
            requirement = self.policy.requirement_for(decision)
            self.assertIs(requirement.state, MarketDataState.COLD)
            self.assertTrue(requirement.permits_delayed)

    def test_fresh_realtime_data_may_execute(self):
        verdict = self.policy.permits(
            DecisionKind.BUY, freshness=DataFreshness.FRESH,
            availability=MarketDataAvailability.AVAILABLE, age_seconds=2.0)
        self.assertTrue(verdict)

    def test_an_unmeasured_age_cannot_execute(self):
        verdict = self.policy.permits(
            DecisionKind.SELL, freshness=DataFreshness.FRESH,
            availability=MarketDataAvailability.AVAILABLE, age_seconds=None)
        self.assertFalse(verdict)
        self.assertIn("never measured", verdict.reason)

    def test_a_missing_realtime_marker_cannot_execute(self):
        verdict = self.policy.permits(
            DecisionKind.BUY, freshness=DataFreshness.FRESH, age_seconds=1.0)
        self.assertFalse(verdict)
        self.assertIn("marker", verdict.reason)

    def test_a_future_timestamp_cannot_execute(self):
        # The 2026-09-21 case: a host clock 674 s behind the venue.
        verdict = self.policy.permits(
            DecisionKind.MODIFY_STOP, freshness=DataFreshness.FRESH,
            availability=MarketDataAvailability.AVAILABLE, age_seconds=-674.0)
        self.assertFalse(verdict)
        self.assertIn("future", verdict.reason)

    def test_an_old_quote_cannot_execute_even_when_marked_live(self):
        verdict = self.policy.permits(
            DecisionKind.BUY, freshness=DataFreshness.FRESH,
            availability=MarketDataAvailability.AVAILABLE, age_seconds=120.0)
        self.assertFalse(verdict)

    def test_no_data_at_all_refuses_everything(self):
        for decision in (DecisionKind.SCREEN, DecisionKind.BUY):
            self.assertFalse(self.policy.permits(decision))

    def test_invalid_quotes_are_refused_even_for_analysis(self):
        self.assertFalse(self.policy.permits(
            DecisionKind.SCREEN, freshness=DataFreshness.INVALID,
            availability=MarketDataAvailability.DELAYED, age_seconds=10.0))

    def test_the_policy_reports_itself_inactive(self):
        self.assertFalse(self.policy.describe()["active"])


class TestLegacyBehaviourUnchanged(unittest.TestCase):
    """The phase's central claim: nothing in production moved."""

    def test_the_capture_universe_artifact_is_untouched(self):
        path = os.path.join(ROOT, "config", "capture_universe_v1.json")
        with open(path, encoding="utf-8") as handle:
            universe = json.load(handle)
        self.assertEqual(universe.get("version"), "v1")
        members = universe.get("members") or universe.get("instruments") or []
        self.assertEqual(len(members), 31)

    def test_no_adaptive_module_imports_an_execution_or_broker_path(self):
        forbidden_roots = ("src.execution", "src.trading")
        adaptive_dir = os.path.join(ROOT, "src", "adaptive")
        for name in sorted(os.listdir(adaptive_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(adaptive_dir, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    for root in forbidden_roots:
                        self.assertFalse(
                            module.startswith(root),
                            f"src/adaptive/{name} imports {module}; the adaptive "
                            f"layer may not reach execution or trading")

    def test_no_adaptive_module_mentions_an_order_submission_call(self):
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"),
                 ("flags.py", "cost_governor.py", "freshness.py")),
                (os.path.join(ROOT, "src", "domain"),
                 ("adaptive_models.py", "thesis_models.py"))):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                calls = {node.func.attr for node in ast.walk(tree)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)}
                for forbidden in ("place_order", "submit_order", "cancel_order",
                                  "modify_order", "reply"):
                    self.assertNotIn(forbidden, calls, f"{name} calls {forbidden}")

    def test_the_existing_execution_safety_gate_is_unchanged(self):
        from src.execution.safety import ExecutionSafety
        self.assertFalse(ExecutionSafety().allow_real_orders)

    def test_no_feature_flag_can_reach_risk_or_execution(self):
        # Every flag on, including the master switch: still no path from
        # this layer to a broker, because nothing here imports one.
        environment = {var: "true" for var in adaptive_flags.FLAG_ENV.values()}
        environment[adaptive_flags.ENV_LAYER] = "true"
        resolved = adaptive_flags.resolve(environment)
        self.assertTrue(resolved.any_enabled)
        described = resolved.describe()
        self.assertFalse(described["enables_broker_writes"])
        self.assertFalse(described["enables_live_trading"])

    def test_the_status_script_reports_an_inactive_layer(self):
        environment = dict(os.environ)
        for var in list(adaptive_flags.FLAG_ENV.values()) + [adaptive_flags.ENV_LAYER]:
            environment.pop(var, None)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "adaptive_status.py"),
             "--json"],
            capture_output=True, text=True, env=environment, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertFalse(report["adaptive_layer"]["any_enabled"])
        self.assertFalse(report["safety"]["broker_writes_introduced"])
        self.assertFalse(report["safety"]["live_trading_enabled"])
        self.assertFalse(report["safety"]["d20_touched"])
        self.assertEqual(report["safety"]["paper_orders_created_by_this_layer"], 0)

    def test_the_status_script_writes_nothing(self):
        capture_db = os.path.join(ROOT, "data", "capture", "intraday_capture.db")
        before = os.path.getmtime(capture_db) if os.path.exists(capture_db) else None
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        subprocess.run([sys.executable,
                        os.path.join(ROOT, "scripts", "adaptive_status.py")],
                       capture_output=True, text=True, env=environment, timeout=120)
        after = os.path.getmtime(capture_db) if os.path.exists(capture_db) else None
        self.assertEqual(before, after, "the diagnostic must not touch the store")

    def test_persistence_is_added_only_where_a_phase_justified_it(self):
        # 25.9I-A deferred persistence entirely and this test asserted
        # that. Each later phase that justifies a schema module adds it
        # here deliberately -- 25.9I-B the session universe, 25.9I-D the
        # shadow allocation plans, 25.9I-E the position theses, 25.9I-C
        # the market-data request ledger -- so the list is a record of
        # what was argued for, not a rubber stamp. A module appearing
        # without a phase behind it still fails here.
        #
        # 25.9I-C's entry earns its place on one fact: IBKR updates its
        # snapshot counter once per business day, so during a session
        # the provider's meter reads zero and a local ledger is the only
        # thing that can bound a spend.
        #
        # 25.9I-F's earns its place on a different one: novelty is the
        # only part of that phase that CANNOT be recomputed. Relevance,
        # direction, materiality and urgency are pure functions of the
        # event and the thesis, but whether a thesis has already reacted
        # to an event is a fact about history -- and unwritten, the
        # system revises a position every time the crawler re-runs.
        data_access = os.path.join(ROOT, "src", "data_access")
        adaptive_modules = sorted(name for name in os.listdir(data_access)
                                  if "adaptive" in name.lower())
        self.assertEqual(adaptive_modules, ["adaptive_allocation_schema.py",
                                            "adaptive_event_schema.py",
                                            "adaptive_market_data_schema.py",
                                            "adaptive_thesis_schema.py",
                                            "adaptive_universe_schema.py"])


class TestD20Untouched(unittest.TestCase):
    """§17 -- the protected experiment stays sealed."""

    #: Names that would mean the protected experiment was read, run or
    #: reasoned about. Asserting it was untouched is not one of them,
    #: which is why the bare string "d20" is not on this list.
    PROTECTED_ARTEFACTS = ("anchor-v2", "anchor_v2", "validate_d20",
                           "protected_ledger", "return_60d", "d20_result",
                           "d20_outcome")

    def test_no_adaptive_code_references_the_protected_experiment(self):
        for directory, names in (
                (os.path.join(ROOT, "src", "adaptive"),
                 ("flags.py", "cost_governor.py", "freshness.py")),
                (os.path.join(ROOT, "src", "domain"),
                 ("adaptive_models.py", "thesis_models.py")),
                (os.path.join(ROOT, "scripts"), ("adaptive_status.py",))):
            for name in names:
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    source = handle.read().lower()
                for forbidden in self.PROTECTED_ARTEFACTS:
                    self.assertNotIn(forbidden, source,
                                     f"{name} mentions {forbidden}")

    #: The protected ledger lives in `src/research/protected_ledger.py`, and
    #: the `src.research` package is where the protected machinery sits, so
    #: adaptive code must not reach into it.
    #:
    #: This was originally written as "no imported module name may contain
    #: the word research", which is a proxy rather than the rule. Phase
    #: 25.9I-F.7 needs `src.domain.research_models.DatasetVersion` to version
    #: a corpus, and that module holds only generic contracts --
    #: DatasetVersion, FeatureValue, LabelValue, ResearchObservation -- that
    #: the whole research pipeline shares. It carries no protected outcome
    #: and importing it cannot read one.
    #:
    #: So the check now names the package it means. What guards the real
    #: risk is unchanged and stricter: `value_json` appears in no F.7
    #: module, no F.7 file names a d20 label, and `protected_ledger` is
    #: forbidden outright below.
    PROTECTED_PACKAGES = ("src.research", "research.protected_ledger",
                          "protected_ledger")

    def test_the_protected_ledger_is_not_imported_by_this_phase(self):
        for directory in ("adaptive", "domain"):
            base = os.path.join(ROOT, "src", directory)
            for name in sorted(os.listdir(base)):
                if not name.endswith(".py") or (
                        directory == "domain"
                        and name not in ("adaptive_models.py", "thesis_models.py")):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        for forbidden in self.PROTECTED_PACKAGES:
                            self.assertFalse(
                                module == forbidden
                                or module.startswith(forbidden + "."),
                                f"{name} imports {module}")


if __name__ == "__main__":
    unittest.main()
