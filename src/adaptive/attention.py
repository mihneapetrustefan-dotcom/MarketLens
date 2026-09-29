"""
src/adaptive/attention.py
---------------------------------------
Deciding where market-data attention goes (Phase 25.9I-C, spec sections 5, 12-14, 30-31, 43).

THE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
    REALTIME CAPACITY IS NOT A TARGET.

Once an exchange has reached its monthly cap, the marginal monetary
cost of another request on it is genuinely zero. The temptation is then
to make everything HOT, because why not. Three reasons why not:

    - request rate is still a real limit, and a storm is still a storm;
    - a HOT instrument carries a freshness OBLIGATION -- something is
      now expected to be execution-grade, and a stale stream on a HOT
      instrument is a fault, where the same staleness on a COLD one is
      nothing at all;
    - attention that is spread over 31 instruments is not attention.

So `AttentionAllocator` grants HOT on the basis of DECISION VALUE and
nothing else. Cheap data widens what is affordable; it does not widen
what is worth watching.

WHAT THIS MODULE WILL NOT INVENT
------------------------------------
No default `max_hot_symbols`. No proximity threshold, no horizon
threshold, no urgency score. Every policy value defaults to None, which
means "that test does not apply", and a caller that wants a rule states
its number. Spec section 14 is explicit: do not hard-code 1, 3 or 5
without evidence, and there is no evidence, because no strategy has run
on realtime data yet -- Phase 25.9H's four sessions were entirely
delayed.

A THESIS ASKS; IT DOES NOT ACTIVATE
---------------------------------------
25.9I-E emits `PositionActionRecommendation`. This module translates one
into a `DataAttentionRequest` (spec section 29) so the interface 25.9I-F
needs exists now. The translation imports only domain types: no
execution module, no broker path, no order of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.billing import (
    BillableRequestPlan, BillingUsage, MarketDataBillingGovernor,
)
from src.adaptive.exchange_map import UNKNOWN_EXCHANGE, ExchangeMap
from src.domain.adaptive_models import (
    BudgetPolicy, MarketDataState, MarketDataStateTransition,
    RealtimeActivationReason, RealtimeDeactivationReason,
)
from src.domain.data_attention_models import (
    AttentionDecision, AttentionPlan, AttentionPriority, DataAttentionRequest,
)
from src.domain.market_data_billing_models import BillingPeriod
from src.domain.thesis_models import PositionAction, PositionActionRecommendation

#: State ordering, used to decide which of several requests is strongest
#: and whether a plan escalated or de-escalated.
STATE_RANK: Dict[MarketDataState, int] = {
    MarketDataState.COLD: 0,
    MarketDataState.WARM: 1,
    MarketDataState.HOT: 2,
}

#: Which recommendations are execution-sensitive, and therefore the only
#: ones that may ask for HOT. `HOLD` is not among them -- 25.9I-E
#: establishes that a price near its target produces HOLD plus a WARM
#: re-check, never an escalation to execution grade.
_EXECUTION_SENSITIVE_ACTIONS = frozenset({
    PositionAction.PARTIAL_EXIT, PositionAction.FULL_EXIT,
})


@dataclass(frozen=True)
class AttentionConfig:
    """
    Every policy value, all of them inert by default.

    `max_hot_symbols=None` means no ceiling is configured, NOT unlimited
    appetite: nothing escalates without a reason regardless. A ceiling
    is a second, cruder guard on top of the reasons.
    """
    max_hot_symbols: Optional[int] = None
    max_warm_symbols: Optional[int] = None
    hot_ttl_seconds: Optional[float] = None
    warm_ttl_seconds: Optional[float] = None
    max_quote_age_seconds: float = 60.0
    #: When True, an escalation whose cost the governor refuses is
    #: downgraded rather than dropped, so the instrument keeps the best
    #: state that WAS affordable instead of falling to nothing.
    downgrade_on_budget_refusal: bool = True

    def __post_init__(self):
        for name in ("max_hot_symbols", "max_warm_symbols"):
            value = getattr(self, name)
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("hot_ttl_seconds", "warm_ttl_seconds"):
            value = getattr(self, name)
            if value is not None and float(value) <= 0:
                raise ValueError(f"{name} must be > 0")


def request_from_recommendation(
        recommendation: PositionActionRecommendation, *,
        requester: str = "thesis_engine",
        priority: Optional[AttentionPriority] = None,
        session_snapshot_id: Optional[str] = None,
) -> DataAttentionRequest:
    """
    Turn a 25.9I-E recommendation into a data request (spec section 29).

    An exit recommendation is execution-sensitive and asks for HOT; a
    hold asks for WARM at most. The mapping is deliberately narrow, and
    `EXTEND_TARGET` is NOT execution-sensitive -- 25.9I-E already refuses
    to extend a target on price alone, and giving the extension realtime
    data would reward exactly the behaviour that rule prevents.
    """
    action = recommendation.action
    if action in _EXECUTION_SENSITIVE_ACTIONS:
        state = MarketDataState.HOT
        activation = RealtimeActivationReason.EXIT_CANDIDATE
        default_priority = AttentionPriority.POSITION_EXIT
    elif action in (PositionAction.TIGHTEN_STOP, PositionAction.REDUCE_TARGET):
        state = MarketDataState.WARM
        activation = None
        default_priority = AttentionPriority.POSITION_MONITOR
    else:
        state = MarketDataState.WARM
        activation = None
        default_priority = AttentionPriority.POSITION_MONITOR
    return DataAttentionRequest(
        instrument_id=recommendation.instrument_id,
        desired_state=state, requester=requester,
        reason=("; ".join(recommendation.reasons)
                or f"thesis recommendation: {action.value}"),
        priority=priority or default_priority,
        activation_reason=activation, as_of=recommendation.at,
        lineage_id=recommendation.lineage_id,
        thesis_id=recommendation.thesis_id,
        session_snapshot_id=session_snapshot_id)


class AttentionAllocator:
    """
    Resolves many requests into one explainable plan per cycle.

    Deterministic: no clock (the caller passes `as_of`), no connection,
    no transport. The same requests in a different order produce the
    same plan.
    """

    def __init__(self, governor: MarketDataBillingGovernor,
                 exchange_map: ExchangeMap,
                 config: Optional[AttentionConfig] = None):
        self.governor = governor
        self.exchange_map = exchange_map
        self.config = config or AttentionConfig()

    # ---------------- planning ----------------

    def plan(self, requests: Sequence[DataAttentionRequest], *,
             as_of: datetime, period: BillingPeriod,
             usage: Optional[BillingUsage] = None,
             current_states: Optional[Mapping[str, MarketDataState]] = None,
             candidates_considered: int = 0,
             session_snapshot_id: Optional[str] = None) -> AttentionPlan:
        """
        One cycle's COLD/WARM/HOT assignment, with reasons for each.

        Order of operations, and why:

        1.  coalesce -- four requesters for one instrument is ONE
            activation, decided before anything is priced, so the merge
            cannot be defeated by pricing each copy separately;
        2.  sort by priority, then by instrument id for determinism --
            an open position's exit is served before a screening scan;
        3.  fill HOT up to the ceiling, then WARM;
        4.  price each escalation and honour the governor's refusal.

        Cost is evaluated per escalation rather than for the batch so a
        refusal names the instrument that could not be afforded, rather
        than failing the whole cycle.
        """
        from src.adaptive.request_ledger import coalesce

        merged = coalesce(requests)
        current = dict(current_states or {})
        tally = usage if usage is not None else BillingUsage(
            policy=self.governor.policy)

        ordered = sorted(
            merged, key=lambda item: (item.priority.rank, item.instrument_id))

        decisions: List[AttentionDecision] = []
        refused: List[Tuple[str, str]] = []
        notes: List[str] = []
        hot_granted = 0
        warm_granted = 0

        for item in ordered:
            instrument_id = item.instrument_id
            exchange = self.exchange_map.exchange_of(instrument_id)
            previous = current.get(instrument_id, MarketDataState.COLD)
            wanted: MarketDataState = item.state
            granted = wanted
            downgraded_from: Optional[MarketDataState] = None
            downgrade_reason = ""

            # --- an unattributable venue can never be paid for ---
            if (wanted is not MarketDataState.COLD
                    and exchange == UNKNOWN_EXCHANGE
                    and not self.governor.conservative_unknown_pricing):
                granted = MarketDataState.COLD
                downgraded_from, downgrade_reason = wanted, (
                    f"{instrument_id} has no known billing exchange; a request "
                    "that cannot be attributed is not made")
                refused.append((instrument_id, downgrade_reason))

            # --- HOT capacity is a ceiling ---
            if (granted is MarketDataState.HOT
                    and self.config.max_hot_symbols is not None
                    and hot_granted >= self.config.max_hot_symbols):
                granted = MarketDataState.WARM
                downgraded_from, downgrade_reason = MarketDataState.HOT, (
                    f"HOT capacity of {self.config.max_hot_symbols} is already "
                    f"used by higher-priority instruments")
                refused.append((instrument_id, downgrade_reason))

            if (granted is MarketDataState.WARM
                    and self.config.max_warm_symbols is not None
                    and warm_granted >= self.config.max_warm_symbols):
                granted = MarketDataState.COLD
                downgraded_from = downgraded_from or MarketDataState.WARM
                downgrade_reason = (
                    f"WARM capacity of {self.config.max_warm_symbols} is full")
                refused.append((instrument_id, downgrade_reason))

            # --- cost ---
            cost: Optional[float] = None
            if granted is not MarketDataState.COLD:
                plan_for_one = BillableRequestPlan(
                    requests_by_exchange={exchange: 1},
                    priority=item.priority,
                    purpose=f"{granted.value} for {instrument_id}",
                    instrument_count=1)
                verdict = self.governor.assess(plan_for_one, tally, period)
                cost = verdict.incremental_cost_usd
                if not verdict.permits_spending:
                    if verdict.safety_conflict:
                        # Never silently downgrade an exit to delayed
                        # data. Surface it and leave the desired state
                        # standing so the conflict is visible.
                        notes.append(
                            f"{verdict.conflict_code}: {instrument_id} "
                            f"({item.priority.value}) -- {verdict.reason}")
                        refused.append((instrument_id, verdict.reason))
                    elif (verdict.policy is BudgetPolicy.THROTTLE
                          and granted is MarketDataState.WARM):
                        granted = MarketDataState.COLD
                        downgraded_from = downgraded_from or MarketDataState.WARM
                        downgrade_reason = (
                            f"throttled: {verdict.reason}")
                        refused.append((instrument_id, verdict.reason))
                    elif self.config.downgrade_on_budget_refusal:
                        step_down = (MarketDataState.WARM
                                     if granted is MarketDataState.HOT
                                     else MarketDataState.COLD)
                        downgraded_from = downgraded_from or granted
                        granted = step_down
                        downgrade_reason = f"budget: {verdict.reason}"
                        refused.append((instrument_id, verdict.reason))
                    else:
                        downgraded_from = downgraded_from or granted
                        granted = MarketDataState.COLD
                        downgrade_reason = f"budget: {verdict.reason}"
                        refused.append((instrument_id, verdict.reason))

            if granted is MarketDataState.HOT:
                hot_granted += 1
            elif granted is MarketDataState.WARM:
                warm_granted += 1

            ttl = (self.config.hot_ttl_seconds
                   if granted is MarketDataState.HOT
                   else self.config.warm_ttl_seconds
                   if granted is MarketDataState.WARM else None)
            decisions.append(AttentionDecision(
                instrument_id=instrument_id, exchange=exchange,
                granted_state=granted, previous_state=previous,
                priority=item.priority, requesters=item.requesters,
                reasons=item.reasons,
                activation_reasons=tuple(item.activation_reasons),
                downgraded_from=downgraded_from,
                downgrade_reason=downgrade_reason,
                estimated_cost_usd=cost, as_of=as_of,
                expires_at=(as_of + timedelta(seconds=ttl)) if ttl else None,
                lineage_id=item.lineage_id))

        return AttentionPlan(
            decisions=tuple(decisions),
            max_hot_symbols=self.config.max_hot_symbols,
            candidates_considered=candidates_considered or len(merged),
            requests_received=len(requests),
            escalations_refused=tuple(refused), as_of=as_of,
            session_snapshot_id=session_snapshot_id, notes=tuple(notes))

    # ---------------- deactivation (spec section 31) ----------------

    def deactivate(self, current_states: Mapping[str, MarketDataState],
                   triggers: Mapping[str, RealtimeDeactivationReason], *,
                   as_of: datetime,
                   to_state: MarketDataState = MarketDataState.COLD
                   ) -> Tuple[MarketDataStateTransition, ...]:
        """
        Step instruments back down, each with a recorded reason.

        HOT must not remain HOT because nobody remembered to turn it
        off. A trigger for an instrument already at or below the target
        state produces no transition rather than an illegal one.
        """
        transitions: List[MarketDataStateTransition] = []
        for instrument_id in sorted(triggers):
            previous = current_states.get(instrument_id, MarketDataState.COLD)
            if STATE_RANK[previous] <= STATE_RANK[to_state]:
                continue
            transitions.append(MarketDataStateTransition(
                instrument_id=instrument_id, previous_state=previous,
                new_state=to_state, at=as_of,
                deactivation_reason=triggers[instrument_id]))
        return tuple(transitions)

    def expired(self, plan: AttentionPlan, *, now: datetime
                ) -> Tuple[str, ...]:
        """Instruments whose granted state has outlived its TTL."""
        return tuple(sorted(
            decision.instrument_id for decision in plan.decisions
            if decision.expires_at is not None and decision.expires_at <= now
            and decision.granted_state is not MarketDataState.COLD))

    # ---------------- transitions ----------------

    def transitions_for(self, plan: AttentionPlan
                        ) -> Tuple[MarketDataStateTransition, ...]:
        """
        The state moves this plan implies, each carrying its reason.

        An escalation without a `RealtimeActivationReason` is refused by
        the contract itself, so a HOT grant whose reasons were lost
        cannot be recorded at all.
        """
        transitions: List[MarketDataStateTransition] = []
        for decision in plan.decisions:
            if decision.granted_state is decision.previous_state:
                continue
            escalating = (STATE_RANK[decision.granted_state]
                          > STATE_RANK[decision.previous_state])
            activation: Optional[RealtimeActivationReason] = None
            deactivation: Optional[RealtimeDeactivationReason] = None
            if escalating:
                activation = (decision.activation_reasons[0]
                              if decision.activation_reasons
                              else RealtimeActivationReason.ENTRY_CANDIDATE)
            else:
                deactivation = (RealtimeDeactivationReason.COST_NOT_JUSTIFIED
                                if decision.was_downgraded
                                else RealtimeDeactivationReason.DECISION_COMPLETE)
            transitions.append(MarketDataStateTransition(
                instrument_id=decision.instrument_id,
                previous_state=decision.previous_state,
                new_state=decision.granted_state,
                at=decision.as_of or plan.as_of,
                activation_reason=activation, deactivation_reason=deactivation,
                note="; ".join(decision.reasons)[:500],
                lineage_id=decision.lineage_id))
        return tuple(transitions)
