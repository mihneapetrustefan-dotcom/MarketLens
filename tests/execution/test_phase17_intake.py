"""
tests/execution/test_phase17_intake.py
--------------------------------------------
The joint between risk and execution (Phase 17, spec §20, §25, §51,
§60, §65).

WHAT THIS DEFENDS
---------------------
That `risk_approved` on an execution request is a FACT ABOUT A RISK
DECISION and never a claim by a caller.

Before Phase 17 the only producers of that field were two CLI flags
named `--assume-risk-approved`. The validator refused to trade without
a verdict, correctly — so the only way to get an order was for a human
to assert one, and the Phase 11 risk engine's actual output reached
the database and stopped there.

These tests exist so that gap cannot silently reopen. The most
important ones assert REFUSALS, and one asserts an absence: that this
module offers no override parameter at all.
"""

import inspect
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.broker_models import CanonicalOrderSide
from src.domain.portfolio_models import (
    OrderIntent, RiskDecision, RiskDecisionState, RiskViolation,
)
from src.execution import intake
from src.execution.intake import (
    IntakeResult, LineageIncomplete, RiskNotApproved, from_decision,
)

AT = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)


def decision(state=RiskDecisionState.APPROVED, **overrides):
    base = dict(decision_id="dec-1", portfolio_id="pf-1", state=state,
                as_of=AT, summary="test")
    base.update(overrides)
    return RiskDecision(**base)


def intent(**overrides):
    base = dict(intent_id="int-1", portfolio_id="pf-1",
                instrument_id="i-aapl", side="buy", target_quantity=10.0,
                source_signal_id="sig-1", decision_id="dec-1")
    base.update(overrides)
    return OrderIntent(**base)


def convert(dec=None, intents=None, **overrides):
    base = dict(broker_id="ibkr", account_id="DU1234567", now=AT,
                prices={"i-aapl": 100.0})
    base.update(overrides)
    return from_decision(dec or decision(), intents or [intent()], **base)


class TestOnlyApprovedRiskReachesExecution(unittest.TestCase):
    """Spec §20: risk decisions cannot be bypassed."""

    def test_an_approved_decision_produces_a_request(self):
        result = convert()
        self.assertEqual(len(result.requests), 1)
        self.assertTrue(result.any_accepted)

    def test_the_request_carries_the_decision_not_a_claim(self):
        result = convert()
        request = result.requests[0]
        self.assertIs(request.risk_approved, True)
        self.assertIn("dec-1", request.risk_detail)
        self.assertIn("approved", request.risk_detail)

    def test_a_rejected_decision_raises(self):
        with self.assertRaises(RiskNotApproved) as caught:
            convert(decision(RiskDecisionState.REJECTED,
                             reasons=["position limit breached"]))
        self.assertIn("REJECTED", str(caught.exception))
        self.assertIn("position limit breached", str(caught.exception))

    def test_requires_review_raises(self):
        """A judgement call is not an approval."""
        with self.assertRaises(RiskNotApproved):
            convert(decision(RiskDecisionState.REQUIRES_REVIEW))

    def test_insufficient_data_raises(self):
        """
        The verdict the engine returns when it cannot establish safety.
        Treating it as approval would make missing data the most
        permissive state in the system.
        """
        with self.assertRaises(RiskNotApproved):
            convert(decision(RiskDecisionState.INSUFFICIENT_DATA))

    def test_a_reduced_decision_is_permitted(self):
        """REDUCED means 'yes, but smaller' — exposure may still change."""
        result = convert(decision(RiskDecisionState.REDUCED))
        self.assertEqual(len(result.requests), 1)
        self.assertIs(result.requests[0].risk_approved, True)

    def test_no_decision_at_all_raises(self):
        with self.assertRaises(RiskNotApproved):
            from_decision(None, [intent()], broker_id="ibkr",
                          account_id="DU1", now=AT)

    def test_there_is_no_override_parameter(self):
        """
        Asserted as an ABSENCE, deliberately.

        The whole failure this module fixes was a parameter that let a
        caller assert approval. A future contributor adding one back
        has to delete this test to do it.
        """
        # Matched on whole underscore-separated tokens: a substring
        # test flags `time_in_force` for containing "force", which is
        # how an assertion starts getting weakened until it means
        # nothing.
        forbidden = {"assume", "override", "bypass", "unapproved",
                     "unchecked", "unsafe"}
        for name in inspect.signature(from_decision).parameters:
            tokens = set(name.lower().split("_"))
            self.assertEqual(tokens & forbidden, set(),
                             f"{name} looks like a risk override")
            self.assertNotEqual(name, "risk_approved",
                                "risk approval must come from the decision")

    def test_risk_approved_is_never_accepted_as_an_argument(self):
        with self.assertRaises(TypeError):
            convert(risk_approved=True)


