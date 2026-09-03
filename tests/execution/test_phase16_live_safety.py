"""
tests/execution/test_phase16_live_safety.py
------------------------------------------------
The tests spec §78 asks for by name, plus the §79 failure injections.

§78 REQUIRES PROOF THAT LIVE EXECUTION IS BLOCKED
-----------------------------------------------------
  by default, and when any of these holds:
      kill switch = ON
      risk = FAIL
      broker health = FAIL
      market data = STALE
      reconciliation = FAIL
      capital limit = exceeded

Each has a test below, and each blocks INDEPENDENTLY — asserted one
condition at a time, because a suite that only ever tested them
together would pass even if five of the six checks were dead code.

There is a stronger claim underneath all of them, and it is tested
first: there is no real-money execution path to block. The gates are
real and would stop a live order; no adapter accepts a real-money
environment, so nothing reaches them.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.broker_models import (
    AccountSnapshot, Broker, BrokerAccount, CanonicalOrderSide,
    ExecutionEnvironment, ExecutionOrderState, ExecutionRejectCode,
    MismatchKind,
    ReconciliationMismatch, ReconciliationRecord,
)
from src.execution.adapters.ibkr.config import (
    IBKRConfig, IBKRConfigurationError, paper_config,
)
from src.execution.adapters.ibkr.mock_transport import (
    MOCK_ACCOUNT, MockIBKRTransport,
)
from src.execution.governance import ExecutionGovernor, ExecutionLevel
from src.execution.limits import (
    CapitalLimits, DayState, LimitBreach, MarketContext, RiskGovernor,
    paper_limits,
)
from src.execution.monitoring import (
    AlertSeverity, Capability, CapabilityState, ExecutionMetrics,
    ExecutionMonitor, ORDER_CRITICAL, SystemHealth, compare_environments,
)
from src.execution.safety import ExecutionSafety
from src.execution.session import (
    SessionAction, SessionConfiguration, new_session, standard_preflight,
)

from tests.execution.ibkr.helpers import (
    AT, INSTRUMENT, build_ibkr, fill_through_gateway, ibkr_request, submit,
)

ALL_PREFLIGHT = {k: True for k in (
    "broker_connected", "account_available", "market_data_live",
    "reconciliation_clean", "risk_available", "capital_configured",
    "no_unknown_orders", "kill_switch_off")}


def account(equity: float = 1_000_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=MOCK_ACCOUNT, broker_id="ibkr", at=AT, cash=equity,
        equity=equity, buying_power=equity, available_funds=equity)


def market(**overrides) -> MarketContext:
    base = dict(quote_at=AT, account_at=AT, position_at=AT, risk_at=AT,
                broker_time=AT, reference_price=100.0, quote_is_live=True)
    base.update(overrides)
    return MarketContext(**base)


def day() -> DayState:
    return DayState(day="2026-08-27", realized_pnl=0.0, unrealized_pnl=0.0,
                    starting_equity=1_000_000.0, current_equity=1_000_000.0,
                    peak_equity=1_000_000.0)


def healthy(at: datetime = AT) -> SystemHealth:
    health = SystemHealth(at=at)
    for capability in Capability:
        health.record(capability, CapabilityState.HEALTHY)
    return health


# ============================================================
# §78 — the structural claim
# ============================================================

class TestNoRealMoneyPathExists(unittest.TestCase):
    """
    The gates below are the second line. This is the first.

    Spec §101 Q11 asks whether live can activate accidentally. It
    cannot, because activating it is not a configuration change — it
    would require writing an adapter that does not exist.
    """

    def test_the_ibkr_config_refuses_a_live_environment(self):
        with self.assertRaises(IBKRConfigurationError):
            IBKRConfig(environment=ExecutionEnvironment.LIVE,
                       account_id=MOCK_ACCOUNT)

    def test_the_environment_cannot_smuggle_live_in(self):
        """
        `from_environment` reads IBKR_ENVIRONMENT, and the refusal
        lives in `__post_init__` rather than in the reader — so it
        applies whatever route the value took.
        """
        os.environ["IBKR_ENVIRONMENT"] = "live"
        try:
            with self.assertRaises(IBKRConfigurationError):
                IBKRConfig.from_environment(account_id=MOCK_ACCOUNT)
        finally:
            os.environ.pop("IBKR_ENVIRONMENT", None)

    def test_the_ibkr_gateway_refuses_to_construct_on_live(self):
        """
        Not a flag that could be flipped. The environment is validated
        in the config's constructor, so a live gateway cannot be
        instantiated at all.
        """
        with self.assertRaises(IBKRConfigurationError):
            paper_config(account_id=MOCK_ACCOUNT,
                         environment=ExecutionEnvironment.LIVE)

    def test_a_live_broker_account_cannot_be_built(self):
        for factory in (
            lambda: Broker(broker_id="x", name="x",
                           environment=ExecutionEnvironment.LIVE,
                           adapter="none"),
            lambda: BrokerAccount(account_id="a", broker_id="x", name="a",
                                  environment=ExecutionEnvironment.LIVE),
        ):
            with self.assertRaises(ValueError):
                factory()

    def test_safety_never_allows_real_orders(self):
        safety = ExecutionSafety()
        self.assertFalse(safety.allow_real_orders)

    def test_allow_real_orders_has_no_setter(self):
        """A read-only property, so no code path can turn it on."""
        safety = ExecutionSafety()
        with self.assertRaises(AttributeError):
            safety.allow_real_orders = True

    def test_an_environment_variable_cannot_request_real_orders(self):
        self.assertFalse(ExecutionSafety.real_orders_requested_by_environment())

    def test_asserting_not_real_money_raises_on_live(self):
        safety = ExecutionSafety()
        with self.assertRaises(Exception):
            safety.assert_not_real_money(ExecutionEnvironment.LIVE)

    def test_no_implemented_execution_level_is_real_money(self):
        governor = ExecutionGovernor()
        for level in ExecutionLevel:
            if level.is_implemented:
                self.assertFalse(level.is_real_money, level.label)

    def test_the_governor_reports_real_money_unreachable(self):
        self.assertFalse(ExecutionGovernor().state(AT)["real_money_reachable"])


# ============================================================
# §78 — each blocking condition, one at a time
# ============================================================

class LiveGateCase(unittest.TestCase):
    """Shared fixture: everything healthy, one thing broken per test."""

    def setUp(self):
        self.governor = paper_limits()
        self.safety = ExecutionSafety()

    def check(self, **overrides):
        base = dict(side=CanonicalOrderSide.BUY, quantity=10.0, price=100.0,
                    instrument_id=INSTRUMENT, account=account(), positions=[],
                    day=day(), market=market(), broker_healthy=True,
                    reconciliation_clean=True)
        base.update(overrides)
        return self.governor.check(AT, **base)

    def assertBlocked(self, decision, breach=None):
        self.assertFalse(decision.permitted, decision.explain())
        if breach is not None:
            self.assertIn(breach, [b for b, _ in decision.breaches],
                          decision.explain())


class TestBlockedByDefault(LiveGateCase):

    def test_a_fresh_risk_governor_permits_a_clean_paper_order(self):
        """The control: without this, every test below proves nothing."""
        self.assertTrue(self.check().permitted)

    def test_an_unconfigured_governor_blocks_real_money_sizing(self):
        """Spec §26. Default = no real-money capital authorised."""
        unconfigured = RiskGovernor(capital=CapitalLimits())
        self.governor = unconfigured
        self.assertBlocked(self.check(require_real_money_config=True),
                           LimitBreach.MAX_LIVE_CAPITAL)


class TestBlockedByKillSwitch(LiveGateCase):

    def test_the_kill_switch_blocks_submission(self):
        self.safety.activate_kill_switch("drill", AT, actor="operator")
        verdict = self.safety.check(ExecutionEnvironment.PAPER)
        self.assertFalse(verdict.permitted)

    def test_the_kill_switch_blocks_an_order_through_the_orchestrator(self):
        stack = build_ibkr()
        stack["safety"].activate_kill_switch("drill", AT, actor="operator")
        submit(stack)
        self.assertEqual(stack["transport"].place_calls, 0,
                         "the venue was reached with the kill switch on")

    def test_releasing_the_kill_switch_requires_a_reason(self):
        self.safety.activate_kill_switch("drill", AT, actor="operator")
        with self.assertRaises(ValueError):
            self.safety.release_kill_switch("", AT, actor="operator")

    def test_a_session_refuses_to_start_with_the_kill_switch_on(self):
        session = new_session(SessionConfiguration(
            account_id=MOCK_ACCOUNT, strategies=("s",)), "alice", AT)
        checks = standard_preflight(**dict(ALL_PREFLIGHT, kill_switch_off=False))
        self.assertFalse(session.run_preflight(checks, "alice", AT))


class TestBlockedByRiskFailure(LiveGateCase):

    def test_a_breached_daily_loss_blocks(self):
        losing = DayState(day="d", realized_pnl=-90_000.0, unrealized_pnl=0.0,
                          starting_equity=1_000_000.0,
                          current_equity=910_000.0, peak_equity=1_000_000.0)
        self.assertBlocked(self.check(day=losing),
                           LimitBreach.DAILY_TOTAL_LOSS)

    def test_a_breached_drawdown_blocks(self):
        drawn = DayState(day="d", realized_pnl=0.0, unrealized_pnl=-300_000.0,
                         starting_equity=1_000_000.0,
                         current_equity=700_000.0, peak_equity=1_000_000.0)
        self.assertBlocked(self.check(day=drawn))

    def test_a_missing_risk_verdict_blocks(self):
        """Unknown risk is not passing risk."""
        self.assertBlocked(self.check(market=market(risk_at=None)))

    def test_a_session_refuses_to_start_without_a_risk_engine(self):
        session = new_session(SessionConfiguration(
            account_id=MOCK_ACCOUNT, strategies=("s",)), "alice", AT)
        checks = standard_preflight(**dict(ALL_PREFLIGHT, risk_available=False))
        self.assertFalse(session.run_preflight(checks, "alice", AT))


class TestBlockedByBrokerHealth(LiveGateCase):

    def test_an_unhealthy_broker_blocks(self):
        self.assertBlocked(self.check(broker_healthy=False),
                           LimitBreach.BROKER_HEALTH)

    def test_an_unmeasured_broker_blocks(self):
        self.assertBlocked(self.check(broker_healthy=None),
                           LimitBreach.BROKER_HEALTH)

    def test_every_order_critical_capability_blocks_on_its_own(self):
        for capability in ORDER_CRITICAL:
            health = healthy()
            health.record(capability, CapabilityState.UNAVAILABLE, "injected")
            self.assertFalse(health.permits_new_orders, capability.value)
            self.assertIn(capability, health.failing)

    def test_degraded_is_not_good_enough_to_submit(self):
        health = healthy()
        health.record(Capability.ORDERS, CapabilityState.DEGRADED, "slow")
        self.assertFalse(health.permits_new_orders)

    def test_an_unknown_capability_blocks(self):
        health = SystemHealth(at=AT)
        self.assertFalse(health.permits_new_orders)
        self.assertIs(health.overall, CapabilityState.UNKNOWN)

    def test_overall_health_is_the_worst_reading_not_an_average(self):
        health = healthy()
        health.record(Capability.ACCOUNT, CapabilityState.UNAVAILABLE, "down")
        self.assertIs(health.overall, CapabilityState.UNAVAILABLE)

    def test_a_read_side_failure_is_reported_without_blocking_orders(self):
        """
        Positions gate through the limit governor, so a stale position
        feed does not become an order-blocking condition twice.
        """
        health = healthy()
        health.record(Capability.POSITIONS, CapabilityState.STALE, "lagging")
        self.assertTrue(health.permits_new_orders)
        self.assertIn(Capability.POSITIONS, health.failing)

    def test_a_failing_capability_raises_an_alert(self):
        monitor = ExecutionMonitor(session_id="s")
        health = healthy()
        health.record(Capability.CONNECTION, CapabilityState.UNAVAILABLE,
                      "socket closed")
        alerts = monitor.raise_health_alerts(health, at=AT)
        self.assertTrue(alerts)
        self.assertTrue(any(a.severity.demands_attention for a in alerts))


class TestBlockedByStaleMarketData(LiveGateCase):

    def test_a_stale_quote_blocks(self):
        self.assertBlocked(
            self.check(market=market(quote_at=AT - timedelta(minutes=10))),
            LimitBreach.STALE_QUOTE)

    def test_a_stale_account_blocks(self):
        self.assertBlocked(
            self.check(market=market(account_at=AT - timedelta(hours=1))),
            LimitBreach.STALE_ACCOUNT)

    def test_a_stale_position_view_blocks(self):
        self.assertBlocked(
            self.check(market=market(position_at=AT - timedelta(hours=1))),
            LimitBreach.STALE_POSITION)

    def test_a_delayed_quote_blocks(self):
        self.assertBlocked(self.check(market=market(quote_is_live=False)))

    def test_the_gateway_reports_market_data_as_untradeable(self):
        """
        The quote object still comes back — with no prices and an
        availability that says why. Returning None would lose the
        distinction between "no subscription" and "never asked".
        """
        stack = build_ibkr()
        stack["transport"].market_data_available = False
        quote = stack["gateway"].quote(INSTRUMENT, AT)
        self.assertIsNotNone(quote)
        self.assertFalse(quote.availability.is_tradeable)
        self.assertIsNone(quote.reference_price)


class TestBlockedByReconciliationFailure(LiveGateCase):

    def test_a_dirty_reconciliation_blocks(self):
        self.assertBlocked(self.check(reconciliation_clean=False),
                           LimitBreach.RECONCILIATION)

    def test_an_unrun_reconciliation_blocks(self):
        self.assertBlocked(self.check(reconciliation_clean=None),
                           LimitBreach.RECONCILIATION)

    def test_a_position_mismatch_is_a_blocking_severity(self):
        record = ReconciliationRecord(reconciliation_id="r", broker_id="ibkr",
                                      account_id=MOCK_ACCOUNT, at=AT)
        record.mismatches = [ReconciliationMismatch(
            kind=MismatchKind.POSITION_MISMATCH, detail="10 vs 12")]
        self.assertTrue(record.blocks_execution)
        self.assertBlocked(
            self.check(reconciliation_clean=not record.blocks_execution),
            LimitBreach.RECONCILIATION)

    def test_a_cosmetic_mismatch_does_not_block(self):
        record = ReconciliationRecord(reconciliation_id="r", broker_id="ibkr",
                                      account_id=MOCK_ACCOUNT, at=AT)
        record.mismatches = [ReconciliationMismatch(
            kind=MismatchKind.PRICE_MISMATCH, detail="0.001")]
        self.assertFalse(record.blocks_execution)


class TestBlockedByCapitalLimit(LiveGateCase):

    def test_an_oversized_order_blocks(self):
        self.assertBlocked(self.check(quantity=100_000.0),
                           LimitBreach.MAX_ORDER_NOTIONAL)

    def test_an_oversized_resulting_position_blocks(self):
        governor = RiskGovernor(capital=CapitalLimits(
            max_order_notional=1e9, max_position_notional=1_000.0))
        self.governor = governor
        self.assertBlocked(self.check(quantity=100.0, price=100.0),
                           LimitBreach.MAX_POSITION_NOTIONAL)

    def test_insufficient_buying_power_blocks(self):
        self.assertBlocked(
            self.check(account=account(50.0), quantity=10.0, price=100.0),
            LimitBreach.INSUFFICIENT_MARGIN)

    def test_a_daily_order_count_cap_blocks(self):
        self.governor = RiskGovernor(capital=CapitalLimits(max_daily_orders=5))
        busy = day()
        busy.orders_submitted = 5
        self.assertBlocked(self.check(day=busy), LimitBreach.MAX_DAILY_ORDERS)
        quiet = day()
        quiet.orders_submitted = 4
        self.assertTrue(self.check(day=quiet).permitted)


# ============================================================
# §79 — failure injection
# ============================================================

class TestFailureInjection(unittest.TestCase):
    """
    Every failure the spec names, injected at the venue and observed
    through the real adapter and orchestrator.
    """

    def setUp(self):
        self.stack = build_ibkr()
        self.transport = self.stack["transport"]

    def test_broker_disconnect_mid_session(self):
        outcome = submit(self.stack)
        self.assertIsNotNone(outcome.order)
        self.transport.connected = False
        self.assertFalse(self.stack["gateway"].health_check(AT).is_usable)

        # A lost connection is caught before an order is built, so
        # nothing is submitted and nothing reaches the venue. The auth
        # case below fails later and does produce a REJECTED order —
        # the two are different depths of the same refusal.
        calls = self.transport.place_calls
        blocked = submit(self.stack, intent_id="int-2")
        self.assertIsNone(blocked.order)
        self.assertEqual(self.transport.place_calls, calls)

    def test_authentication_loss_rejects_without_reaching_the_venue(self):
        """
        The refusal is recorded as a REJECTED order rather than
        discarded. An order that vanished would leave nothing to
        explain the missing trade with.
        """
        self.transport.authenticated = False
        before = self.transport.place_calls
        outcome = submit(self.stack, intent_id="int-auth")
        self.assertIs(outcome.order.state, ExecutionOrderState.REJECTED)
        self.assertIs(outcome.order.reject_code,
                      ExecutionRejectCode.BROKER_DISCONNECTED)
        self.assertIsNone(outcome.order.broker_order_id)
        self.assertEqual(self.transport.place_calls, before)

    def test_a_competing_session_is_surfaced_as_unhealthy(self):
        """
        IBKR allows one session per login. A second one silently
        displaces the first, and an order sent afterwards may never be
        acknowledged.
        """
        self.transport.competing = True
        self.assertFalse(self.stack["gateway"].health_check(AT).is_usable)

    def test_market_data_loss_leaves_no_price_to_trade_on(self):
        self.transport.market_data_available = False
        quote = self.stack["gateway"].quote(INSTRUMENT, AT)
        self.assertFalse(quote.availability.is_tradeable)
        self.assertIsNone(quote.reference_price)

    def test_a_timeout_after_submission_leaves_the_order_unknown(self):
        """
        The dangerous case: the request left, the answer did not. The
        order is UNKNOWN, not FAILED — claiming it failed would invite
        a duplicate.
        """
        self.transport.timeout_on_place = True
        outcome = submit(self.stack, intent_id="int-timeout")
        order = outcome.order
        self.assertIsNotNone(order)
        self.assertIs(order.state, ExecutionOrderState.UNKNOWN)

    def test_an_unknown_order_blocks_a_session_start(self):
        session = new_session(SessionConfiguration(
            account_id=MOCK_ACCOUNT, strategies=("s",)), "alice", AT)
        checks = standard_preflight(
            **dict(ALL_PREFLIGHT, no_unknown_orders=False))
        self.assertFalse(session.run_preflight(checks, "alice", AT))

    def test_a_duplicate_execution_is_applied_once(self):
        outcome = submit(self.stack, intent_id="int-dupe")
        order = outcome.order
        applied = fill_through_gateway(self.stack, order, 10.0, 100.0,
                                       execution_id="exec-same")
        again = fill_through_gateway(self.stack, order, 10.0, 100.0,
                                     execution_id="exec-same")
        self.assertEqual(applied, 1)
        self.assertEqual(again, 0, "the same execution filled twice")
        self.assertEqual(order.filled_quantity, 10.0)

    def test_a_rate_limit_is_distinguishable_from_a_venue_rejection(self):
        """
        Both end REJECTED, and they must not be confused: a pacing
        violation means the order was never sent and may be retried,
        while a venue rejection means it was seen and refused.
        """
        self.transport.rate_limited = True
        outcome = submit(self.stack, intent_id="int-rate")
        self.assertIs(outcome.order.reject_code,
                      ExecutionRejectCode.RATE_LIMITED)
        self.assertIsNone(outcome.order.broker_order_id)
        self.assertIn("was not sent", outcome.order.reject_detail)

    def test_a_venue_rejection_is_recorded_not_retried(self):
        self.transport.reject_on_place = True
        before = self.transport.place_calls
        submit(self.stack, intent_id="int-reject")
        self.assertLessEqual(self.transport.place_calls - before, 1,
                             "a rejected order was resubmitted")

    def test_a_restart_does_not_resubmit_a_live_order(self):
        """
        Spec §79. The idempotency key is derived from the intent, so
        the same intent after a restart resolves to the same order.
        """
        first = submit(self.stack, intent_id="int-restart")
        self.assertIsNotNone(first.order)
        calls = self.transport.place_calls
        second = submit(self.stack, intent_id="int-restart")
        self.assertEqual(self.transport.place_calls, calls,
                         "a duplicate intent reached the venue")
        if second.order is not None:
            self.assertEqual(second.order.order_id, first.order.order_id)


# ============================================================
# §80 — end to end, paper
# ============================================================

class TestEndToEndPaper(unittest.TestCase):
    """
    Signal to order to fill to position to reconciliation to journal,
    through the real stack against the mock venue.
    """

    def test_the_full_paper_path(self):
        from src.execution.outcomes import (
            ExecutionJournal, ExitReason, TradeOutcome, classify_errors,
            lineage_from_order, quality_from_order,
        )

        stack = build_ibkr()
        session = new_session(SessionConfiguration(
            account_id=MOCK_ACCOUNT, strategies=("strat-1",),
            model_version="m-1"), "alice", AT)
        self.assertTrue(session.run_preflight(
            standard_preflight(**ALL_PREFLIGHT), "alice", AT))
        session.apply(SessionAction.START, "alice", AT)
        permitted, why = session.may_submit(AT)
        self.assertTrue(permitted, why)

        # --- order -------------------------------------------------
        outcome = submit(stack, intent_id="e2e-1",
                         correlation_id="cor-e2e", signal_id="sig-1",
                         prediction_id="pred-1", model_version="m-1",
                         decision_id="dec-1", strategy_id="strat-1",
                         portfolio_id="pf-1")
        order = outcome.order
        self.assertIsNotNone(order, "the order never reached the venue")
        self.assertTrue(order.broker_order_id)

        # --- fill --------------------------------------------------
        applied = fill_through_gateway(stack, order, 10.0, 100.05,
                                       commission=1.0)
        self.assertEqual(applied, 1)
        self.assertEqual(order.filled_quantity, 10.0)

        # The totals and the state advance by different routes: fills
        # fold into the order, and the venue reports the status change
        # as an event. Draining is what a running session does each
        # tick, and the two must agree afterwards.
        stack["orchestrator"].drain_events("ibkr", AT)
        self.assertIs(order.state, ExecutionOrderState.FILLED)
        self.assertEqual(order.remaining, 0.0)

        # --- reconciliation ---------------------------------------
        stack["transport"].set_position("265598", 10.0, 100.05)
        record = stack["orchestrator"].reconcile(
            "ibkr", MOCK_ACCOUNT, AT, internal_positions={INSTRUMENT: 10.0})
        self.assertIsNotNone(record)
        self.assertFalse(record.blocks_execution,
                         [m.detail for m in record.mismatches])

        # --- lineage and quality ----------------------------------
        fills = [f for f in stack["orchestrator"].fills
                 if f.order_id == order.order_id]
        self.assertTrue(fills, "the fill never reached the orchestrator")
        lineage = lineage_from_order(order, session_id=session.session_id,
                                     fills=fills, code_version="phase-16")
        self.assertTrue(lineage.is_complete, lineage.missing_links)
        self.assertEqual(lineage.missing_links, [],
                         "a lineage with holes cannot answer why the trade "
                         "happened, which is the whole point of keeping it")
        self.assertEqual(lineage.model_version, "m-1")
        self.assertTrue(lineage.execution_ids)

        quality = quality_from_order(order, fills)
        self.assertIsNotNone(quality.slippage_bps)
        self.assertGreater(quality.slippage_bps, 0.0,
                           "a buy filled above the decision price should "
                           "show positive slippage")

        # --- outcome and journal ----------------------------------
        journal = ExecutionJournal(session_id=session.session_id)
        trade = TradeOutcome(
            outcome_id="out-1", instrument_id=INSTRUMENT,
            side=CanonicalOrderSide.BUY, quantity=10.0,
            lineage=lineage, quality=quality,
            entry_at=AT, exit_at=AT + timedelta(days=2),
            entry_price=100.05, exit_price=104.0, gross_pnl=39.5, fees=1.0,
            exit_reason=ExitReason.SIGNAL_EXIT, environment="paper")
        classify_errors(trade)
        journal.add_outcome(trade)

        self.assertAlmostEqual(trade.net_pnl, 38.5)
        self.assertTrue(trade.was_profitable)
        self.assertTrue(trade.post_mortem.direction_correct)
        self.assertIsNone(trade.post_mortem.data_error)

        report = journal.daily_report(AT + timedelta(days=2))
        self.assertEqual(report["trades_closed"], 1)
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["session_id"], session.session_id)

        # --- close ------------------------------------------------
        session.apply(SessionAction.STOP, "alice", AT + timedelta(hours=8),
                      "end of day")
        self.assertFalse(session.may_submit(AT + timedelta(hours=8))[0])

    def test_a_prevented_signal_is_recorded_as_a_miss(self):
        """
        Spec §22. Without this the journal records only what happened
        and cannot tell a bad signal from a good one risk refused.
        """
        from src.execution.outcomes import (
            ExecutionJournal, MissedTrade, MissReason, TradeLineage,
        )

        journal = ExecutionJournal(session_id="s")
        journal.add_missed(MissedTrade(
            missed_id="m-1", at=AT, instrument_id=INSTRUMENT,
            reason=MissReason.LIMIT_BLOCKED, detail="daily loss limit",
            side=CanonicalOrderSide.BUY, intended_quantity=10.0,
            reference_price=100.0,
            lineage=TradeLineage(correlation_id="c-1", signal_id="sig-1")))
        journal.add_missed(MissedTrade(
            missed_id="m-2", at=AT, instrument_id=INSTRUMENT,
            reason=MissReason.NEVER_FILLED, detail="limit not reached"))

        report = journal.daily_report(AT)
        self.assertEqual(report["missed"], 2)
        self.assertEqual(report["missed_prevented_by_system"], 1,
                         "an unfilled limit order is the market's doing, "
                         "not the system's")


# ============================================================
# §61 — environment comparison honesty
# ============================================================

class TestEnvironmentComparison(unittest.TestCase):

    def test_a_short_history_is_never_conclusive(self):
        comparison = compare_environments(
            AT, backtest={"median_slippage_bps": 5.0},
            paper={"median_slippage_bps": 6.0},
            live={"median_slippage_bps": 30.0, "trades": 4, "days": 3})
        self.assertFalse(comparison.is_conclusive)
        self.assertTrue(any("below the 30 trades" in n
                            for n in comparison.notes))

    def test_only_mechanical_metrics_are_reported_as_drift(self):
        """
        Return differences over a short period are noise. Calling them
        drift would invite acting on them.
        """
        comparison = compare_environments(
            AT, paper={"total_return": 0.10, "median_slippage_bps": 5.0},
            live={"total_return": -0.50, "median_slippage_bps": 5.0,
                  "trades": 100, "days": 100})
        drifted = [r.metric for r in comparison.drifted()]
        self.assertNotIn("total_return", drifted)

    def test_a_mechanical_divergence_is_reported(self):
        comparison = compare_environments(
            AT, paper={"median_slippage_bps": 5.0},
            live={"median_slippage_bps": 45.0, "trades": 100, "days": 100})
        self.assertIn("median_slippage_bps",
                      [r.metric for r in comparison.drifted()])


class TestMetricsRefuseToInvent(unittest.TestCase):

    def test_rates_are_none_rather_than_zero_when_nothing_happened(self):
        """
        A zero rejection rate over zero orders reads as a perfect
        record. It is an absence of evidence.
        """
        metrics = ExecutionMetrics(at=AT)
        for rate in (metrics.rejection_rate, metrics.fill_rate,
                     metrics.unknown_state_rate, metrics.error_rate,
                     metrics.median_slippage_bps):
            self.assertIsNone(rate)


if __name__ == "__main__":
    unittest.main()
