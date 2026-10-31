"""
tests/adaptive/test_capital_allocation.py
------------------------------------------------------
Phase 25.9I-D: the allocator is allowed to say no.

Every test here is an attempt to make the allocator spend money it
should not spend, or to make it treat something unknown as something
safe. The fixtures are synthetic and deterministic; real-data behaviour
is exercised through the CLI in the boundary suite.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.allocator import (
    AllocationConfig, SmartCapitalAllocator, all_cash_baseline,
    equal_weight_baseline,
)
from src.adaptive.desirability import DesirabilityConfig, DesirabilityEngine
from src.adaptive.risk_budget import (
    PositionRiskEstimator, RiskBudgetState, RiskConfig,
)
from src.domain.adaptive_models import AllocationRejectionReason
from src.domain.allocation_models import (
    AllocationCandidate, AllocationLineStatus, AllocationPlanStatus,
    CapitalDesirability, CapitalStateStatus, ExistingExposure,
    PortfolioCapitalState, PositionRiskEstimate, RiskEstimateStatus,
)

UTC = timezone.utc
AT = datetime(2026, 9, 29, 13, 25, tzinfo=UTC)


def desirability(instrument_id, overall, completeness_unavailable=2):
    return CapitalDesirability(
        instrument_id=instrument_id, overall=overall,
        positives={"opportunity_rank": overall, "liquidity": 0.8},
        penalties={"volatility": 0.2},
        weights={"opportunity_rank": 0.45, "liquidity": 0.30, "volatility": 0.30},
        unavailable=tuple("u%d" % i for i in range(completeness_unavailable)),
        method_version="test")


def risk(instrument_id, per_dollar=0.05, status=RiskEstimateStatus.MEASURED):
    if status is RiskEstimateStatus.UNKNOWN:
        return PositionRiskEstimate(
            instrument_id=instrument_id, status=status, method="none",
            notes=("no estimate available",))
    return PositionRiskEstimate(
        instrument_id=instrument_id, status=status, risk_per_dollar=per_dollar,
        method="test")


def candidate(instrument_id, overall=0.7, per_dollar=0.05, sector="technology",
              risk_status=RiskEstimateStatus.MEASURED):
    return AllocationCandidate(
        instrument_id=instrument_id, as_of=AT,
        desirability=desirability(instrument_id, overall),
        risk=risk(instrument_id, per_dollar, risk_status), sector_id=sector,
        session_snapshot_id="su-test", admission_decision_id="adm-" + instrument_id)


def state(equity=100.0, cash=None, holds=()):
    return PortfolioCapitalState(
        as_of=AT, account_equity=equity,
        cash=equity if cash is None else cash,
        existing_exposures=tuple(holds))


def allocator(**config):
    risk_kwargs = {k: config.pop(k) for k in list(config)
                   if k in ("account_risk_budget_usd", "per_position_risk_budget_usd",
                            "unknown_risk_notional_cap_usd", "volatility_multiple")}
    minimum = config.pop("minimum_desirability", 0.25)
    return SmartCapitalAllocator(AllocationConfig(**config),
                                 RiskConfig(**risk_kwargs),
                                 minimum_desirability=minimum)


class TestDeploymentIsNeverForced(unittest.TestCase):

    def test_one_strong_candidate_does_not_receive_the_whole_account(self):
        plan = allocator().allocate([candidate("a", 0.95)], state(100.0), AT)
        line = plan.line("a")
        self.assertLess(line.allocated_notional, 100.0)
        self.assertLessEqual(line.allocated_notional, 20.0)   # position ceiling
        self.assertGreater(plan.retained_cash, 0)

    def test_multiple_candidates_are_not_equally_weighted(self):
        plan = allocator(minimum_allocation_usd=0.0).allocate(
            [candidate("a", 0.9), candidate("b", 0.6), candidate("c", 0.4)],
            state(1000.0), AT)
        amounts = {line.instrument_id: line.allocated_notional
                   for line in plan.lines}
        self.assertNotEqual(len({round(v, 4) for v in amounts.values()}), 1,
                            "equal weighting is not smart allocation")

    def test_a_better_candidate_receives_more_all_else_equal(self):
        plan = allocator(minimum_allocation_usd=0.0).allocate(
            [candidate("weak", 0.4), candidate("strong", 0.9)], state(1000.0), AT)
        self.assertGreater(plan.line("strong").allocated_notional,
                           plan.line("weak").allocated_notional)

    def test_higher_risk_reduces_allocation(self):
        plan = allocator(minimum_allocation_usd=0.0,
                         per_position_risk_budget_usd=2.0).allocate(
            [candidate("safe", 0.7, per_dollar=0.02),
             candidate("risky", 0.7, per_dollar=0.20)], state(1000.0), AT)
        self.assertGreater(plan.line("safe").allocated_notional,
                           plan.line("risky").allocated_notional)
        self.assertTrue(any(c.constraint == "per_position_risk_budget"
                            for c in plan.line("risky").constraints_applied))

    def test_a_candidate_may_receive_zero(self):
        plan = allocator(minimum_desirability=0.5).allocate(
            [candidate("a", 0.9), candidate("weak", 0.3)], state(100.0), AT)
        weak = plan.line("weak")
        self.assertEqual(weak.allocated_notional, 0.0)
        self.assertIs(weak.status, AllocationLineStatus.NOT_FUNDED)
        self.assertTrue(weak.reasons)

    def test_all_candidates_may_receive_zero_and_the_plan_is_valid(self):
        plan = allocator(minimum_desirability=0.9).allocate(
            [candidate("a", 0.5), candidate("b", 0.4)], state(100.0), AT)
        self.assertTrue(plan.is_all_cash)
        self.assertEqual(plan.deployed, 0.0)
        self.assertEqual(plan.retained_cash, 100.0)
        self.assertIn(plan.status, (AllocationPlanStatus.COMPLETE,
                                    AllocationPlanStatus.PARTIAL))

    def test_no_candidates_is_a_complete_all_cash_plan(self):
        plan = allocator().allocate([], state(100.0), AT)
        self.assertTrue(plan.is_all_cash)
        self.assertTrue(plan.gaps)
        self.assertTrue(plan.check_identity())

    def test_max_deployment_is_a_ceiling_not_a_target(self):
        plan = allocator(max_deployment_fraction=0.7,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("a", 0.9)], state(100.0), AT)
        self.assertLess(plan.deployed, 70.0)
        self.assertLessEqual(plan.deployment_fraction, 0.7)


class TestConstraints(unittest.TestCase):

    def test_single_position_cap_binds(self):
        plan = allocator(max_position_fraction=0.10).allocate(
            [candidate("a", 0.95)], state(100.0), AT)
        self.assertLessEqual(plan.line("a").allocated_notional, 10.0)
        self.assertTrue(any(c.constraint == "max_position_fraction"
                            for c in plan.line("a").constraints_applied))

    def test_sector_cap_binds_across_candidates(self):
        candidates = [candidate("t%d" % i, 0.9 - i * 0.01, sector="technology")
                      for i in range(5)]
        plan = allocator(max_sector_fraction=0.25, max_position_fraction=0.20,
                         minimum_allocation_usd=0.0).allocate(
            candidates, state(1000.0), AT)
        self.assertLessEqual(plan.sector_allocation().get("technology", 0.0),
                             250.0 + 1e-6)

    def test_a_sector_cap_leaves_cash_rather_than_funding_a_weak_name(self):
        candidates = [candidate("t%d" % i, 0.9, sector="technology")
                      for i in range(3)]
        candidates.append(candidate("weak", 0.10, sector="healthcare"))
        plan = allocator(max_sector_fraction=0.15, minimum_desirability=0.5,
                         minimum_allocation_usd=0.0).allocate(
            candidates, state(1000.0), AT)
        self.assertEqual(plan.line("weak").allocated_notional, 0.0)
        self.assertGreater(plan.unallocated, 0.0)

    def test_account_risk_budget_limits_aggregate_risk(self):
        candidates = [candidate("c%d" % i, 0.8, per_dollar=0.10) for i in range(6)]
        plan = allocator(account_risk_budget_usd=5.0, minimum_allocation_usd=0.0,
                         max_position_fraction=1.0).allocate(
            candidates, state(1000.0), AT)
        self.assertLessEqual(plan.total_capital_at_risk, 5.0 + 1e-6)

    def test_per_position_risk_budget_limits_one_line(self):
        plan = allocator(per_position_risk_budget_usd=1.0,
                         minimum_allocation_usd=0.0,
                         max_position_fraction=1.0).allocate(
            [candidate("a", 0.9, per_dollar=0.10)], state(1000.0), AT)
        self.assertLessEqual(plan.line("a").capital_at_risk, 1.0 + 1e-6)

    def test_existing_exposure_reduces_available_capacity(self):
        holds = (ExistingExposure("a", notional=15.0, sector_id="technology",
                                  capital_at_risk=0.5),)
        plan = allocator(max_position_fraction=0.20).allocate(
            [candidate("a", 0.95)], state(100.0, holds=holds), AT)
        self.assertLessEqual(plan.line("a").allocated_notional, 5.0 + 1e-6)

    def test_existing_sector_exposure_reduces_new_allocation(self):
        holds = (ExistingExposure("held", notional=30.0, sector_id="technology",
                                  capital_at_risk=1.0),)
        plan = allocator(max_sector_fraction=0.35,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("a", 0.9, sector="technology")],
            state(100.0, holds=holds), AT)
        self.assertLessEqual(plan.line("a").allocated_notional, 5.0 + 1e-6)

    def test_the_deployment_ceiling_counts_what_is_already_held(self):
        holds = (ExistingExposure("held", notional=60.0, sector_id="energy",
                                  capital_at_risk=2.0),)
        plan = allocator(max_deployment_fraction=0.60).allocate(
            [candidate("a", 0.9)], state(100.0, cash=40.0, holds=holds), AT)
        self.assertEqual(plan.deployable_capital, 0.0)
        self.assertTrue(plan.is_all_cash)


class TestUnknownsAreNotZero(unittest.TestCase):

    def test_unknown_downside_is_not_treated_as_risk_free(self):
        plan = allocator(unknown_risk_notional_cap_usd=5.0,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("unknown", 0.9, risk_status=RiskEstimateStatus.UNKNOWN)],
            state(1000.0), AT)
        line = plan.line("unknown")
        self.assertLessEqual(line.allocated_notional, 5.0)
        self.assertIsNone(line.capital_at_risk)
        self.assertTrue(any("UNKNOWN" in r for r in line.reasons))

    def test_unknown_downside_without_a_cap_receives_nothing(self):
        plan = allocator(minimum_allocation_usd=0.0).allocate(
            [candidate("unknown", 0.9, risk_status=RiskEstimateStatus.UNKNOWN)],
            state(1000.0), AT)
        self.assertEqual(plan.line("unknown").allocated_notional, 0.0)

    def test_a_plan_with_unknown_funded_risk_reports_total_risk_as_unknown(self):
        plan = allocator(unknown_risk_notional_cap_usd=5.0,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("known", 0.9, per_dollar=0.05),
             candidate("unknown", 0.85, risk_status=RiskEstimateStatus.UNKNOWN)],
            state(1000.0), AT)
        self.assertIsNone(plan.total_capital_at_risk)
        self.assertIs(plan.status, AllocationPlanStatus.PARTIAL)

    def test_a_risk_estimate_of_exactly_zero_is_refused(self):
        with self.assertRaises(ValueError):
            PositionRiskEstimate(instrument_id="a",
                                 status=RiskEstimateStatus.MEASURED,
                                 risk_per_dollar=0.0)

    def test_an_unknown_estimate_may_not_carry_a_number(self):
        with self.assertRaises(ValueError):
            PositionRiskEstimate(instrument_id="a",
                                 status=RiskEstimateStatus.UNKNOWN,
                                 risk_per_dollar=0.05, notes=("x",))

    def test_unknown_existing_risk_makes_remaining_budget_unknown(self):
        budget = RiskBudgetState(account_budget_usd=10.0, existing_risk_usd=None)
        self.assertIsNone(budget.remaining_usd)
        self.assertFalse(budget.is_measurable)
        self.assertFalse(budget.would_exceed(99.0))

    def test_no_expected_return_is_fabricated(self):
        assessed = DesirabilityEngine().assess(
            "a", opportunity_score=0.8, score_completeness=0.5,
            liquidity_notional=1e7, volatility=0.02,
            existing_exposure_fraction=0.0, sector_exposure_fraction=0.0)
        self.assertIn("expected_return", assessed.unavailable)
        self.assertIn("model_confidence", assessed.unavailable)
        self.assertNotIn("expected_return", assessed.positives)
        self.assertLess(assessed.completeness, 1.0)

    def test_unknown_risk_is_itself_a_penalty(self):
        engine = DesirabilityEngine()
        common = dict(opportunity_score=0.8, score_completeness=0.5,
                      liquidity_notional=1e7, volatility=0.02,
                      existing_exposure_fraction=0.0, sector_exposure_fraction=0.0)
        known = engine.assess("a", risk_is_known=True, **common)
        unknown = engine.assess("a", risk_is_known=False, **common)
        self.assertLess(unknown.overall, known.overall)
        self.assertEqual(unknown.penalties["risk_uncertainty"], 1.0)

    def test_no_kelly_sizing_exists_anywhere_in_the_allocator(self):
        import src.adaptive.allocator as module
        source = open(module.__file__, encoding="utf-8").read().lower()
        self.assertNotIn("kelly", source)


class TestDeterminism(unittest.TestCase):

    def test_same_inputs_give_the_same_plan(self):
        candidates = [candidate("a", 0.8), candidate("b", 0.6)]
        first = allocator().allocate(candidates, state(500.0), AT)
        second = allocator().allocate(candidates, state(500.0), AT)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual([(l.instrument_id, l.allocated_notional) for l in first.lines],
                         [(l.instrument_id, l.allocated_notional) for l in second.lines])

    def test_candidate_order_does_not_change_the_economics(self):
        candidates = [candidate("a", 0.8), candidate("b", 0.6), candidate("c", 0.7)]
        forward = allocator().allocate(candidates, state(500.0), AT)
        reverse = allocator().allocate(list(reversed(candidates)), state(500.0), AT)
        self.assertEqual(
            {l.instrument_id: l.allocated_notional for l in forward.lines},
            {l.instrument_id: l.allocated_notional for l in reverse.lines})
        self.assertEqual(forward.plan_id, reverse.plan_id)

    def test_no_allocation_is_negative_or_non_finite(self):
        plan = allocator().allocate(
            [candidate("a", 0.9), candidate("b", 0.1)], state(100.0), AT)
        for line in plan.lines:
            self.assertGreaterEqual(line.allocated_notional, 0.0)
            self.assertEqual(line.allocated_notional, line.allocated_notional)
            self.assertNotIn(line.allocated_notional, (float("inf"), float("-inf")))


class TestAccounting(unittest.TestCase):

    def test_the_identity_reconciles(self):
        plan = allocator(account_risk_budget_usd=5.0).allocate(
            [candidate("a", 0.9), candidate("b", 0.7), candidate("c", 0.5)],
            state(1000.0), AT)
        self.assertTrue(plan.check_identity())

    def test_no_overspending_is_possible(self):
        candidates = [candidate("c%d" % i, 0.9) for i in range(20)]
        plan = allocator(minimum_allocation_usd=0.0).allocate(
            candidates, state(100.0), AT)
        self.assertLessEqual(plan.deployed, plan.deployable_capital + 1e-9)
        self.assertLessEqual(plan.deployed, 100.0)

    def test_cash_reserve_survives_allocation(self):
        plan = allocator(minimum_cash_fraction=0.25,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("a", 0.95)], state(100.0), AT)
        self.assertGreaterEqual(plan.reserved, 25.0)
        self.assertGreaterEqual(plan.retained_cash, 25.0)

    def test_an_opportunity_reserve_is_held_back_separately(self):
        plan = allocator(minimum_cash_fraction=0.10,
                         opportunity_reserve_fraction=0.20,
                         minimum_allocation_usd=0.0).allocate(
            [candidate("a", 0.95)], state(100.0), AT)
        self.assertEqual(len(plan.cash_reserves), 2)
        self.assertGreaterEqual(plan.reserved, 30.0)

    def test_a_plan_cannot_deploy_more_than_deployable(self):
        from src.domain.allocation_models import AllocationLine, PortfolioAllocationPlan
        line = AllocationLine(
            instrument_id="a", status=AllocationLineStatus.FUNDED,
            requested_notional=50.0, allocated_notional=50.0,
            desirability=desirability("a", 0.9), risk=risk("a"))
        with self.assertRaises(ValueError):
            PortfolioAllocationPlan(
                plan_id="p", as_of=AT, status=AllocationPlanStatus.COMPLETE,
                capital_state=state(100.0), deployable_capital=10.0,
                lines=(line,))


class TestSmallAccounts(unittest.TestCase):

    def test_a_hundred_dollar_account_produces_viable_positions(self):
        candidates = [candidate("c%d" % i, 0.9 - i * 0.02) for i in range(20)]
        plan = allocator(minimum_allocation_usd=5.0,
                         max_position_fraction=0.20).allocate(
            candidates, state(100.0), AT)
        for line in plan.funded_lines:
            self.assertGreaterEqual(line.allocated_notional, 5.0)
        self.assertTrue(plan.check_identity())

    def test_skip_policy_funds_nothing_when_shares_are_too_small(self):
        candidates = [candidate("c%d" % i, 0.9) for i in range(20)]
        plan = allocator(min_size_policy="skip",
                         minimum_allocation_usd=5.0).allocate(
            candidates, state(100.0), AT)
        self.assertTrue(plan.is_all_cash)
        for line in plan.lines:
            self.assertTrue(any(r.value == "below_minimum_size"
                                for r in line.rejection_reasons)
                            or line.reasons)

    def test_larger_accounts_fund_proportionally_not_identically(self):
        candidates = [candidate("a", 0.9), candidate("b", 0.5)]
        small = allocator().allocate(candidates, state(100.0), AT)
        large = allocator().allocate(candidates, state(500.0), AT)
        self.assertGreater(large.deployed, small.deployed)

    def test_no_share_quantity_is_ever_produced(self):
        plan = allocator().allocate([candidate("a", 0.9)], state(100.0), AT)
        for line in plan.lines:
            for forbidden in ("quantity", "shares", "order_id", "venue"):
                self.assertFalse(hasattr(line, forbidden))


class TestBaselines(unittest.TestCase):

    def test_equal_weight_baseline_is_available_for_comparison_only(self):
        candidates = [candidate("a", 0.9), candidate("b", 0.3)]
        baseline = equal_weight_baseline(candidates, 60.0)
        self.assertEqual(set(baseline.values()), {30.0})
        smart = allocator(minimum_allocation_usd=0.0).allocate(
            candidates, state(100.0), AT)
        self.assertNotEqual(
            {l.instrument_id: l.allocated_notional for l in smart.lines}, baseline)

    def test_all_cash_baseline_deploys_nothing(self):
        self.assertEqual(set(all_cash_baseline([candidate("a")], 60.0).values()),
                         {0.0})


class TestCapitalState(unittest.TestCase):

    def test_a_state_without_capital_blocks_the_plan(self):
        empty = PortfolioCapitalState(as_of=AT, account_equity=0.0, cash=0.0)
        plan = allocator().allocate([candidate("a", 0.9)], empty, AT)
        self.assertIs(plan.status, AllocationPlanStatus.BLOCKED)
        self.assertTrue(plan.gaps)
        self.assertTrue(plan.is_all_cash)

    def test_an_insufficient_data_state_blocks_the_plan(self):
        unknown = PortfolioCapitalState(
            as_of=AT, account_equity=100.0, cash=100.0,
            status=CapitalStateStatus.INSUFFICIENT_DATA,
            gaps=("account balance could not be read",))
        plan = allocator().allocate([candidate("a", 0.9)], unknown, AT)
        self.assertIs(plan.status, AllocationPlanStatus.BLOCKED)

    def test_existing_risk_is_unknown_when_any_holding_is_unmeasured(self):
        partial = state(100.0, holds=(
            ExistingExposure("a", 10.0, "technology", capital_at_risk=0.5),
            ExistingExposure("b", 10.0, "energy")))
        self.assertIsNone(partial.existing_capital_at_risk)

    def test_a_partial_state_must_state_its_gaps(self):
        with self.assertRaises(ValueError):
            PortfolioCapitalState(as_of=AT, account_equity=1.0, cash=1.0,
                                  status=CapitalStateStatus.PARTIAL)


class TestExplainability(unittest.TestCase):

    def test_every_zero_allocation_records_a_reason(self):
        candidates = [candidate("c%d" % i, 0.9 - i * 0.1) for i in range(6)]
        plan = allocator(minimum_desirability=0.5,
                         account_risk_budget_usd=1.0).allocate(
            candidates, state(100.0), AT)
        for line in plan.lines:
            if line.allocated_notional == 0:
                self.assertTrue(line.reasons or line.rejection_reasons,
                                line.instrument_id)

    def test_every_reduction_names_the_constraint_that_caused_it(self):
        plan = allocator(max_position_fraction=0.05).allocate(
            [candidate("a", 0.95)], state(1000.0), AT)
        line = plan.line("a")
        self.assertIs(line.status, AllocationLineStatus.REDUCED)
        self.assertTrue(line.constraints_applied)
        self.assertGreater(line.reduction, 0)

    def test_a_reduced_line_without_a_constraint_is_refused(self):
        from src.domain.allocation_models import AllocationLine
        with self.assertRaises(ValueError):
            AllocationLine(instrument_id="a", status=AllocationLineStatus.REDUCED,
                           requested_notional=10.0, allocated_notional=5.0,
                           desirability=desirability("a", 0.9), risk=risk("a"))

    def test_a_zero_line_without_a_reason_is_refused(self):
        from src.domain.allocation_models import AllocationLine
        with self.assertRaises(ValueError):
            AllocationLine(instrument_id="a",
                           status=AllocationLineStatus.NOT_FUNDED,
                           requested_notional=10.0, allocated_notional=0.0,
                           desirability=desirability("a", 0.9), risk=risk("a"))

    def test_desirability_contributions_are_inspectable(self):
        assessed = DesirabilityEngine().assess(
            "a", opportunity_score=0.8, score_completeness=0.6,
            liquidity_notional=5e7, volatility=0.03,
            existing_exposure_fraction=0.1, sector_exposure_fraction=0.2)
        contributions = assessed.contributions()
        self.assertTrue(contributions)
        self.assertTrue(any(value < 0 for _, value in contributions),
                        "penalties must appear as negative contributions")

    def test_lineage_reaches_back_to_the_session_universe(self):
        plan = allocator().allocate([candidate("a", 0.9)], state(100.0), AT)
        line = plan.line("a")
        self.assertEqual(line.session_snapshot_id, "su-test")
        self.assertEqual(line.admission_decision_id, "adm-a")


class TestRiskEstimation(unittest.TestCase):

    def test_an_invalidation_level_gives_a_measured_estimate(self):
        estimate = PositionRiskEstimator().estimate(
            "a", reference_price=100.0, invalidation_price=92.0)
        self.assertIs(estimate.status, RiskEstimateStatus.MEASURED)
        self.assertAlmostEqual(estimate.risk_per_dollar, 0.08, places=6)
        self.assertEqual(estimate.capital_at_risk(50.0), 4.0)

    def test_volatility_alone_is_only_partial(self):
        estimate = PositionRiskEstimator().estimate("a", daily_volatility=0.02)
        self.assertIs(estimate.status, RiskEstimateStatus.PARTIAL)
        self.assertTrue(estimate.notes)

    def test_nothing_defensible_gives_unknown(self):
        estimate = PositionRiskEstimator().estimate("a")
        self.assertIs(estimate.status, RiskEstimateStatus.UNKNOWN)
        self.assertIsNone(estimate.risk_per_dollar)
        self.assertIsNone(estimate.capital_at_risk(100.0))

    def test_an_invalidation_above_the_reference_price_is_unknown(self):
        estimate = PositionRiskEstimator().estimate(
            "a", reference_price=100.0, invalidation_price=105.0)
        self.assertIs(estimate.status, RiskEstimateStatus.UNKNOWN)

    def test_equal_notional_can_carry_different_risk(self):
        estimator = PositionRiskEstimator()
        calm = estimator.estimate("calm", daily_volatility=0.01)
        wild = estimator.estimate("wild", daily_volatility=0.06)
        self.assertLess(calm.capital_at_risk(20.0), wild.capital_at_risk(20.0))

    def test_no_production_risk_percentage_is_assumed(self):
        config = RiskConfig()
        self.assertIsNone(config.account_risk_budget_usd)
        self.assertIsNone(config.per_position_risk_budget_usd)
        self.assertIsNone(config.unknown_risk_notional_cap_usd)


if __name__ == "__main__":
    unittest.main()