class TestLineage(unittest.TestCase):
    """Spec §51: every trade traceable to what produced it."""

    def test_the_full_chain_is_carried_across(self):
        result = convert(model_version="m-1", strategy_id="strat-1",
                         predictions={"i-aapl": "pred-1"})
        request = result.requests[0]
        self.assertEqual(request.signal_id, "sig-1")
        self.assertEqual(request.decision_id, "dec-1")
        self.assertEqual(request.portfolio_id, "pf-1")
        self.assertEqual(request.prediction_id, "pred-1")
        self.assertEqual(request.model_version, "m-1")
        self.assertEqual(request.strategy_id, "strat-1")

    def test_an_untraceable_intent_is_refused(self):
        with self.assertRaises(LineageIncomplete) as caught:
            convert(intents=[intent(source_signal_id=None)])
        self.assertIn("signal_id", str(caught.exception))

    def test_lineage_can_be_relaxed_for_an_operator_smoke_test(self):
        """
        Explicitly named and off by default. An operator checking the
        wiring by hand has no signal, and refusing that is unhelpful —
        but it must be a deliberate act, not a silent default.
        """
        result = convert(intents=[intent(source_signal_id=None)],
                         require_lineage=False)
        self.assertEqual(len(result.requests), 1)

    def test_the_decision_supplies_ids_the_intent_omits(self):
        result = convert(intents=[intent(decision_id=None,
                                         portfolio_id="pf-1")])
        self.assertEqual(result.requests[0].decision_id, "dec-1")


class TestWhatIsRefusedRatherThanGuessed(unittest.TestCase):
    """Spec §17: no silent state corrections, no invented inputs."""

    def test_an_intent_with_no_price_is_rejected_not_sent(self):
        result = convert(prices={})
        self.assertEqual(result.requests, [])
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].reason, "no_reference_price")

    def test_an_intent_with_no_quantity_is_rejected_not_sized_here(self):
        """
        Sizing belongs to the portfolio layer. Inventing a quantity in
        the execution intake would be a second sizing engine, and a
        second source of truth.
        """
        result = convert(intents=[intent(target_quantity=None,
                                         target_weight=0.05)])
        self.assertEqual(result.requests, [])
        self.assertEqual(result.rejected[0].reason, "no_quantity")

    def test_rejections_are_reported_not_discarded(self):
        """Spec §22: the trades that did not happen are half the evidence."""
        result = convert(
            intents=[intent(), intent(intent_id="int-2",
                                      instrument_id="i-msft")],
            prices={"i-aapl": 100.0})
        self.assertEqual(len(result.requests), 1)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].instrument_id, "i-msft")
        self.assertIn("i-msft", str(result.as_dict()))

    def test_an_unparseable_side_raises_rather_than_defaulting(self):
        with self.assertRaises(ValueError):
            convert(intents=[intent(side="hold")])

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            convert(now=datetime(2026, 9, 4, 14, 0))


class TestItReachesTheRealExecutionStack(unittest.TestCase):
    """
    The join is only real if the request the intake builds is one the
    orchestrator actually accepts. Asserted end to end against the
    mock venue rather than by inspecting fields.
    """

    def test_an_approved_decision_becomes_an_ibkr_paper_order(self):
        from tests.execution.ibkr.helpers import build_ibkr, INSTRUMENT
        from src.domain.broker_models import ExecutionOrderState

        stack = build_ibkr()
        result = convert(
            intents=[intent(instrument_id=INSTRUMENT)],
            prices={INSTRUMENT: 100.0},
            account_id="DU1234567",
            now=datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc))

        outcome = stack["orchestrator"].execute(result.requests[0])
        self.assertIsNotNone(outcome.order, outcome.validation.explanation)
        self.assertIsNot(outcome.order.state, ExecutionOrderState.REJECTED)
        self.assertEqual(outcome.order.decision_id, "dec-1")
        self.assertEqual(outcome.order.signal_id, "sig-1")

    def test_a_rejected_decision_never_reaches_the_venue(self):
        from tests.execution.ibkr.helpers import build_ibkr

        stack = build_ibkr()
        with self.assertRaises(RiskNotApproved):
            convert(decision(RiskDecisionState.REJECTED))
        self.assertEqual(stack["transport"].place_calls, 0)


if __name__ == "__main__":
    unittest.main()
