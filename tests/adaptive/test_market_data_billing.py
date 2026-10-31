"""
tests/adaptive/test_market_data_billing.py
------------------------------------------------------
Phase 25.9I-C: what a request costs, and what it does not prove.

The boundary cases at 149/150/151/10,000 are the whole reason this
phase exists, so they are asserted directly rather than through a
helper that could smooth over an off-by-one at exactly the point where
the money stops.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.billing import (
    BillableRequestPlan, BillingUsage, MarketDataBillingGovernor,
    ibkr_snapshot_policy, IBKR_SNAPSHOT_POLICY_561227,
    entitlement_expected_after_cap,
)
from src.adaptive.exchange_map import UNKNOWN_EXCHANGE
from src.adaptive.request_ledger import (
    RequestLedger, RequestRefused, reconcile_portal_counter,
)
from src.domain.adaptive_models import BudgetPolicy
from src.domain.data_attention_models import AttentionPriority
from src.domain.market_data_billing_models import (
    BillingPeriod, CapState, EntitlementStatus, ExchangeBillingPolicy,
    ExchangeBillingState, PricingConfidence, ProviderBillingPolicy,
    RequestCountingBasis,
)

UTC = timezone.utc
OCTOBER = BillingPeriod(2026, 10)
SEPTEMBER = BillingPeriod(2026, 9)
NOON = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)


class TestPolicyNumbers(unittest.TestCase):
    """Section 48.1-48.7 -- the arithmetic of the cap."""

    def setUp(self):
        self.policy = IBKR_SNAPSHOT_POLICY_561227.policy_for("NASDAQ")

    def test_the_configured_price_is_one_cent(self):
        self.assertEqual(self.policy.cost_per_request_usd, 0.01)

    def test_the_threshold_is_one_hundred_and_fifty(self):
        self.assertEqual(self.policy.request_threshold, 150)

    def test_the_cap_is_one_dollar_fifty(self):
        self.assertEqual(self.policy.max_variable_cost_usd, 1.50)

    def test_the_cost_curve_at_every_boundary(self):
        for count, expected in ((0, 0.0), (1, 0.01), (149, 1.49),
                                (150, 1.50), (151, 1.50), (10000, 1.50)):
            self.assertAlmostEqual(self.policy.cost_for(count), expected,
                                   places=6, msg=f"{count} requests")

    def test_the_cap_state_flips_exactly_at_the_threshold(self):
        self.assertIs(self.policy.cap_state_for(149), CapState.BELOW_THRESHOLD)
        self.assertIs(self.policy.cap_state_for(150), CapState.CAP_REACHED)
        self.assertIs(self.policy.cap_state_for(151), CapState.CAP_REACHED)

    def test_requests_until_cap_never_goes_negative(self):
        self.assertEqual(self.policy.requests_until_cap(0), 150)
        self.assertEqual(self.policy.requests_until_cap(149), 1)
        self.assertEqual(self.policy.requests_until_cap(10000), 0)

    def test_a_negative_count_is_refused(self):
        with self.assertRaises(ValueError):
            self.policy.cost_for(-1)

    def test_a_threshold_without_a_ceiling_is_refused(self):
        # Half a policy describes nothing: after the threshold, what?
        with self.assertRaises(ValueError):
            ExchangeBillingPolicy("NYSE", 0.01, request_threshold=150)
        with self.assertRaises(ValueError):
            ExchangeBillingPolicy("NYSE", 0.01, max_variable_cost_usd=1.5)


class TestPerExchangeIsolation(unittest.TestCase):
    """Section 48.8 and section 11 -- one counter per venue, never a shared one."""

    def setUp(self):
        self.usage = BillingUsage(policy=IBKR_SNAPSHOT_POLICY_561227)

    def test_capping_one_exchange_leaves_another_untouched(self):
        for _ in range(200):
            self.usage.record("NYSE", OCTOBER, 1, NOON)
        self.usage.record("NASDAQ", OCTOBER, 5, NOON)

        nyse = self.usage.state_for("NYSE", OCTOBER)
        nasdaq = self.usage.state_for("NASDAQ", OCTOBER)
        self.assertIs(nyse.cap_state, CapState.CAP_REACHED)
        self.assertEqual(nyse.estimated_cost_usd, 1.50)
        self.assertIs(nasdaq.cap_state, CapState.BELOW_THRESHOLD)
        self.assertEqual(nasdaq.estimated_cost_usd, 0.05)

    def test_the_total_sums_the_independent_venues(self):
        self.usage.record("NYSE", OCTOBER, 200, NOON)
        self.usage.record("NASDAQ", OCTOBER, 200, NOON)
        self.usage.record("ARCA", OCTOBER, 200, NOON)
        total, unpriceable = self.usage.total_variable_cost_usd(OCTOBER)
        self.assertEqual(unpriceable, ())
        self.assertAlmostEqual(total, 4.50, places=6)

    def test_an_uncapped_figure_records_the_size_of_the_bet(self):
        self.usage.record("NYSE", OCTOBER, 12000, NOON)
        state = self.usage.state_for("NYSE", OCTOBER)
        self.assertEqual(state.estimated_cost_usd, 1.50)
        self.assertEqual(state.uncapped_cost_usd, 120.0)


class TestMonthlyPeriods(unittest.TestCase):
    """Section 48.9, 48.10 and section 10 -- months reset; history survives."""

    def test_september_and_october_are_separate_counters(self):
        usage = BillingUsage(policy=IBKR_SNAPSHOT_POLICY_561227)
        usage.record("NYSE", SEPTEMBER, 200, datetime(2026, 9, 30, tzinfo=UTC))
        usage.record("NYSE", OCTOBER, 3, NOON)
        self.assertEqual(usage.count_for("NYSE", SEPTEMBER), 200)
        self.assertEqual(usage.count_for("NYSE", OCTOBER), 3)
        self.assertIs(usage.state_for("NYSE", SEPTEMBER).cap_state,
                      CapState.CAP_REACHED)
        self.assertIs(usage.state_for("NYSE", OCTOBER).cap_state,
                      CapState.BELOW_THRESHOLD)

    def test_history_is_not_deleted_when_a_new_month_starts(self):
        usage = BillingUsage(policy=IBKR_SNAPSHOT_POLICY_561227)
        usage.record("NYSE", SEPTEMBER, 10, datetime(2026, 9, 30, tzinfo=UTC))
        usage.record("NYSE", OCTOBER, 10, NOON)
        self.assertEqual(usage.periods(), ("2026-09", "2026-10"))

    def test_month_arithmetic_crosses_a_year_boundary(self):
        self.assertEqual(BillingPeriod(2026, 12).next().key, "2027-01")
        self.assertEqual(BillingPeriod(2026, 1).previous().key, "2025-12")

    def test_a_moment_lands_in_its_own_month(self):
        period = BillingPeriod.for_moment(datetime(2026, 10, 31, 23, 59,
                                                   tzinfo=UTC))
        self.assertEqual(period.key, "2026-10")
        self.assertTrue(period.contains(datetime(2026, 10, 1, tzinfo=UTC)))
        self.assertFalse(period.contains(datetime(2026, 11, 1, tzinfo=UTC)))

    def test_an_unresolvable_billing_timezone_is_refused_not_guessed(self):
        # The provider did not state one. A period boundary off by a day
        # files requests in the wrong month.
        with self.assertRaises(ValueError):
            BillingPeriod.for_moment(NOON, timezone_name="America/New_York")

    def test_the_timezone_assumption_is_declared(self):
        self.assertTrue(IBKR_SNAPSHOT_POLICY_561227.billing_timezone_assumed)


class TestUnknownIsNeverZero(unittest.TestCase):
    """Section 48.11, 48.12 and section 21."""

    def setUp(self):
        self.governor = MarketDataBillingGovernor()
        self.usage = BillingUsage(policy=self.governor.policy)

    def test_an_unconfigured_exchange_has_no_policy(self):
        self.assertIsNone(self.governor.policy.policy_for("IEX"))
        self.assertIsNone(self.governor.policy.policy_for(""))
        self.assertIsNone(self.governor.policy.policy_for(None))

    def test_an_unknown_exchange_blocks_rather_than_costing_nothing(self):
        plan = BillableRequestPlan(requests_by_exchange={"IEX": 1})
        verdict = self.governor.assess(plan, self.usage, OCTOBER)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)
        self.assertIn("IEX", verdict.unpriceable_exchanges)
        self.assertNotEqual(verdict.incremental_cost_usd, 0.0)

    def test_an_unpriceable_state_reports_none_not_zero(self):
        state = ExchangeBillingState("IEX", OCTOBER, request_count=50)
        self.assertIsNone(state.estimated_cost_usd)
        self.assertIsNone(state.uncapped_cost_usd)
        self.assertIs(state.cap_state, CapState.UNKNOWN)

    def test_a_ceiling_is_none_when_any_venue_is_unbounded(self):
        ceiling, unbounded = self.governor.policy.maximum_known_variable_cost_usd(
            ("NYSE", "IEX"))
        self.assertIsNone(ceiling)
        self.assertEqual(unbounded, ("IEX",))

    def test_the_conservative_fallback_is_the_dearest_and_uncapped(self):
        governor = MarketDataBillingGovernor(conservative_unknown_pricing=True)
        usage = BillingUsage(policy=governor.policy)
        # 1000 x 0.01 = 10.00 with NO cap applied. A capped venue would
        # have charged 1.50 for the same thousand requests, so the
        # stand-in is nearly 7x dearer -- which is the point of it.
        verdict = governor.assess(
            BillableRequestPlan(requests_by_exchange={"IEX": 1000}),
            usage, OCTOBER)
        self.assertEqual(verdict.incremental_cost_usd, 10.0)
        self.assertGreater(verdict.incremental_cost_usd,
                           governor.policy.policy_for("NYSE").cost_for(1000))
        # ... and it therefore runs out of budget where a capped venue
        # never would: 3000 requests is 30 USD against a 20 USD budget.
        blocked = governor.assess(
            BillableRequestPlan(requests_by_exchange={"IEX": 3000}),
            usage, OCTOBER)
        self.assertIs(blocked.policy, BudgetPolicy.BLOCK)


class TestBudget(unittest.TestCase):
    """Section 48.13-48.17 and sections 19, 20, 44."""

    def setUp(self):
        self.governor = MarketDataBillingGovernor()
        self.usage = BillingUsage(policy=self.governor.policy)

    def test_the_default_budget_is_the_users_twenty_dollars(self):
        self.assertEqual(self.governor.monthly_budget_usd, 20.0)

    def test_three_capped_exchanges_fit_the_budget(self):
        forecast = self.governor.forecast_month(("NASDAQ", "NYSE", "ARCA"))
        self.assertEqual(forecast["variable_ceiling_usd"], 4.50)
        self.assertEqual(forecast["total_worst_case_usd"], 4.50)
        self.assertTrue(forecast["fits_budget"])

    def test_the_forecast_grows_with_a_fourth_exchange(self):
        # Section 17: nothing assumes there are permanently three.
        policy = ibkr_snapshot_policy(("NASDAQ", "NYSE", "ARCA", "BATS"))
        governor = MarketDataBillingGovernor(policy)
        forecast = governor.forecast_month(("NASDAQ", "NYSE", "ARCA", "BATS"))
        self.assertEqual(forecast["exchange_count"], 4)
        self.assertEqual(forecast["variable_ceiling_usd"], 6.0)

    def test_a_fixed_subscription_is_added_to_the_forecast(self):
        governor = MarketDataBillingGovernor(
            ibkr_snapshot_policy(fixed_monthly_subscription_cost_usd=14.5))
        forecast = governor.forecast_month(("NASDAQ", "NYSE", "ARCA"))
        self.assertEqual(forecast["total_worst_case_usd"], 19.0)
        self.assertTrue(forecast["fits_budget"])

    def test_a_subscription_that_breaks_the_budget_is_reported_as_such(self):
        governor = MarketDataBillingGovernor(
            ibkr_snapshot_policy(fixed_monthly_subscription_cost_usd=30.0))
        forecast = governor.forecast_month(("NASDAQ",))
        self.assertFalse(forecast["fits_budget"])

    def test_an_unset_subscription_price_stays_unknown(self):
        self.assertIsNone(
            IBKR_SNAPSHOT_POLICY_561227.fixed_monthly_subscription_cost_usd)
        comparison = self.governor.compare_plans(("NASDAQ", "NYSE", "ARCA"))
        described = comparison.describe()
        self.assertIsNone(described["plan_b_fixed_subscription"]["monthly_cost_usd"])
        self.assertTrue(any("subscription price" in u
                            for u in described["unknowns"]))

    def test_the_comparison_recommends_nothing(self):
        described = self.governor.compare_plans(("NASDAQ",)).describe()
        self.assertNotIn("recommended", described)
        self.assertIn("no purchase path exists", described["note"])

    def test_a_plan_beyond_the_budget_is_blocked(self):
        governor = MarketDataBillingGovernor(monthly_budget_usd=0.10)
        usage = BillingUsage(policy=governor.policy)
        plan = BillableRequestPlan(requests_by_exchange={"NYSE": 100})
        verdict = governor.assess(plan, usage, OCTOBER)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)

    def test_warn_and_throttle_appear_before_block(self):
        governor = MarketDataBillingGovernor(monthly_budget_usd=2.0)
        usage = BillingUsage(policy=governor.policy)
        usage.record("NYSE", OCTOBER, 145, NOON)          # 1.45 spent
        verdict = governor.assess(
            BillableRequestPlan(requests_by_exchange={"NYSE": 1}), usage, OCTOBER)
        self.assertIn(verdict.policy, (BudgetPolicy.WARN, BudgetPolicy.THROTTLE))

    def test_a_request_on_a_capped_venue_costs_nothing_further(self):
        self.usage.record("NYSE", OCTOBER, 150, NOON)
        verdict = self.governor.assess(
            BillableRequestPlan(requests_by_exchange={"NYSE": 500}),
            self.usage, OCTOBER)
        self.assertEqual(verdict.incremental_cost_usd, 0.0)
        self.assertIs(verdict.policy, BudgetPolicy.ALLOW)

    def test_the_incremental_cost_is_correct_across_the_threshold(self):
        self.usage.record("NYSE", OCTOBER, 149, NOON)
        verdict = self.governor.assess(
            BillableRequestPlan(requests_by_exchange={"NYSE": 10}),
            self.usage, OCTOBER)
        # 1.49 -> 1.50, not 1.49 -> 1.59.
        self.assertAlmostEqual(verdict.incremental_cost_usd, 0.01, places=6)

    def test_an_urgent_exit_refusal_is_escalated_not_absorbed(self):
        """Section 20 -- the rule that must never resolve into delayed data."""
        governor = MarketDataBillingGovernor(monthly_budget_usd=0.001)
        usage = BillingUsage(policy=governor.policy)
        verdict = governor.assess(
            BillableRequestPlan(requests_by_exchange={"NYSE": 50},
                                priority=AttentionPriority.POSITION_EXIT),
            usage, OCTOBER)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)
        self.assertTrue(verdict.safety_conflict)
        self.assertEqual(verdict.conflict_code, "BUDGET_CONFLICT")
        self.assertIn("delayed data is NOT an acceptable substitute",
                      verdict.reason)

    def test_a_screening_refusal_is_not_escalated(self):
        governor = MarketDataBillingGovernor(monthly_budget_usd=0.001)
        verdict = governor.assess(
            BillableRequestPlan(requests_by_exchange={"NYSE": 50},
                                priority=AttentionPriority.BROAD_SCREENING),
            BillingUsage(policy=governor.policy), OCTOBER)
        self.assertIs(verdict.policy, BudgetPolicy.BLOCK)
        self.assertFalse(verdict.safety_conflict)


class TestPolicyIsNotVerification(unittest.TestCase):
    """Section 48.19, 50.34-50.35 and section 22 -- the central distinction."""

    def test_the_configured_policy_is_provider_stated_not_verified(self):
        self.assertIs(IBKR_SNAPSHOT_POLICY_561227.confidence,
                      PricingConfidence.PROVIDER_STATED)
        self.assertIsNot(IBKR_SNAPSHOT_POLICY_561227.confidence,
                         PricingConfidence.ACCOUNT_VERIFIED)

    def test_the_governor_reports_the_account_as_unverified(self):
        self.assertFalse(MarketDataBillingGovernor().describe()["account_verified"])

    def test_the_policy_names_its_evidence(self):
        self.assertIn("561227", IBKR_SNAPSHOT_POLICY_561227.evidence_source)
        self.assertEqual(IBKR_SNAPSHOT_POLICY_561227.evidence_date, "2026-09-29")

    def test_a_confident_policy_without_evidence_is_refused(self):
        with self.assertRaises(ValueError):
            ProviderBillingPolicy(provider="X",
                                  confidence=PricingConfidence.PROVIDER_STATED)

    def test_the_expectation_after_cap_is_an_expectation(self):
        expected = entitlement_expected_after_cap(
            IBKR_SNAPSHOT_POLICY_561227, "NASDAQ")
        self.assertIs(expected, EntitlementStatus.REALTIME_VERIFIED)
        # ... and reaching the cap does not itself produce that status.
        usage = BillingUsage(policy=IBKR_SNAPSHOT_POLICY_561227)
        usage.record("NASDAQ", OCTOBER, 150, NOON)
        state = usage.state_for("NASDAQ", OCTOBER)
        self.assertIs(state.cap_state, CapState.CAP_REACHED)
        self.assertFalse(hasattr(state, "realtime_verified"))

    def test_the_counting_basis_is_recorded_as_unknown(self):
        self.assertIs(IBKR_SNAPSHOT_POLICY_561227.counting_basis,
                      RequestCountingBasis.UNKNOWN)
        self.assertIs(RequestCountingBasis.conservative(),
                      RequestCountingBasis.PER_INSTRUMENT)


class TestExposureProjection(unittest.TestCase):
    """Section 26 -- computed from the universe, never copied from a note."""

    def setUp(self):
        self.governor = MarketDataBillingGovernor()

    def test_a_thirty_minute_test_on_the_real_distribution(self):
        projection = self.governor.project_exposure(
            {"NASDAQ": 13, "NYSE": 17, "ARCA": 1}, minutes=30)
        self.assertEqual(projection.cycles, 30)
        self.assertAlmostEqual(projection.capped_cost_usd, 3.30, places=2)
        self.assertAlmostEqual(projection.uncapped_cost_per_instrument_usd,
                               9.30, places=2)
        self.assertAlmostEqual(projection.uncapped_cost_per_call_usd,
                               0.90, places=2)

    def test_the_worst_case_takes_the_dearer_basis(self):
        projection = self.governor.project_exposure(
            {"NASDAQ": 13, "NYSE": 17, "ARCA": 1}, minutes=30)
        self.assertEqual(projection.worst_case_uncapped_usd,
                         projection.uncapped_cost_per_instrument_usd)

    def test_minutes_until_cap_follow_the_instrument_counts(self):
        projection = self.governor.project_exposure(
            {"NASDAQ": 13, "NYSE": 17, "ARCA": 1}, minutes=30)
        # 150/17 cycles at 60s, 150/13, 150/1.
        self.assertAlmostEqual(projection.minutes_until_cap["NYSE"], 8.82, places=1)
        self.assertAlmostEqual(projection.minutes_until_cap["NASDAQ"], 11.54,
                               places=1)
        self.assertAlmostEqual(projection.minutes_until_cap["ARCA"], 150.0,
                               places=1)

    def test_a_full_session_is_bounded_by_the_cap_but_not_by_the_uncapped_case(self):
        projection = self.governor.project_exposure(
            {"NASDAQ": 13, "NYSE": 17, "ARCA": 1}, minutes=390)
        self.assertAlmostEqual(projection.capped_cost_usd, 4.50, places=2)
        self.assertGreater(projection.worst_case_uncapped_usd, 100.0)

    def test_an_unpriceable_venue_makes_the_projection_none(self):
        projection = self.governor.project_exposure({"IEX": 5}, minutes=30)
        self.assertIsNone(projection.capped_cost_usd)
        self.assertEqual(projection.unpriceable_exchanges, ("IEX",))


class TestRequestLedger(unittest.TestCase):
    """Sections 23, 24, 35 -- the local meter and its limits."""

    def setUp(self):
        self.governor = MarketDataBillingGovernor()
        self.ledger = RequestLedger(self.governor)
        self.exchanges = {"a": "NYSE", "b": "NASDAQ", "c": "ARCA"}

    def test_recording_bills_each_instrument_under_the_conservative_basis(self):
        self.ledger.record(["a", "b", "c"], self.exchanges, NOON, OCTOBER)
        self.assertEqual(self.ledger.usage.count_for("NYSE", OCTOBER), 1)
        self.assertEqual(self.ledger.billed_units(), 3)

    def test_the_per_call_basis_bills_once_per_venue_touched(self):
        ledger = RequestLedger(
            self.governor, counting_basis=RequestCountingBasis.PER_HTTP_CALL)
        ledger.record(["a", "b", "c"], self.exchanges, NOON, OCTOBER)
        for name in ("NYSE", "NASDAQ", "ARCA"):
            self.assertEqual(ledger.usage.count_for(name, OCTOBER), 1)

    def test_an_unattributable_instrument_is_refused(self):
        with self.assertRaises(RequestRefused) as caught:
            self.ledger.authorize(["z"], {"z": UNKNOWN_EXCHANGE}, NOON, OCTOBER)
        self.assertIn("cannot be attributed", str(caught.exception))

    def test_the_minimum_refresh_interval_refuses_a_rapid_repeat(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        with self.assertRaises(RequestRefused) as caught:
            self.ledger.authorize(["a"], self.exchanges,
                                  NOON + timedelta(seconds=5), OCTOBER)
        self.assertIn("minimum refresh interval", str(caught.exception))

    def test_the_refresh_interval_allows_the_next_cycle(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        verdict = self.ledger.authorize(["a"], self.exchanges,
                                        NOON + timedelta(seconds=60), OCTOBER)
        self.assertTrue(verdict.permits_spending)

    def test_a_request_storm_hits_the_local_ceiling(self):
        ledger = RequestLedger(self.governor, max_requests_per_minute=5,
                               min_refresh_seconds=0)
        ledger.record(["a"] * 5, {"a": "NYSE"}, NOON, OCTOBER)
        with self.assertRaises(RequestRefused) as caught:
            ledger.authorize(["a"] * 3, {"a": "NYSE"}, NOON, OCTOBER)
        self.assertIn("request ceiling", str(caught.exception))

    def test_the_retry_budget_is_finite(self):
        for _ in range(self.ledger.retry_budget):
            self.ledger.note_retry("snapshot")
        with self.assertRaises(RequestRefused):
            self.ledger.note_retry("snapshot")

    def test_the_retry_budget_resets_on_success(self):
        self.ledger.note_retry("snapshot")
        self.ledger.reset_retries("snapshot")
        for _ in range(self.ledger.retry_budget):
            self.ledger.note_retry("snapshot")   # no raise

    def test_run_costs_are_tracked_per_validation_run(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER,
                           validation_run_id="run-1")
        self.ledger.record(["b"], self.exchanges,
                           NOON + timedelta(seconds=61), OCTOBER,
                           validation_run_id="run-2")
        self.assertEqual(self.ledger.run_cost_usd("run-1"), 0.01)
        self.assertEqual(self.ledger.request_count("run-2"), 1)

    def test_the_uncapped_run_cost_is_reported_beside_the_billed_one(self):
        for index in range(160):
            self.ledger.record(["a"], self.exchanges,
                               NOON + timedelta(seconds=61 * index), OCTOBER,
                               validation_run_id="run")
        self.assertAlmostEqual(self.ledger.run_cost_usd("run"), 1.50, places=2)
        self.assertAlmostEqual(self.ledger.uncapped_run_cost_usd("run"),
                               1.60, places=2)


class TestLedgerPersistence(unittest.TestCase):
    """Section 47 -- additive, idempotent, month-preserving."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.governor = MarketDataBillingGovernor()
        self.ledger = RequestLedger(self.governor)
        self.exchanges = {"a": "NYSE", "b": "NASDAQ"}

    def tearDown(self):
        self.conn.close()

    def test_a_flush_writes_requests_and_a_rollup(self):
        self.ledger.record(["a", "b"], self.exchanges, NOON, OCTOBER)
        written = self.ledger.flush(self.conn, NOON)
        self.assertEqual(written["requests_written"], 2)
        self.assertEqual(written["usage_rows"], 2)

    def test_flushing_twice_writes_nothing_new(self):
        self.ledger.record(["a", "b"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        again = self.ledger.flush(self.conn, NOON)
        self.assertEqual(again["requests_written"], 0)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM adaptive_market_data_requests").fetchone()[0]
        self.assertEqual(count, 2)

    def test_a_replay_rebuilds_the_tally_from_the_rows(self):
        self.ledger.record(["a", "b"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        replayed = RequestLedger.replay(self.conn, self.governor, OCTOBER)
        self.assertEqual(replayed.usage.count_for("NYSE", OCTOBER), 1)
        self.assertEqual(replayed.usage.count_for("NASDAQ", OCTOBER), 1)

    def test_a_replay_of_a_different_month_is_empty(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        replayed = RequestLedger.replay(self.conn, self.governor, SEPTEMBER)
        self.assertEqual(replayed.usage.count_for("NYSE", SEPTEMBER), 0)

    def test_no_credential_column_exists(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        for table in ("adaptive_market_data_requests",
                      "adaptive_market_data_exchange_usage",
                      "adaptive_market_data_entitlements",
                      "adaptive_market_data_state_transitions"):
            columns = [row[1].lower() for row in
                       self.conn.execute(f"PRAGMA table_info({table})")]
            for forbidden in ("password", "token", "credential", "secret",
                              "account_id", "username", "cookie"):
                self.assertNotIn(forbidden, columns,
                                 f"{table} must not store {forbidden}")

    def test_reconciliation_records_the_portal_beside_the_local_estimate(self):
        """Section 48.18 -- a disagreement is the valuable observation."""
        for index in range(150):
            self.ledger.record(["a"], self.exchanges,
                               NOON + timedelta(seconds=61 * index), OCTOBER)
        self.ledger.flush(self.conn, NOON)
        result = reconcile_portal_counter(
            self.conn, provider="IBKR", period=OCTOBER, exchange="NYSE",
            portal_count=150, portal_cost_usd=1.50, at=NOON)
        self.assertEqual(result["local_request_count"], 150)
        self.assertEqual(result["portal_reported_count"], 150)
        self.assertTrue(result["counts_agree"])

    def test_a_disagreement_is_reported_not_smoothed_over(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        result = reconcile_portal_counter(
            self.conn, provider="IBKR", period=OCTOBER, exchange="NYSE",
            portal_count=31, portal_cost_usd=0.31, at=NOON,
            note="portal counted per instrument")
        self.assertFalse(result["counts_agree"])
        self.assertEqual(result["local_request_count"], 1)
        # The local estimate is NOT overwritten by the portal figure.
        row = self.conn.execute(
            "SELECT request_count FROM adaptive_market_data_exchange_usage "
            "WHERE exchange='NYSE'").fetchone()
        self.assertEqual(row[0], 1)

    def test_an_unreconciled_row_reads_as_not_compared_not_agreed(self):
        self.ledger.record(["a"], self.exchanges, NOON, OCTOBER)
        self.ledger.flush(self.conn, NOON)
        row = self.conn.execute(
            "SELECT portal_reported_count, reconciled_at FROM "
            "adaptive_market_data_exchange_usage WHERE exchange='NYSE'"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])


if __name__ == "__main__":
    unittest.main()
