"""
tests/execution/test_phase16_governance.py
-----------------------------------------------
Execution levels, promotion gates, readiness, human approval, sessions
and operational limits (Phase 16, spec §7-§14, §25-§29, §41-§45,
§63-§64, §82-§83).

WHAT THESE DEFEND
---------------------
That the system cannot promote itself, cannot trade past a limit it has
breached, cannot start a session on unmeasured evidence, and cannot
reach a real-money level that has no execution path.

The tests that matter most assert REFUSALS, and several assert that a
missing measurement blocks — because a gate that passed on absent data
would be most permissive exactly when instrumentation had failed.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.broker_models import (
    AccountSnapshot, CanonicalOrderSide, ExecutionEnvironment, MarginSnapshot,
    MismatchKind, MismatchSeverity, PositionSnapshot, ReconciliationMismatch,
    ReconciliationRecord,
)
from src.execution.governance import (
    ApprovalState, DEFAULT_GATES, ExecutionGovernor, ExecutionLevel,
    PromotionGate, ReadinessAssessment, ReadinessCategory, ReadinessVerdict,
    assess_readiness,
)
from src.execution.limits import (
    CapitalLimits, DayState, ExecutionQualityLimits, FreshnessLimits,
    LimitBreach, LossLimits, MarketContext, RiskGovernor, paper_limits,
)
from src.execution.session import (
    PreflightCheck, SessionAction, SessionConfiguration, SessionState,
    SessionSummary, SessionTransitionError, new_session, standard_preflight,
)

AT = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)


def healthy_account(equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="DU1", broker_id="ibkr", at=AT, cash=equity, equity=equity,
        buying_power=equity, available_funds=equity)


def fresh_market() -> MarketContext:
    return MarketContext(quote_at=AT, account_at=AT, position_at=AT,
                         risk_at=AT, broker_time=AT, reference_price=100.0,
                         quote_is_live=True)


def flat_day() -> DayState:
    return DayState(day="2026-08-27", realized_pnl=0.0, unrealized_pnl=0.0,
                    starting_equity=100_000.0, current_equity=100_000.0,
                    peak_equity=100_000.0)


# ============================================================
# Execution levels
# ============================================================

class TestExecutionLevels(unittest.TestCase):

    def test_levels_five_and_above_are_real_money(self):
        for level in ExecutionLevel:
            expected = level >= ExecutionLevel.MICRO_CAPITAL_LIVE
            self.assertIs(level.is_real_money, expected, level.label)

    def test_no_real_money_level_is_implemented(self):
        """
        The central claim of this phase. Levels 4+ are specified and
        gated; none has an execution path.
        """
        for level in ExecutionLevel:
            if level.is_real_money:
                self.assertFalse(level.is_implemented, level.label)

    def test_anything_past_paper_requires_approval(self):
        for level in ExecutionLevel:
            self.assertEqual(level.requires_approval,
                             level > ExecutionLevel.PAPER, level.label)

    def test_levels_map_to_environments_and_none_is_live_below_five(self):
        for level in ExecutionLevel:
            environment = level.environment
            if level <= ExecutionLevel.LIVE_PREPARATION:
                self.assertFalse(environment.is_real_money, level.label)


class TestPromotionApproval(unittest.TestCase):
    """Spec §41, §83: nothing promotes itself."""

    def setUp(self):
        self.governor = ExecutionGovernor()

    def test_the_baseline_without_any_approval_is_paper(self):
        self.assertIs(self.governor.effective_level(AT), ExecutionLevel.PAPER)

    def test_a_request_alone_grants_nothing(self):
        self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        self.assertIs(self.governor.effective_level(AT), ExecutionLevel.PAPER)

    def test_nobody_may_approve_their_own_request(self):
        """
        The cheapest possible four-eyes control, and worth having even
        with one operator: it forces approval to be a deliberate second
        act rather than a continuation of the first.
        """
        request = self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        with self.assertRaises(ValueError) as caught:
            request.approve("alice", AT)
        self.assertIn("may not approve it", str(caught.exception))

    def test_a_second_actor_may_approve(self):
        request = self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        request.approve("bob", AT)
        self.assertIs(request.state, ApprovalState.APPROVED)
        self.assertIs(self.governor.effective_level(AT),
                      ExecutionLevel.BROKER_PAPER)

    def test_approvals_expire(self):
        """
        A standing permission nobody renews is how a temporary decision
        becomes permanent by inattention.
        """
        request = self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        request.approve("bob", AT, ttl=timedelta(hours=1))
        self.assertTrue(request.is_active(AT))
        self.assertFalse(request.is_active(AT + timedelta(hours=2)))
        self.assertIs(self.governor.effective_level(AT + timedelta(hours=2)),
                      ExecutionLevel.PAPER)

    def test_an_approval_can_be_revoked(self):
        request = self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        request.approve("bob", AT)
        request.revoke("bob", AT, "withdrawn after review")
        self.assertIs(self.governor.effective_level(AT), ExecutionLevel.PAPER)

    def test_approving_a_rejected_request_is_refused(self):
        request = self.governor.request(ExecutionLevel.BROKER_PAPER, "alice", AT)
        request.reject("bob", AT, "not yet")
        with self.assertRaises(ValueError):
            request.approve("carol", AT)

    def test_approving_an_unimplemented_level_still_degrades(self):
        """
        Approving a level the code cannot execute does not make it
        executable. The approval is recorded; the effective level is
        the highest IMPLEMENTED one.
        """
        request = self.governor.request(ExecutionLevel.PRODUCTION_LIVE,
                                        "alice", AT)
        request.approve("bob", AT)
        effective = self.governor.effective_level(AT)
        self.assertFalse(effective.is_real_money)
        self.assertIs(effective, ExecutionLevel.BROKER_PAPER)

    def test_the_state_never_reports_real_money_as_reachable(self):
        request = self.governor.request(ExecutionLevel.PRODUCTION_LIVE,
                                        "alice", AT)
        request.approve("bob", AT)
        self.assertFalse(self.governor.state(AT)["real_money_reachable"])


class TestPromotionGates(unittest.TestCase):
    """Spec §8: measurable criteria, and absence blocks."""

    def setUp(self):
        self.governor = ExecutionGovernor()
        self.passing = {
            "paper_days": 45, "paper_trades": 60, "max_drawdown_pct": 0.08,
            "worst_daily_loss_pct": 0.02, "max_position_weight": 0.15,
            "max_leverage": 1.0, "execution_error_rate": 0.0,
            "median_slippage_bps": 8.0, "rejection_rate": 0.02,
            "reconciliation_mismatch_rate": 0.0, "unknown_state_rate": 0.0,
            "broker_uptime": 0.99, "signal_stability": 0.8,
            "model_stability": 0.8,
        }
        self.ready = assess_readiness(
            AT, execution={"error_rate": 0.0}, broker={"connected": True},
            reconciliation={"clean": True}, data={"fresh": True},
            risk={"healthy": True}, security_reviewed=True,
            operations_ready=True)
        for category in ReadinessCategory:
            if self.ready.verdicts[category] is ReadinessVerdict.UNKNOWN:
                self.ready.record(category, ReadinessVerdict.PASS, "test")

    def test_an_unmeasured_gate_blocks(self):
        """
        Not measuring something is not evidence that it is fine, and a
        gate that passed on absent data would be most permissive
        exactly when instrumentation had failed.
        """
        evaluation = self.governor.evaluate(
            ExecutionLevel.MICRO_CAPITAL_LIVE, AT, metrics={},
            readiness=self.ready)
        self.assertFalse(evaluation.gates_pass)
        for gate in evaluation.gates:
            self.assertFalse(gate.measured, gate.gate_id)
            self.assertTrue(gate.blocks)

    def test_every_gate_passes_with_good_metrics(self):
        evaluation = self.governor.evaluate(
            ExecutionLevel.MICRO_CAPITAL_LIVE, AT, self.passing, self.ready)
        self.assertTrue(evaluation.gates_pass,
                        [g.explain() for g in evaluation.failing_gates])

    def test_gates_still_do_not_permit_an_unimplemented_level(self):
        """
        Gates measure evidence. Implementation is a fact about the
        code, and no amount of evidence changes it.
        """
        request = self.governor.request(ExecutionLevel.MICRO_CAPITAL_LIVE,
                                        "alice", AT)
        request.approve("bob", AT)
        evaluation = self.governor.evaluate(
            ExecutionLevel.MICRO_CAPITAL_LIVE, AT, self.passing, self.ready)
        self.assertTrue(evaluation.gates_pass)
        self.assertTrue(evaluation.approved)
        self.assertTrue(evaluation.readiness.is_ready)
        self.assertFalse(evaluation.permitted)
        self.assertIn("no execution path", evaluation.explain())

    def test_a_max_gate_compares_the_right_way_round(self):
        """
        A gate called `max_drawdown` compared the wrong way would pass
        exactly the systems it exists to stop.
        """
        metrics = dict(self.passing, max_drawdown_pct=0.90)
        evaluation = self.governor.evaluate(
            ExecutionLevel.MICRO_CAPITAL_LIVE, AT, metrics, self.ready)
        failing = [g.gate_id for g in evaluation.failing_gates]
        self.assertIn("max_drawdown", failing)

    def test_profitability_is_deliberately_not_a_gate(self):
        """
        Spec §8. A strategy can be profitable over a short period by
        luck, and promoting on that basis commits capital to noise.
        """
        metrics = {g.metric for g in DEFAULT_GATES}
        for forbidden in ("total_return", "profit", "sharpe", "pnl",
                          "win_rate"):
            self.assertNotIn(forbidden, metrics)

    def test_gates_do_not_apply_below_live_preparation(self):
        evaluation = self.governor.evaluate(ExecutionLevel.PAPER, AT, {})
        self.assertEqual(evaluation.gates, [])
        self.assertTrue(evaluation.permitted)


class TestReadiness(unittest.TestCase):
    """Spec §9."""

    def test_unmeasured_categories_block(self):
        assessment = ReadinessAssessment(at=AT)
        self.assertFalse(assessment.is_ready)
        self.assertEqual(len(assessment.blocking), len(ReadinessCategory))

    def test_conditional_also_blocks_readiness(self):
        assessment = assess_readiness(AT, security_reviewed=True,
                                      operations_ready=False)
        self.assertIn(ReadinessCategory.OPERATIONS, assessment.conditional)
        self.assertFalse(assessment.is_ready)

    def test_there_is_no_weighted_score_that_could_offset_a_failure(self):
        """
        Security and execution are not substitutable, and a single
        number would imply they are.
        """
        assessment = ReadinessAssessment(at=AT)
        self.assertFalse(hasattr(assessment, "score"))
        self.assertFalse(hasattr(assessment, "weighted"))

    def test_every_category_is_represented(self):
        assessment = ReadinessAssessment(at=AT)
        self.assertEqual(set(assessment.verdicts), set(ReadinessCategory))


# ============================================================
# Operational limits
# ============================================================

class TestOperationalLimits(unittest.TestCase):
    """Spec §12-§15, §25, §64."""

    def setUp(self):
        self.governor = paper_limits()

    def check(self, **overrides):
        base = dict(side=CanonicalOrderSide.BUY, quantity=100.0, price=100.0,
                    instrument_id="i-aapl", account=healthy_account(),
                    positions=[], day=flat_day(), market=fresh_market(),
                    broker_healthy=True, reconciliation_clean=True)
        base.update(overrides)
        return self.governor.check(AT, **base)

    def test_a_clean_order_passes_every_check(self):
        decision = self.check()
        self.assertTrue(decision.permitted, decision.explain())
        self.assertGreaterEqual(decision.checks_performed, 15)

    def test_unmeasured_broker_health_blocks(self):
        self.assertFalse(self.check(broker_healthy=None).permitted)

    def test_unmeasured_reconciliation_blocks(self):
        self.assertFalse(self.check(reconciliation_clean=None).permitted)

    def test_stale_state_blocks_each_input_separately(self):
        """Spec §15: quote, account, position AND risk, not just quotes."""
        for field, breach in (("quote_at", LimitBreach.STALE_QUOTE),
                              ("account_at", LimitBreach.STALE_ACCOUNT),
                              ("position_at", LimitBreach.STALE_POSITION),
                              ("risk_at", LimitBreach.STALE_RISK)):
            market = fresh_market()
            setattr(market, field, AT - timedelta(hours=2))
            decision = self.check(market=market)
            self.assertIn(breach, [b for b, _ in decision.breaches], field)

    def test_a_missing_timestamp_is_treated_as_stale(self):
        market = fresh_market()
        market.quote_at = None
        self.assertFalse(self.check(market=market).permitted)

    def test_data_stamped_in_the_future_is_refused(self):
        market = fresh_market()
        market.quote_at = AT + timedelta(minutes=5)
        self.assertFalse(self.check(market=market).permitted)

    def test_clock_drift_blocks(self):
        market = fresh_market()
        market.broker_time = AT - timedelta(seconds=30)
        decision = self.check(market=market)
        self.assertIn(LimitBreach.CLOCK_DRIFT,
                      [b for b, _ in decision.breaches])

    def test_the_daily_loss_limit_latches(self):
        """
        A limit that resumed the moment the market ticked back up would
        defeat its own purpose.
        """
        day = DayState(day="d", realized_pnl=-9_000.0, unrealized_pnl=0.0,
                       starting_equity=100_000.0, current_equity=91_000.0,
                       peak_equity=100_000.0)
        first = self.check(day=day)
        self.assertFalse(first.permitted)
        self.assertIn(LimitBreach.DAILY_TOTAL_LOSS, first.latching)

        recovered = self.check(day=flat_day())
        self.assertFalse(recovered.permitted,
                         "a latched loss limit cleared itself")

    def test_clearing_a_latch_requires_an_actor_and_a_reason(self):
        day = DayState(day="d", realized_pnl=-9_000.0, unrealized_pnl=0.0,
                       starting_equity=100_000.0, current_equity=91_000.0,
                       peak_equity=100_000.0)
        self.check(day=day)
        with self.assertRaises(ValueError):
            self.governor.reactivate_all("", "")
        self.governor.reactivate_all("operator", "reviewed and reset")
        self.assertTrue(self.check(day=flat_day()).permitted)

    def test_staleness_does_not_latch(self):
        """Staleness legitimately recovers; a loss limit does not."""
        market = fresh_market()
        market.quote_at = AT - timedelta(hours=2)
        self.check(market=market)
        self.assertTrue(self.check().permitted)

    def test_order_notional_cap(self):
        decision = self.check(quantity=1_000.0)
        self.assertIn(LimitBreach.MAX_ORDER_NOTIONAL,
                      [b for b, _ in decision.breaches])

    def test_position_notional_uses_the_projected_position(self):
        held = [PositionSnapshot(account_id="DU1", broker_id="ibkr",
                                 instrument_id="i-aapl", quantity=450.0,
                                 average_price=100.0, at=AT)]
        decision = self.check(quantity=100.0, positions=held)
        self.assertIn(LimitBreach.MAX_POSITION_NOTIONAL,
                      [b for b, _ in decision.breaches])

    def test_open_position_count_only_blocks_a_new_instrument(self):
        governor = RiskGovernor(capital=CapitalLimits(max_open_positions=1))
        held = [PositionSnapshot(account_id="DU1", broker_id="ibkr",
                                 instrument_id="i-msft", quantity=10.0,
                                 average_price=100.0, at=AT)]
        self.governor = governor
        opening_new = self.check(instrument_id="i-aapl", positions=held)
        self.assertIn(LimitBreach.MAX_OPEN_POSITIONS,
                      [b for b, _ in opening_new.breaches])
        adding = self.check(instrument_id="i-msft", positions=held)
        self.assertNotIn(LimitBreach.MAX_OPEN_POSITIONS,
                         [b for b, _ in adding.breaches])

    def test_buying_power_respects_the_cash_reserve(self):
        governor = RiskGovernor(capital=CapitalLimits(reserve_cash=95_000.0))
        self.governor = governor
        decision = self.check(quantity=100.0, price=100.0)
        self.assertIn(LimitBreach.INSUFFICIENT_MARGIN,
                      [b for b, _ in decision.breaches])

    def test_maintenance_margin_breach_blocks(self):
        account = healthy_account(1_000.0)
        account.margin = MarginSnapshot(maintenance_margin=5_000.0)
        decision = self.check(account=account, quantity=1.0)
        self.assertIn(LimitBreach.MAINTENANCE_MARGIN,
                      [b for b, _ in decision.breaches])

    def test_liquidity_participation_ceiling(self):
        market = fresh_market()
        market.average_volume = 500.0
        decision = self.check(quantity=100.0, market=market)
        self.assertIn(LimitBreach.LIQUIDITY, [b for b, _ in decision.breaches])

    def test_real_money_caps_must_be_configured_before_real_money(self):
        """Spec §26: no strategy acquires unlimited capital automatically."""
        decision = self.check(require_real_money_config=True)
        self.assertIn(LimitBreach.MAX_LIVE_CAPITAL,
                      [b for b, _ in decision.breaches])

    def test_no_real_money_defaults_are_shipped(self):
        """
        Spec §25 warns against exactly this: a number shipped as a
        default becomes a production default by inattention.
        """
        limits = CapitalLimits()
        self.assertIsNone(limits.max_live_capital)
        self.assertIsNone(limits.max_order_notional)
        self.assertFalse(limits.configured_for_real_money)

    def test_quality_thresholds_pause_execution(self):
        decision = self.check(quality_metrics={"median_slippage_bps": 500.0})
        self.assertFalse(decision.permitted)


# ============================================================
# Sessions
# ============================================================

class TestSessions(unittest.TestCase):
    """Spec §42-§45, §56, §87."""

    def config(self, **overrides):
        base = dict(account_id="DU1", strategies=("s1",), model_version="m-1")
        base.update(overrides)
        return SessionConfiguration(**base)

    def started(self):
        session = new_session(self.config(), "alice", AT)
        session.run_preflight(
            standard_preflight(**{k: True for k in (
                "broker_connected", "account_available", "market_data_live",
                "reconciliation_clean", "risk_available", "capital_configured",
                "no_unknown_orders", "kill_switch_off")}), "alice", AT)
        session.apply(SessionAction.START, "alice", AT)
        return session

    def test_a_session_cannot_be_configured_for_real_money(self):
        with self.assertRaises(ValueError):
            SessionConfiguration(environment=ExecutionEnvironment.LIVE)

    def test_preflight_must_pass_before_orders(self):
        session = new_session(self.config(), "alice", AT)
        session.apply(SessionAction.START, "alice", AT)
        permitted, why = session.may_submit(AT)
        self.assertFalse(permitted)
        self.assertIn("validation", why)

    def test_an_unmeasured_preflight_check_blocks(self):
        session = new_session(self.config(), "alice", AT)
        passed = session.run_preflight(
            standard_preflight(broker_connected=True), "alice", AT)
        self.assertFalse(passed)

    def test_configuration_is_frozen_once_active(self):
        session = self.started()
        with self.assertRaises(SessionTransitionError):
            session.amend("alice", AT, "widen caps", capital_limit=1e9)

    def test_configuration_drift_is_detected_even_if_amend_is_bypassed(self):
        """
        Checked rather than trusted: a mutation that bypassed `amend`
        still reaches a report, because the fingerprint is recomputed
        and compared.
        """
        session = self.started()
        session.config.capital_limit = 999_999.0
        self.assertTrue(session.configuration_drifted)
        self.assertFalse(session.may_submit(AT)[0])

    def test_an_emergency_stop_is_terminal(self):
        session = self.started()
        session.apply(SessionAction.EMERGENCY_STOP, "alice", AT, "anomaly")
        self.assertFalse(session.state.is_resumable)
        with self.assertRaises(SessionTransitionError):
            session.apply(SessionAction.RESUME, "alice", AT)

    def test_a_routine_pause_resumes(self):
        session = self.started()
        session.apply(SessionAction.PAUSE, "alice", AT)
        self.assertFalse(session.may_submit(AT)[0])
        session.apply(SessionAction.RESUME, "alice", AT)
        self.assertTrue(session.may_submit(AT)[0])

    def test_stopping_requires_a_reason(self):
        session = self.started()
        with self.assertRaises(ValueError):
            session.apply(SessionAction.STOP, "alice", AT)

    def test_every_action_names_an_actor(self):
        session = self.started()
        with self.assertRaises(ValueError):
            session.apply(SessionAction.PAUSE, "", AT)

    def test_history_is_kept_after_a_stop(self):
        """Spec §43: nothing is deleted."""
        session = self.started()
        before = len(session.events)
        session.apply(SessionAction.STOP, "alice", AT, "end of day")
        self.assertGreater(len(session.events), before)
        self.assertTrue(session.events[0].actor)

    def test_a_level_needing_approval_refuses_without_one(self):
        session = new_session(
            self.config(level=ExecutionLevel.BROKER_PAPER), "alice", AT)
        session.run_preflight(
            standard_preflight(**{k: True for k in (
                "broker_connected", "account_available", "market_data_live",
                "reconciliation_clean", "risk_available", "capital_configured",
                "no_unknown_orders", "kill_switch_off")}), "alice", AT)
        session.apply(SessionAction.START, "alice", AT)
        permitted, why = session.may_submit(AT)
        self.assertFalse(permitted)
        self.assertIn("approval", why)

    def test_a_summary_with_open_orders_is_not_a_clean_close(self):
        summary = SessionSummary(session_id="s", at=AT,
                                 reconciliation_clean=True, open_orders=1)
        self.assertFalse(summary.is_clean_close)

    def test_a_summary_with_unknown_orders_is_not_a_clean_close(self):
        summary = SessionSummary(session_id="s", at=AT,
                                 reconciliation_clean=True, orders_unknown=1)
        self.assertFalse(summary.is_clean_close)

    def test_the_fingerprint_covers_every_version_field(self):
        baseline = self.config().fingerprint()
        for field, value in (("model_version", "m-2"),
                             ("strategy_version", "s-2"),
                             ("feature_version", "f-2"),
                             ("signal_version", "sig-2"),
                             ("risk_config_version", "v2"),
                             ("execution_config_version", "exec-v2"),
                             ("capital_limit", 1234.0)):
            self.assertNotEqual(baseline,
                                self.config(**{field: value}).fingerprint(),
                                field)


# ============================================================
# Reconciliation severity
# ============================================================

class TestReconciliationSeverity(unittest.TestCase):
    """Spec §32, §33."""

    def record(self, *kinds):
        record = ReconciliationRecord(
            reconciliation_id="r", broker_id="ibkr", account_id="DU1", at=AT)
        record.mismatches = [ReconciliationMismatch(kind=k, detail="test")
                             for k in kinds]
        return record

    def test_money_and_exposure_differences_are_critical(self):
        for kind in (MismatchKind.POSITION_MISMATCH, MismatchKind.CASH_MISMATCH,
                     MismatchKind.DUPLICATE_FILL,
                     MismatchKind.UNKNOWN_BROKER_ORDER):
            self.assertIs(kind.severity, MismatchSeverity.CRITICAL, kind.value)

    def test_only_critical_blocks_execution(self):
        self.assertTrue(self.record(MismatchKind.POSITION_MISMATCH)
                        .blocks_execution)
        self.assertFalse(self.record(MismatchKind.PRICE_MISMATCH)
                         .blocks_execution)
        self.assertFalse(self.record(MismatchKind.STATUS_MISMATCH)
                         .blocks_execution)

    def test_only_info_findings_auto_resolve(self):
        record = self.record(MismatchKind.PRICE_MISMATCH,
                             MismatchKind.POSITION_MISMATCH)
        self.assertEqual(record.auto_resolve_safe(), 1)
        self.assertTrue(record.blocks_execution)

    def test_the_system_cannot_resolve_a_critical_finding(self):
        """
        Spec §33: never automatically "fix" an unknown capital or
        position discrepancy — it destroys the evidence of its cause.
        """
        record = self.record(MismatchKind.CASH_MISMATCH)
        with self.assertRaises(ValueError):
            record.mismatches[0].resolve("system", "looks fine")

    def test_a_human_can_resolve_a_critical_finding_with_a_note(self):
        record = self.record(MismatchKind.CASH_MISMATCH)
        record.mismatches[0].resolve("operator", "verified against statement")
        self.assertFalse(record.blocks_execution)

    def test_resolving_requires_a_note(self):
        record = self.record(MismatchKind.PRICE_MISMATCH)
        with self.assertRaises(ValueError):
            record.mismatches[0].resolve("operator", "")

    def test_the_worst_severity_is_reported_not_the_average(self):
        record = self.record(MismatchKind.PRICE_MISMATCH,
                             MismatchKind.PRICE_MISMATCH,
                             MismatchKind.CASH_MISMATCH)
        self.assertIs(record.worst_severity, MismatchSeverity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
