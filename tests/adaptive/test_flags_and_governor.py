"""
tests/adaptive/test_flags_and_governor.py
------------------------------------------------------
Phase 25.9I-A: the switches are off, and the meter refuses to guess.

The cost tests are written against the open question they came from: on
2026-09-24 IBKR's assistant gave two contradictory answers about
per-snapshot billing for ~250,000 monthly API requests, a difference of
roughly USD 15 against USD 2,500 a month. A governor that treats an
unknown price as free would have approved the expensive answer.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import flags as adaptive_flags
from src.adaptive.cost_governor import (
    CostConfigurationError, DEFAULT_MONTHLY_BUDGET_USD, MarketDataCostGovernor,
    MarketDataCostModel,
)
from src.domain.adaptive_models import BudgetPolicy, MarketDataBudgetState

UTC = timezone.utc


class TestFlagDefaults(unittest.TestCase):

    def test_every_flag_is_off_in_an_empty_environment(self):
        resolved = adaptive_flags.resolve({})
        self.assertFalse(resolved.any_enabled)
        self.assertFalse(resolved.layer_enabled)
        for name in adaptive_flags.FLAG_ENV:
            self.assertFalse(resolved.is_enabled(name), name)

    def test_the_dataclass_itself_defaults_to_off(self):
        # Constructed with no arguments at all, not merely resolved off.
        blank = adaptive_flags.AdaptiveFlags()
        self.assertFalse(blank.any_enabled)
        self.assertFalse(blank.layer_enabled)

    def test_a_flag_can_be_turned_on_explicitly(self):
        resolved = adaptive_flags.resolve(
            {adaptive_flags.FLAG_ENV["adaptive_universe_enabled"]: "true"})
        self.assertTrue(resolved.adaptive_universe_enabled)
        self.assertEqual(resolved.enabled_flags, ("adaptive_universe_enabled",))

    def test_an_unreadable_value_fails_closed_and_is_reported(self):
        resolved = adaptive_flags.resolve(
            {adaptive_flags.FLAG_ENV["position_thesis_enabled"]: "maybe"})
        self.assertFalse(resolved.position_thesis_enabled)
        self.assertTrue(any("maybe" in problem for problem in resolved.problems))

    def test_dependencies_suppress_rather_than_half_enable(self):
        resolved = adaptive_flags.resolve(
            {adaptive_flags.FLAG_ENV["dynamic_exit_enabled"]: "1"})
        self.assertFalse(resolved.dynamic_exit_enabled)
        self.assertTrue(any("position_thesis_enabled" in s
                            for s in resolved.suppressed))

    def test_a_dependency_satisfied_lets_the_flag_through(self):
        resolved = adaptive_flags.resolve({
            adaptive_flags.FLAG_ENV["dynamic_exit_enabled"]: "1",
            adaptive_flags.FLAG_ENV["position_thesis_enabled"]: "1"})
        self.assertTrue(resolved.dynamic_exit_enabled)
        self.assertEqual(resolved.suppressed, ())

    def test_realtime_escalation_requires_the_cost_governor(self):
        resolved = adaptive_flags.resolve(
            {adaptive_flags.FLAG_ENV["adaptive_market_data_enabled"]: "yes"})
        self.assertFalse(resolved.adaptive_market_data_enabled)

    def test_the_master_switch_enables_nothing_by_itself(self):
        resolved = adaptive_flags.resolve({adaptive_flags.ENV_LAYER: "true"})
        self.assertTrue(resolved.layer_enabled)
        self.assertFalse(resolved.any_enabled)

    def test_describe_states_that_no_flag_reaches_the_broker(self):
        described = adaptive_flags.resolve({}).describe()
        self.assertFalse(described["enables_broker_writes"])
        self.assertFalse(described["enables_live_trading"])

    def test_unknown_flag_names_are_refused(self):
        with self.assertRaises(KeyError):
            adaptive_flags.resolve({}).is_enabled("not_a_flag")

    def test_flags_module_imports_nothing_that_can_write_to_a_broker(self):
        # Imports, not prose: the docstring names the modules it stays
        # away from, and a substring search would flag that as a breach.
        import ast
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "src", "adaptive", "flags.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertEqual(imported, ["__future__", "os", "dataclasses", "typing"])


class TestCostModelConfiguration(unittest.TestCase):

    def test_unset_prices_are_unknown_not_zero(self):
        model = MarketDataCostModel()
        self.assertIsNone(model.per_snapshot_cost_usd)
        self.assertIn("per_snapshot_cost_usd", model.unknown_prices())
        self.assertFalse(model.can_price(snapshots=10))

    def test_the_budget_default_is_configurable(self):
        self.assertEqual(MarketDataCostModel().monthly_budget_usd,
                         DEFAULT_MONTHLY_BUDGET_USD)
        self.assertEqual(MarketDataCostModel(monthly_budget_usd=5.0)
                         .monthly_budget_usd, 5.0)
        from_env = MarketDataCostModel.from_environment(
            {"MARKETLENS_MDCOST_MONTHLY_BUDGET_USD": "42.5"})
        self.assertEqual(from_env.monthly_budget_usd, 42.5)

    def test_invalid_configuration_fails_closed(self):
        for bad in ("abc", "-1", "nan"):
            with self.assertRaises(CostConfigurationError):
                MarketDataCostModel.from_environment(
                    {"MARKETLENS_MDCOST_PER_SNAPSHOT_USD": bad})
        with self.assertRaises(CostConfigurationError):
            MarketDataCostModel(monthly_budget_usd=-1.0)

    def test_no_provider_pricing_is_hard_coded(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "src",
                            "adaptive", "cost_governor.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        # The known IBKR numbers may appear in prose, never as values.
        for number in ("0.01", "4.50", "10.00", "14.50"):
            self.assertNotIn("= " + number, code)


class TestCostGovernor(unittest.TestCase):

    def governor(self, **model):
        return MarketDataCostGovernor(MarketDataCostModel(**model))

    def test_an_unpriceable_plan_is_blocked_not_approved(self):
        governor = self.governor()                       # prices unknown
        estimate = governor.estimate(instrument_count=31, snapshots=12000)
        verdict = governor.assess(
            MarketDataBudgetState(month="2026-09", budget_usd=20.0), estimate)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)
        self.assertIn("cannot price", verdict.reason)
        self.assertFalse(verdict.permits_spending)

    def test_the_expensive_answer_is_blocked_against_a_20_dollar_budget(self):
        # 12,000 snapshots a day at USD 0.01 is USD 120: the answer that
        # would have been approved by a governor assuming zero.
        governor = self.governor(per_snapshot_cost_usd=0.01, monthly_budget_usd=20.0)
        estimate = governor.estimate(instrument_count=31, snapshots=12000)
        self.assertEqual(estimate.total_usd, 120.0)
        verdict = governor.assess(
            MarketDataBudgetState(month="2026-09", budget_usd=20.0), estimate)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)

    def test_the_cheap_answer_is_allowed(self):
        governor = self.governor(per_snapshot_cost_usd=0.0,
                                 fixed_monthly_subscription_cost_usd=14.5,
                                 monthly_budget_usd=20.0)
        estimate = governor.estimate(instrument_count=31, snapshots=250000,
                                     include_fixed=True)
        self.assertEqual(estimate.total_usd, 14.5)
        verdict = governor.assess(
            MarketDataBudgetState(month="2026-09", budget_usd=20.0), estimate)
        self.assertIn(verdict.policy, (BudgetPolicy.ALLOW, BudgetPolicy.WARN))

    def test_warn_then_throttle_then_block_as_the_budget_fills(self):
        governor = MarketDataCostGovernor(
            MarketDataCostModel(per_request_cost_usd=1.0, monthly_budget_usd=100.0),
            warn_fraction=0.7, throttle_fraction=0.9)
        budget = lambda spent: MarketDataBudgetState(
            month="2026-09", budget_usd=100.0, spent_usd=spent)
        plan = governor.estimate(requests=10)
        self.assertIs(governor.assess(budget(0.0), plan).policy, BudgetPolicy.ALLOW)
        self.assertIs(governor.assess(budget(65.0), plan).policy, BudgetPolicy.WARN)
        self.assertIs(governor.assess(budget(85.0), plan).policy, BudgetPolicy.THROTTLE)
        self.assertIs(governor.assess(budget(95.0), plan).policy, BudgetPolicy.BLOCK)

    def test_estimates_are_deterministic(self):
        governor = self.governor(per_request_cost_usd=0.002)
        first = governor.estimate(instrument_count=31, requests=12000)
        second = governor.estimate(instrument_count=31, requests=12000)
        self.assertEqual(first, second)
        self.assertEqual(first.total_usd, 24.0)

    def test_a_free_allowance_is_applied_once(self):
        governor = self.governor(per_snapshot_cost_usd=0.01, free_allowance_usd=1.0)
        estimate = governor.estimate(snapshots=200)
        self.assertEqual(estimate.total_usd, 1.0)      # 2.00 less the 1.00 waiver

    def test_no_budget_configured_blocks(self):
        governor = self.governor(per_request_cost_usd=0.0)
        verdict = governor.assess(
            MarketDataBudgetState(month="2026-09", budget_usd=0.0),
            governor.estimate(requests=1))
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)

    def test_negative_plan_sizes_are_refused(self):
        with self.assertRaises(CostConfigurationError):
            self.governor().estimate(requests=-1)

    def test_governor_thresholds_are_validated(self):
        with self.assertRaises(CostConfigurationError):
            MarketDataCostGovernor(warn_fraction=0.9, throttle_fraction=0.5)

    def test_budget_state_helper_reports_its_own_policy(self):
        governor = self.governor(monthly_budget_usd=20.0)
        state = governor.budget_state("2026-09", spent_usd=19.0)
        self.assertIs(state.policy, BudgetPolicy.THROTTLE)
        self.assertEqual(state.remaining_usd, 1.0)

    def test_the_governor_reports_itself_inactive_in_this_phase(self):
        self.assertFalse(self.governor().describe()["active"])


if __name__ == "__main__":
    unittest.main()
