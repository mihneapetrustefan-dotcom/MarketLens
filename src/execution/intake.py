"""
src/execution/intake.py
-----------------------------
The join between Phase 11 (risk) and Phase 14 (execution).

WHY THIS MODULE EXISTS
--------------------------
The Phase 17 audit traced the chain the whole project is built to
produce:

    signal -> portfolio decision -> risk decision -> order intent
           -> execution order -> IBKR order -> fill -> outcome

and found it broken at exactly one joint. `IntentRequest` — the only
entry point into the execution stack — was constructed in two places,
both of them CLI scripts, both by hand, and both obtaining
`risk_approved` from a flag literally named `--assume-risk-approved`.

The Phase 11 risk engine produced `RiskDecision` objects that reached
the database and the dashboard and nothing else. The execution
validator, meanwhile, correctly refused to trade without a risk
verdict — so the only way to make an order was for a human to assert
one. Every safety control was present and the wire between them was
not.

The paper stack (Phase 13) never had this gap: `PaperSession` calls
`PortfolioService.evaluate()` and refuses to place anything the real
engine did not approve. So the gap was also an asymmetry — the paper
path was strictly safer than the broker path, which is the wrong way
round.

WHAT THIS MODULE GUARANTEES
-------------------------------
`risk_approved` on an `IntentRequest` built here is a fact about a
`RiskDecision` object, not a claim by a caller. There is no parameter
that overrides it. A caller holding an unapproved decision cannot
produce a request at all — `from_decision` raises rather than
returning something a later check has to catch.

WHAT IT DELIBERATELY DOES NOT DO
------------------------------------
It does not run the risk engine, size positions, choose a broker or
decide when to trade. It converts an approved decision into the shape
the execution layer accepts, and carries the lineage across. Anything
more would be a second risk engine, which is precisely the duplicate
source of truth this phase exists to eliminate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.domain.broker_models import CanonicalOrderSide, CanonicalTimeInForce
from src.domain.portfolio_models import (
    OrderIntent, RiskDecision, RiskDecisionState,
)
from src.execution.orchestrator import IntentRequest


class RiskNotApproved(Exception):
    """
    Raised when a caller tries to execute an intent the risk engine did
    not approve.

    An exception rather than a rejected result: a caller cannot
    accidentally ignore it, and there is no code path where continuing
    is the right answer.
    """


class LineageIncomplete(Exception):
    """
    Raised when an intent cannot be traced back to what produced it.

    Spec §51 requires every trade to carry its full provenance. A
    trade whose chain is already broken at submission can never be
    repaired afterwards, so it is refused at the boundary instead.
    """


#: Identifiers an execution request must carry to remain traceable.
#: `decision_id` and `portfolio_id` come from the risk decision;
#: `signal_id` from the intent that the decision approved.
REQUIRED_LINEAGE = ("decision_id", "portfolio_id", "signal_id")


@dataclass
class IntakeRejection:
    """One intent that will not become an order, and why."""
    intent_id: str
    instrument_id: str
    reason: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"intent_id": self.intent_id,
                "instrument_id": self.instrument_id,
                "reason": self.reason, "detail": self.detail}


@dataclass
class IntakeResult:
    """
    What one evaluation produced for the execution layer.

    Requests and rejections are returned together. A caller that only
    reads `requests` still trades correctly; a caller that reads both
    can also explain the trades that did not happen, which spec §22
    calls half the evidence.
    """
    requests: List[IntentRequest]
    rejected: List[IntakeRejection]
    decision_id: Optional[str] = None
    decision_state: Optional[str] = None

    @property
    def any_accepted(self) -> bool:
        return bool(self.requests)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision_state": self.decision_state,
            "accepted": len(self.requests),
            "rejected": [r.as_dict() for r in self.rejected],
        }


def _side(raw: str) -> CanonicalOrderSide:
    """Phase 11 stores the side as a string; Phase 14 wants the enum."""
    try:
        return CanonicalOrderSide(str(raw).strip().lower())
    except ValueError:
        raise ValueError(
            f"{raw!r} is not a side this system can execute. Phase 11 "
            f"intents carry 'buy' or 'sell'.")


def _reason_for(decision: RiskDecision) -> str:
    """A human-readable account of why a decision blocks execution."""
    if decision.reasons:
        return "; ".join(decision.reasons)
    if decision.violations:
        return "; ".join(v.detail or v.constraint_id
                         for v in decision.violations[:3])
    return decision.summary or decision.state.value


def from_decision(decision: RiskDecision,
                  intents: Sequence[OrderIntent],
                  *,
                  broker_id: str,
                  account_id: str,
                  now: datetime,
                  prices: Optional[Dict[str, float]] = None,
                  quantities: Optional[Dict[str, float]] = None,
                  strategy_id: Optional[str] = None,
                  model_version: Optional[str] = None,
                  predictions: Optional[Dict[str, str]] = None,
                  time_in_force: CanonicalTimeInForce = CanonicalTimeInForce.DAY,
                  policy: str = "market",
                  data_is_stale: bool = False,
                  freshness_detail: str = "",
                  require_lineage: bool = True) -> IntakeResult:
    """
    Turn an approved risk decision and its intents into execution
    requests.

    Raises `RiskNotApproved` when the decision does not permit exposure
    to change. That check comes first and has no override — every other
    argument is irrelevant if the risk engine said no.

    `prices` supplies the reference price per instrument. An intent
    with no price is REJECTED rather than sent: an order with no
    reference price cannot be validated for notional, slippage or
    staleness, and the execution validator would refuse it anyway —
    better to say so here, with the instrument named.
    """
    if decision is None:
        raise RiskNotApproved(
            "No risk decision was supplied. Absence of a verdict is not "
            "approval; the execution layer requires one.")

    if not decision.is_approved:
        raise RiskNotApproved(
            f"Risk decision {decision.decision_id} is "
            f"{decision.state.value.upper()}, which does not permit exposure "
            f"to change. {_reason_for(decision)}")

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")

    prices = prices or {}
    quantities = quantities or {}
    predictions = predictions or {}

    requests: List[IntentRequest] = []
    rejected: List[IntakeRejection] = []

    for intent in intents:
        # Belt and braces: the intent carries its own guard, and it is
        # invoked here too. `require_approval` is a static method on
        # OrderIntent precisely so no caller has to remember.
        try:
            OrderIntent.require_approval(decision)
        except ValueError as error:
            raise RiskNotApproved(str(error)) from error

        quantity = quantities.get(intent.instrument_id)
        if quantity is None:
            quantity = intent.target_quantity
        if quantity is None or quantity <= 0:
            rejected.append(IntakeRejection(
                intent_id=intent.intent_id,
                instrument_id=intent.instrument_id,
                reason="no_quantity",
                detail="the intent carries a target weight but no quantity, "
                       "and none was supplied; sizing belongs to the "
                       "portfolio layer, not to execution"))
            continue

        reference_price = prices.get(intent.instrument_id)
        if reference_price is None or reference_price <= 0:
            rejected.append(IntakeRejection(
                intent_id=intent.intent_id,
                instrument_id=intent.instrument_id,
                reason="no_reference_price",
                detail="an order with no reference price cannot be checked "
                       "for notional, slippage or staleness"))
            continue

        signal_id = intent.source_signal_id
        decision_id = intent.decision_id or decision.decision_id
        portfolio_id = intent.portfolio_id or decision.portfolio_id

        if require_lineage:
            missing = [name for name, value in
                       (("decision_id", decision_id),
                        ("portfolio_id", portfolio_id),
                        ("signal_id", signal_id)) if not value]
            if missing:
                raise LineageIncomplete(
                    f"Intent {intent.intent_id} cannot be traced: missing "
                    f"{', '.join(missing)}. A trade whose provenance is "
                    f"already broken at submission cannot be repaired "
                    f"afterwards (spec §51).")

        requests.append(IntentRequest(
            intent_id=intent.intent_id,
            broker_id=broker_id,
            account_id=account_id,
            instrument_id=intent.instrument_id,
            side=_side(intent.side),
            quantity=float(quantity),
            now=now,
            time_in_force=time_in_force,
            policy=policy,
            reference_price=float(reference_price),
            decision_price=float(reference_price),
            # The whole point of this module: a fact about the decision
            # object, not a claim by whoever called us.
            risk_approved=decision.is_approved,
            risk_detail=(f"risk decision {decision.decision_id} "
                         f"{decision.state.value}"),
            data_is_stale=data_is_stale,
            freshness_detail=freshness_detail,
            signal_id=signal_id,
            prediction_id=predictions.get(intent.instrument_id),
            model_version=model_version,
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            decision_id=decision_id,
            expires_at=intent.valid_until))

    return IntakeResult(requests=requests, rejected=rejected,
                        decision_id=decision.decision_id,
                        decision_state=decision.state.value)


def from_evaluation(evaluation: Any, **kwargs: Any) -> IntakeResult:
    """
    Convenience wrapper over a Phase 11 `EvaluationResult`.

    Kept deliberately thin: it unpacks `decision` and `intents` and
    delegates. The typed work lives in `from_decision`, so there is one
    implementation of the rule rather than two that can drift.
    """
    return from_decision(evaluation.decision, evaluation.intents, **kwargs)
