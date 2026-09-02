"""
tests/paper/test_paper_models.py
-------------------------------------
The Phase 13 domain types (spec 8, 9, 17, 20, 61).

These are the invariants everything downstream assumes. If a
`PaperAccount` could be constructed with `is_paper=False`, or a fill
could carry a venue other than PAPER, no amount of care in the session
runner would keep the phase inside its boundary - so the refusals are
tested here, at the type, rather than at every call site.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.paper_models import (
    DEFAULT_FRESHNESS_POLICIES, AlertSeverity, ComponentHealth, DataFreshness,
    ExecutionVenue, FreshnessPolicy, HealthState, MarketDataStatus, OrderSide,
    PaperAccount, PaperAccountStatus, PaperFill, PaperOrder, PaperOrderState,
    PaperOrderType, PaperSession, PaperSessionConfig, PaperSessionStatus,
    PaperSnapshot, SystemHealth, TimeInForce, finite_or_none, safe_ratio,
)

AT = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


class TestNumericGuards(unittest.TestCase):

    def test_infinities_and_nan_become_none(self):
        self.assertIsNone(finite_or_none(float("inf")))
        self.assertIsNone(finite_or_none(float("-inf")))
        self.assertIsNone(finite_or_none(float("nan")))
        self.assertEqual(finite_or_none(1.5), 1.5)

    def test_a_zero_denominator_gives_none_not_an_exception(self):
        self.assertIsNone(safe_ratio(1.0, 0.0))
        self.assertIsNone(safe_ratio(1.0, None))
        self.assertAlmostEqual(safe_ratio(1.0, 4.0), 0.25)


class TestTimezoneDiscipline(unittest.TestCase):
    """
    Every timestamp in this phase is UTC-aware. A naive datetime that
    slipped through would silently be compared against aware ones and
    raise somewhere far from where it entered.
    """

    def test_a_naive_timestamp_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            PaperAccount(account_id="a", name="n",
                         created_at=datetime(2026, 8, 27, 20, 0))

    def test_a_non_utc_offset_is_refused(self):
        with self.assertRaises(ValueError):
            PaperAccount(account_id="a", name="n",
                         created_at=datetime(2026, 8, 27, 20, 0,
                                             tzinfo=timezone(timedelta(hours=3))))

    def test_utc_is_accepted(self):
        account = PaperAccount(account_id="a", name="n", created_at=AT)
        self.assertEqual(account.created_at, AT)


class TestPaperAccount(unittest.TestCase):

    def test_an_account_cannot_declare_itself_not_paper(self):
        """
        The boundary of the whole phase, expressed as a type invariant
        rather than as a convention someone could forget.
        """
        with self.assertRaises(ValueError) as caught:
            PaperAccount(account_id="a", name="n", is_paper=False)
        self.assertIn("no live execution path", str(caught.exception))

    def test_capital_must_be_positive(self):
        for capital in (0.0, -1.0):
            with self.assertRaises(ValueError):
                PaperAccount(account_id="a", name="n", initial_capital=capital)

    def test_reduce_only_permits_reductions_but_not_new_exposure(self):
        account = PaperAccount(account_id="a", name="n",
                               status=PaperAccountStatus.REDUCE_ONLY)
        self.assertFalse(account.allows_new_exposure)
        self.assertTrue(account.allows_reductions)

    def test_an_emergency_stopped_account_permits_neither(self):
        account = PaperAccount(account_id="a", name="n",
                               status=PaperAccountStatus.EMERGENCY_STOP)
        self.assertFalse(account.allows_new_exposure)
        self.assertFalse(account.allows_reductions)

    def test_emergency_stop_is_distinct_from_a_routine_pause(self):
        """
        Both halt trading, but only one records that something went
        wrong. Collapsing them would lose that at the moment it matters.
        """
        self.assertNotEqual(PaperAccountStatus.PAUSED,
                            PaperAccountStatus.EMERGENCY_STOP)
        for status in (PaperAccountStatus.PAUSED,
                       PaperAccountStatus.EMERGENCY_STOP,
                       PaperAccountStatus.CLOSED):
            account = PaperAccount(account_id="a", name="n", status=status)
            self.assertFalse(account.allows_new_exposure, status.value)

    def test_shorting_follows_the_account_type(self):
        self.assertFalse(PaperAccount(account_id="a", name="n").allows_shorting)
        self.assertTrue(PaperAccount(account_id="a", name="n",
                                     account_type="long_short").allows_shorting)


class TestOrderStates(unittest.TestCase):

    def test_terminal_and_working_states_do_not_overlap(self):
        for state in PaperOrderState:
            self.assertFalse(state.is_terminal and state.is_working, state.value)

    def test_a_partially_filled_order_is_still_working(self):
        self.assertTrue(PaperOrderState.PARTIALLY_FILLED.is_working)
        self.assertFalse(PaperOrderState.PARTIALLY_FILLED.is_terminal)

    def test_cancel_requested_is_neither_yet(self):
        """
        A cancel that has been asked for but not confirmed must not be
        counted as cancelled - the order can still fill first.
        """
        self.assertFalse(PaperOrderState.CANCEL_REQUESTED.is_terminal)
        self.assertFalse(PaperOrderState.CANCEL_REQUESTED.is_working)

    def test_every_terminal_state_is_reachable_and_named(self):
        terminal = {s for s in PaperOrderState if s.is_terminal}
        self.assertEqual(terminal, {PaperOrderState.FILLED,
                                    PaperOrderState.REJECTED,
                                    PaperOrderState.CANCELLED,
                                    PaperOrderState.EXPIRED})


class TestPaperOrder(unittest.TestCase):

    def order(self, **overrides):
        base = dict(order_id="o-1", session_id="s-1", account_id="a-1",
                    instrument_id="i-1", side=OrderSide.BUY, quantity=10.0,
                    created_at=AT)
        base.update(overrides)
        return PaperOrder(**base)

    def test_quantity_carries_no_sign(self):
        """Direction lives in `side`; a signed quantity would let the two disagree."""
        with self.assertRaises(ValueError) as caught:
            self.order(quantity=-10.0)
        self.assertIn("direction is", str(caught.exception))

    def test_zero_quantity_is_refused(self):
        with self.assertRaises(ValueError):
            self.order(quantity=0.0)

    def test_a_limit_order_without_a_limit_price_is_refused(self):
        with self.assertRaises(ValueError):
            self.order(order_type=PaperOrderType.LIMIT)

    def test_a_stop_order_without_a_stop_price_is_refused(self):
        with self.assertRaises(ValueError):
            self.order(order_type=PaperOrderType.STOP)

    def test_signed_filled_follows_the_side(self):
        buy = self.order(filled_quantity=4.0)
        sell = self.order(side=OrderSide.SELL, filled_quantity=4.0)
        self.assertAlmostEqual(buy.signed_filled, 4.0)
        self.assertAlmostEqual(sell.signed_filled, -4.0)

    def test_remaining_never_goes_negative(self):
        over = self.order(quantity=10.0, filled_quantity=12.0)
        self.assertGreaterEqual(over.remaining, 0.0)

    def test_expiry_is_evaluated_against_a_moment_not_wall_clock(self):
        order = self.order(expires_at=AT + timedelta(days=1))
        self.assertFalse(order.is_expired_at(AT))
        self.assertTrue(order.is_expired_at(AT + timedelta(days=2)))

    def test_an_order_without_an_expiry_never_expires(self):
        self.assertFalse(self.order().is_expired_at(AT + timedelta(days=3650)))


class TestPaperFill(unittest.TestCase):

    def fill(self, **overrides):
        base = dict(fill_id="f-1", order_id="o-1", session_id="s-1",
                    account_id="a-1", instrument_id="i-1", side=OrderSide.BUY,
                    quantity=10.0, price=100.0, reference_price=100.0,
                    filled_at=AT)
        base.update(overrides)
        return PaperFill(**base)

    def test_no_venue_other_than_paper_can_be_recorded(self):
        """
        `ExecutionVenue` has a single member today, so this test guards
        the future: adding a broker venue must fail here loudly rather
        than quietly become constructible.
        """
        self.assertEqual([v for v in ExecutionVenue], [ExecutionVenue.PAPER])
        fill = self.fill()
        self.assertEqual(fill.venue, ExecutionVenue.PAPER)

    def test_total_cost_includes_commission_and_slippage(self):
        fill = self.fill(commission=5.0, slippage_cost=3.0)
        self.assertAlmostEqual(fill.notional, 1000.0)
        self.assertAlmostEqual(fill.total_cost, 8.0)

    def test_signed_quantity_follows_the_side(self):
        self.assertAlmostEqual(self.fill().signed_quantity, 10.0)
        self.assertAlmostEqual(
            self.fill(side=OrderSide.SELL).signed_quantity, -10.0)

    def test_an_ambiguous_intrabar_fill_says_so_rather_than_hiding_it(self):
        fill = self.fill(intrabar_ambiguous=True)
        self.assertTrue(fill.intrabar_ambiguous)


class TestFreshnessPolicy(unittest.TestCase):

    def test_unknown_age_is_unavailable_not_stale(self):
        """
        "I do not know how old this is" and "this is old" are different
        facts, and conflating them would let a missing feed masquerade
        as a merely slow one.
        """
        self.assertEqual(FreshnessPolicy().classify(None),
                         DataFreshness.UNAVAILABLE)

    def test_data_stamped_in_the_future_is_invalid_not_very_fresh(self):
        self.assertEqual(FreshnessPolicy().classify(-1.0), DataFreshness.INVALID)

    def test_the_bands_are_inclusive_at_their_upper_edge(self):
        policy = FreshnessPolicy(fresh_seconds=100, aging_seconds=200,
                                 stale_seconds=300)
        self.assertEqual(policy.classify(100), DataFreshness.FRESH)
        self.assertEqual(policy.classify(100.5), DataFreshness.AGING)
        self.assertEqual(policy.classify(200), DataFreshness.AGING)
        self.assertEqual(policy.classify(300), DataFreshness.STALE)
        self.assertEqual(policy.classify(300.5), DataFreshness.INVALID)

    def test_only_fresh_and_aging_may_back_an_order(self):
        tradeable = {f for f in DataFreshness if f.is_tradeable}
        self.assertEqual(tradeable, {DataFreshness.FRESH, DataFreshness.AGING})

    def test_asset_classes_do_not_share_one_threshold(self):
        """
        A four-hour-old crypto quote is stale; a four-hour-old equity
        bar after the close is normal. One number could not be right for
        both.
        """
        four_hours = 4 * 3600.0
        self.assertEqual(
            DEFAULT_FRESHNESS_POLICIES["crypto"].classify(four_hours),
            DataFreshness.AGING)
        self.assertEqual(
            DEFAULT_FRESHNESS_POLICIES["stock"].classify(four_hours),
            DataFreshness.FRESH)
        self.assertNotEqual(DEFAULT_FRESHNESS_POLICIES["crypto"].fresh_seconds,
                            DEFAULT_FRESHNESS_POLICIES["bvb"].fresh_seconds)

    def test_a_week_old_crypto_quote_is_invalid(self):
        week = 7 * 86_400.0
        self.assertEqual(DEFAULT_FRESHNESS_POLICIES["crypto"].classify(week),
                         DataFreshness.INVALID)


class TestMarketDataStatus(unittest.TestCase):

    def test_age_is_measured_from_observation_to_evaluation(self):
        status = MarketDataStatus(instrument_id="i-1", observed_at=AT,
                                  evaluated_at=AT + timedelta(hours=2))
        self.assertAlmostEqual(status.age_seconds, 7200.0)

    def test_age_is_unknown_when_either_end_is_missing(self):
        self.assertIsNone(MarketDataStatus(instrument_id="i-1").age_seconds)
        self.assertIsNone(MarketDataStatus(instrument_id="i-1",
                                           observed_at=AT).age_seconds)

    def test_cached_data_is_still_labelled_cached(self):
        """
        Spec 8: a paper session on cached daily bars must not present
        them as live. The default is honest.
        """
        self.assertTrue(MarketDataStatus(instrument_id="i-1").is_cached)


class TestHealth(unittest.TestCase):

    def test_only_healthy_and_degraded_allow_new_orders(self):
        allowed = {s for s in HealthState if s.allows_new_orders}
        self.assertEqual(allowed, {HealthState.HEALTHY, HealthState.DEGRADED})

    def test_overall_health_is_the_worst_component_not_the_average(self):
        """
        Averaging would let six healthy components hide one failed one -
        precisely the component that matters.
        """
        health = SystemHealth(at=AT)
        for name in ("data", "signals", "risk", "execution", "ledger"):
            health.record(name, HealthState.HEALTHY, AT)
        health.record("persistence", HealthState.FAILED, AT)
        self.assertEqual(health.overall, HealthState.FAILED)
        self.assertFalse(health.allows_new_orders)

    def test_a_system_with_no_components_is_not_asserted_healthy(self):
        health = SystemHealth(at=AT)
        self.assertFalse(health.allows_new_orders)

    def test_safe_mode_blocks_orders_whatever_the_components_say(self):
        health = SystemHealth(at=AT, safe_mode=True,
                              safe_mode_reason="operator halt")
        health.record("data", HealthState.HEALTHY, AT)
        self.assertFalse(health.allows_new_orders)

    def test_component_age_needs_a_heartbeat(self):
        self.assertIsNone(ComponentHealth(component="data").age_seconds(AT))
        component = ComponentHealth(component="data", last_heartbeat_at=AT)
        self.assertAlmostEqual(
            component.age_seconds(AT + timedelta(minutes=5)), 300.0)


class TestSnapshot(unittest.TestCase):

    def snapshot(self, **overrides):
        base = dict(snapshot_id="snap-1", session_id="s-1", account_id="a-1",
                    at=AT, equity=100.0, cash=50.0, positions_value=50.0)
        base.update(overrides)
        return PaperSnapshot(**base)

    def test_total_pnl_is_none_when_unrealized_is_unknown(self):
        """
        Positions that could not be priced make total P&L unknowable.
        Reporting realized P&L alone as the total would understate a
        loss the book is actually carrying.
        """
        snapshot = self.snapshot(realized_pnl=5.0, unrealized_pnl=None)
        self.assertIsNone(snapshot.total_pnl)

    def test_total_pnl_adds_up_when_everything_is_priced(self):
        snapshot = self.snapshot(realized_pnl=5.0, unrealized_pnl=3.0)
        self.assertAlmostEqual(snapshot.total_pnl, 8.0)

    def test_a_snapshot_with_unpriced_positions_is_not_complete(self):
        snapshot = self.snapshot(unpriced_positions=1)
        self.assertFalse(snapshot.is_complete)


class TestSessionConfigFingerprint(unittest.TestCase):

    def config(self, **overrides):
        base = dict(universe=["i-a", "i-b"])
        base.update(overrides)
        return PaperSessionConfig(**base)

    def test_the_same_configuration_fingerprints_identically(self):
        self.assertEqual(self.config().fingerprint(), self.config().fingerprint())

    def test_universe_order_does_not_change_the_fingerprint(self):
        self.assertEqual(self.config(universe=["i-a", "i-b"]).fingerprint(),
                         self.config(universe=["i-b", "i-a"]).fingerprint())

    def test_every_behavioural_field_changes_the_fingerprint(self):
        baseline = self.config().fingerprint()
        for field_name, value in (
            ("universe", ["i-a"]),
            ("constraint_set_version", "v2"),
            ("sizing_target_weight", 0.10),
            ("cost_model_version", "cost-v2"),
            ("slippage_model_version", "slip-v2"),
            ("execution_model_version", "paper-exec-v2"),
            ("commission_bps", 4.0),
            ("slippage_bps", 10.0),
            ("signal_to_order_seconds", 120.0),
            ("max_participation", 0.20),
            ("default_order_type", PaperOrderType.LIMIT),
            ("default_time_in_force", TimeInForce.GTC),
            ("max_orders_per_tick", 5),
            ("max_orders_per_day", 10),
            ("daily_loss_limit_pct", 0.10),
            ("max_drawdown_pct", 0.30),
            ("require_fresh_data", False),
            ("tick_interval_seconds", 3600.0),
        ):
            self.assertNotEqual(baseline,
                                self.config(**{field_name: value}).fingerprint(),
                                field_name)

    def test_a_cosmetic_field_does_not_change_it(self):
        """
        The fingerprint answers "would this configuration behave
        differently", so a renamed strategy label must not invalidate a
        comparison between two otherwise identical runs.
        """
        self.assertEqual(self.config().fingerprint(),
                         self.config(strategy_id=None).fingerprint())


class TestSessionLifecycle(unittest.TestCase):

    def session(self, status):
        return PaperSession(session_id="s-1", account_id="a-1", name="n",
                            config=PaperSessionConfig(universe=["i-a"]),
                            status=status)

    def test_a_created_session_accepts_ticks_before_it_has_started(self):
        self.assertTrue(self.session(PaperSessionStatus.CREATED).accepts_ticks)
        self.assertFalse(self.session(PaperSessionStatus.CREATED).is_running)

    def test_terminal_and_paused_sessions_do_not_accept_ticks(self):
        for status in (PaperSessionStatus.PAUSED, PaperSessionStatus.COMPLETED,
                       PaperSessionStatus.FAILED, PaperSessionStatus.CANCELLED):
            self.assertFalse(self.session(status).accepts_ticks, status.value)


if __name__ == "__main__":
    unittest.main()
