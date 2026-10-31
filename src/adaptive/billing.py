"""
src/adaptive/billing.py
-------------------------------------
Exchange-aware, capped market-data billing (Phase 25.9I-C).

WHAT CHANGED, AND WHAT DID NOT
----------------------------------
Phase 25.9I-A's `MarketDataCostGovernor` prices uncapped plans against
a monthly budget and is UNCHANGED: it still governs any provider whose
bill is fixed + per-request. This module adds the shape that governor
cannot express -- a per-venue monthly ceiling -- and the two compose
rather than compete (`MarketDataBillingGovernor.assess` produces the
same `BudgetPolicy` vocabulary).

THE EVIDENCE BEHIND THE NUMBERS
-----------------------------------
`docs/evidence/IBKR_MARKET_DATA_BILLING_561227.md`, from IBKR Client
Services on 2026-09-29:

    USD 0.01 per snapshot request
    150 requests per exchange per calendar month
    USD 1.50 maximum snapshot fee per exchange per month
    realtime streaming stated to follow, free, for the rest of the month

That is `PROVIDER_STATED`, and this module will not let it become
`ACCOUNT_VERIFIED` without an observation. The supersession it performs
is narrow and explicit: the old "~USD 2,500/month uncapped" figure was
an UNCAPPED model, and item 2 above establishes a ceiling. It does not
establish that the ceiling applies to this account.

WHY THE COVERED EXCHANGES ARE A LIST AND NOT A DEFAULT
----------------------------------------------------------
IBKR stated the rule for "a given exchange", not "every exchange", and
named no list. `policy_for` therefore has no fallback: a venue nobody
configured is unpriceable, and a plan touching it is BLOCKed. A fourth
exchange still gets its own counter automatically -- the ledger creates
buckets from whatever arrives -- but it does not get a PRICE
automatically, because nobody has evidence for one.

A caller that must proceed anyway may ask for
`conservative_unknown_pricing=True`, which prices an unknown venue at
the dearest known per-request rate WITH NO CAP. That is deliberately
the most expensive assumption available: an unknown cost is never the
cheap answer (spec section 21).

THE COUNTING BASIS IS STILL UNKNOWN
---------------------------------------
`transport.market_snapshot` batches the whole universe into one HTTP
call. Whether IBKR's counter sees 1 request or 31 was not answered, and
it moves worst-case exposure by 31x. `RequestCountingBasis.UNKNOWN` is
the configured value and `project_exposure` reports BOTH readings, so
no plan is approved on the strength of the favourable one.

NOTHING HERE SENDS A REQUEST OR BUYS A SUBSCRIPTION
-------------------------------------------------------
No transport, no connection, no clock. Every input is an argument.
Subscription purchase is a human governance action and has no code
path here at all (spec section 18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.adaptive.exchange_map import UNKNOWN_EXCHANGE
from src.domain.adaptive_models import BudgetPolicy, MarketDataState
from src.domain.data_attention_models import AttentionPriority
from src.domain.market_data_billing_models import (
    BillingPeriod, CapState, EntitlementStatus, ExchangeBillingPolicy,
    ExchangeBillingState, PricingConfidence, ProviderBillingPolicy,
    RequestCountingBasis,
)

# ======================================================================
# The provider policy, with its evidence attached
# ======================================================================

EVIDENCE_SOURCE = "docs/evidence/IBKR_MARKET_DATA_BILLING_561227.md"
EVIDENCE_DATE = "2026-09-29"

IBKR_SNAPSHOT_COST_PER_REQUEST_USD = 0.01
IBKR_SNAPSHOT_REQUEST_THRESHOLD = 150
IBKR_SNAPSHOT_MAX_COST_PER_EXCHANGE_USD = 1.50

#: The venues this repository has actually resolved contracts for, read
#: from `broker_instrument_mapping` on 2026-09-29: NASDAQ 13, NYSE 17,
#: ARCA 1. NOT a claim that IBKR covers only these three -- it is the
#: set for which we know a request could currently be generated. A
#: fourth venue is added here, with evidence, never inferred.
IBKR_COVERED_EXCHANGES: Tuple[str, ...] = ("NASDAQ", "NYSE", "ARCA")

#: The user's ceiling for all market data, restated rather than
#: imported so this module has no hidden dependency on the 25.9I-A
#: constant. Configurable; never silently raised.
DEFAULT_MONTHLY_BUDGET_USD = 20.0

DEFAULT_WARN_FRACTION = 0.70
DEFAULT_THROTTLE_FRACTION = 0.90


def ibkr_snapshot_policy(
        exchanges: Sequence[str] = IBKR_COVERED_EXCHANGES,
        *, fixed_monthly_subscription_cost_usd: Optional[float] = None,
) -> ProviderBillingPolicy:
    """
    The #561227 terms, applied to each named venue.

    `fixed_monthly_subscription_cost_usd` defaults to None -- UNKNOWN,
    not zero. No bundle is subscribed, and the reply did not quote a
    price, so carrying one over from an unverified earlier note would
    be inventing evidence. A caller comparing plans supplies it.
    """
    policies = {
        str(name).strip().upper(): ExchangeBillingPolicy(
            exchange=name,
            cost_per_request_usd=IBKR_SNAPSHOT_COST_PER_REQUEST_USD,
            request_threshold=IBKR_SNAPSHOT_REQUEST_THRESHOLD,
            max_variable_cost_usd=IBKR_SNAPSHOT_MAX_COST_PER_EXCHANGE_USD,
            entitlement_expected_after_cap=MarketDataState.HOT,
            note="ticket #561227; streaming stated to follow the cap, unverified")
        for name in exchanges}
    return ProviderBillingPolicy(
        provider="IBKR",
        exchange_policies=policies,
        fixed_monthly_subscription_cost_usd=fixed_monthly_subscription_cost_usd,
        # Not PER_HTTP_CALL and not PER_INSTRUMENT: the reply said
        # "per request" and the transport batches. See section 4 of the
        # evidence document.
        counting_basis=RequestCountingBasis.UNKNOWN,
        confidence=PricingConfidence.PROVIDER_STATED,
        evidence_source=EVIDENCE_SOURCE,
        evidence_date=EVIDENCE_DATE,
        policy_version="561227.1",
        billing_timezone="UTC",
        billing_timezone_assumed=True)


#: The configured default. Built by the factory so a test can build a
#: different one without mutating this.
IBKR_SNAPSHOT_POLICY_561227 = ibkr_snapshot_policy()


# ======================================================================
# Usage across exchanges
# ======================================================================


@dataclass
class BillingUsage:
    """
    Request counts per (period, exchange), and the cost that implies.

    Mutable by design -- it is a tally that a ledger replays into -- but
    it produces frozen `ExchangeBillingState` values for anyone asking a
    question, so no caller can adjust a count by accident.

    Past periods are KEPT. September's usage stays readable after
    October starts (spec section 10); nothing here deletes history.
    """
    policy: ProviderBillingPolicy
    counts: Dict[Tuple[str, str], int] = field(default_factory=dict)
    first_seen: Dict[Tuple[str, str], datetime] = field(default_factory=dict)
    last_seen: Dict[Tuple[str, str], datetime] = field(default_factory=dict)

    # ---------------- accumulation ----------------

    def record(self, exchange: str, period: BillingPeriod, count: int = 1,
               at: Optional[datetime] = None) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        key = (period.key, str(exchange).strip().upper())
        self.counts[key] = self.counts.get(key, 0) + int(count)
        if at is not None:
            self.first_seen.setdefault(key, at)
            self.last_seen[key] = at

    # ---------------- questions ----------------

    def count_for(self, exchange: str, period: BillingPeriod) -> int:
        return self.counts.get((period.key, str(exchange).strip().upper()), 0)

    def state_for(self, exchange: str, period: BillingPeriod
                  ) -> ExchangeBillingState:
        name = str(exchange).strip().upper()
        key = (period.key, name)
        return ExchangeBillingState(
            exchange=name, period=period, request_count=self.counts.get(key, 0),
            policy=self.policy.policy_for(name),
            first_request_at=self.first_seen.get(key),
            last_request_at=self.last_seen.get(key))

    def exchanges_in(self, period: BillingPeriod) -> Tuple[str, ...]:
        return tuple(sorted(exchange for (month, exchange) in self.counts
                            if month == period.key))

    def periods(self) -> Tuple[str, ...]:
        return tuple(sorted({month for (month, _) in self.counts}))

    def states_in(self, period: BillingPeriod) -> Tuple[ExchangeBillingState, ...]:
        return tuple(self.state_for(name, period)
                     for name in self.exchanges_in(period))

    def total_variable_cost_usd(self, period: BillingPeriod
                                ) -> Tuple[Optional[float], Tuple[str, ...]]:
        """
        (total, unpriceable venues). None total when anything is unpriceable.

        A partial sum presented as a total is the failure mode this
        return shape exists to prevent.
        """
        total, unpriceable = 0.0, []
        for state in self.states_in(period):
            cost = state.estimated_cost_usd
            if cost is None:
                unpriceable.append(state.exchange)
                continue
            total += cost
        if unpriceable:
            return None, tuple(sorted(unpriceable))
        return round(total, 6), ()


# ======================================================================
# Plans and verdicts
# ======================================================================


@dataclass(frozen=True)
class BillableRequestPlan:
    """
    A proposed set of billable requests, grouped by venue.

    `requests_by_exchange` is already resolved to venues by
    `ExchangeMap`, so this module never has to guess where a request
    belongs -- it either arrived with a venue or arrived as UNKNOWN.
    """
    requests_by_exchange: Dict[str, int] = field(default_factory=dict)
    priority: AttentionPriority = AttentionPriority.BROAD_SCREENING
    purpose: str = ""
    instrument_count: int = 0

    def __post_init__(self):
        cleaned: Dict[str, int] = {}
        for name, count in dict(self.requests_by_exchange).items():
            if count < 0:
                raise ValueError("request counts must be >= 0")
            cleaned[str(name).strip().upper() or UNKNOWN_EXCHANGE] = int(count)
        object.__setattr__(self, "requests_by_exchange", cleaned)

    @property
    def total_requests(self) -> int:
        return sum(self.requests_by_exchange.values())

    @property
    def unknown_requests(self) -> int:
        return self.requests_by_exchange.get(UNKNOWN_EXCHANGE, 0)


@dataclass(frozen=True)
class BillingVerdict:
    """
    ALLOW / WARN / THROTTLE / BLOCK, with the arithmetic that produced it.

    `safety_conflict` is the spec section 20 escape hatch: when a plan
    that an OPEN POSITION needs for an exit is refused on cost, the
    answer is not "use delayed data". It is to surface the conflict for
    Portfolio, Risk or a human to resolve. The verdict still says BLOCK
    -- nothing is quietly permitted -- but it says WHY loudly.
    """
    policy: BudgetPolicy
    incremental_cost_usd: Optional[float] = None
    projected_period_cost_usd: Optional[float] = None
    budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD
    reason: str = ""
    per_exchange: Tuple[ExchangeBillingState, ...] = ()
    unpriceable_exchanges: Tuple[str, ...] = ()
    safety_conflict: bool = False
    conflict_code: str = ""

    @property
    def permits_spending(self) -> bool:
        return self.policy in (BudgetPolicy.ALLOW, BudgetPolicy.WARN)

    def describe(self) -> Dict[str, object]:
        return {
            "policy": self.policy.value,
            "incremental_cost_usd": self.incremental_cost_usd,
            "projected_period_cost_usd": self.projected_period_cost_usd,
            "budget_usd": self.budget_usd,
            "reason": self.reason,
            "unpriceable_exchanges": list(self.unpriceable_exchanges),
            "safety_conflict": self.safety_conflict,
            "conflict_code": self.conflict_code,
            "per_exchange": [s.describe() for s in self.per_exchange],
        }


#: Codes a caller must escalate rather than absorb (spec section 20).
SAFETY_DATA_REQUIRED = "SAFETY_DATA_REQUIRED"
BUDGET_CONFLICT = "BUDGET_CONFLICT"


# ======================================================================
# Exposure projection (spec section 26)
# ======================================================================


@dataclass(frozen=True)
class ExposureProjection:
    """
    What a bounded test would cost, under both counting bases.

    Every number is computed from the arguments; nothing is copied from
    a prior manual estimate. Where the two bases disagree, BOTH are
    reported, and `worst_case_uncapped_usd` uses the dearer one.
    """
    minutes: float
    cycles: int
    instruments_by_exchange: Dict[str, int] = field(default_factory=dict)
    requests_by_exchange_per_instrument: Dict[str, int] = field(default_factory=dict)
    requests_by_exchange_per_call: Dict[str, int] = field(default_factory=dict)
    capped_cost_usd: Optional[float] = None
    uncapped_cost_per_instrument_usd: Optional[float] = None
    uncapped_cost_per_call_usd: Optional[float] = None
    minutes_until_cap: Dict[str, Optional[float]] = field(default_factory=dict)
    requests_until_cap: Dict[str, Optional[int]] = field(default_factory=dict)
    unpriceable_exchanges: Tuple[str, ...] = ()
    counting_basis: RequestCountingBasis = RequestCountingBasis.UNKNOWN

    @property
    def worst_case_uncapped_usd(self) -> Optional[float]:
        """The dearer of the two readings, because the basis is unknown."""
        values = [v for v in (self.uncapped_cost_per_instrument_usd,
                              self.uncapped_cost_per_call_usd) if v is not None]
        return max(values) if values else None

    def describe(self) -> Dict[str, object]:
        return {
            "minutes": self.minutes,
            "cycles": self.cycles,
            "instruments_by_exchange": dict(self.instruments_by_exchange),
            "counting_basis": self.counting_basis.value,
            "requests_per_instrument_basis": dict(
                self.requests_by_exchange_per_instrument),
            "requests_per_call_basis": dict(self.requests_by_exchange_per_call),
            "capped_cost_usd": self.capped_cost_usd,
            "uncapped_cost_usd_per_instrument_basis":
                self.uncapped_cost_per_instrument_usd,
            "uncapped_cost_usd_per_call_basis": self.uncapped_cost_per_call_usd,
            "worst_case_uncapped_usd": self.worst_case_uncapped_usd,
            "minutes_until_cap": dict(self.minutes_until_cap),
            "requests_until_cap": dict(self.requests_until_cap),
            "unpriceable_exchanges": list(self.unpriceable_exchanges),
        }


@dataclass(frozen=True)
class PlanComparison:
    """
    Capped snapshots vs a fixed subscription. Reported, never chosen.

    Purchasing is a human governance action (spec section 18), so this
    type has no `recommended` field and no method that returns one.
    """
    capped_snapshot_cost_usd: Optional[float]
    subscription_cost_usd: Optional[float]
    budget_usd: float
    exchanges: Tuple[str, ...] = ()
    unknowns: Tuple[str, ...] = ()
    account_verified: bool = False

    def describe(self) -> Dict[str, object]:
        def fits(value: Optional[float]) -> Optional[bool]:
            return None if value is None else value <= self.budget_usd
        return {
            "plan_a_capped_snapshots": {
                "monthly_cost_usd": self.capped_snapshot_cost_usd,
                "exchanges": list(self.exchanges),
                "fits_budget": fits(self.capped_snapshot_cost_usd),
                "account_verified": self.account_verified,
            },
            "plan_b_fixed_subscription": {
                "monthly_cost_usd": self.subscription_cost_usd,
                "fits_budget": fits(self.subscription_cost_usd),
                "account_verified": False,
            },
            "budget_usd": self.budget_usd,
            "unknowns": list(self.unknowns),
            "note": "reported for human governance; no purchase path exists",
        }


# ======================================================================
# The governor
# ======================================================================


class MarketDataBillingGovernor:
    """
    Prices capped, per-exchange plans against a monthly budget.

    Deterministic and stateless with respect to time: no clock, no
    connection, no transport. The same arguments always give the same
    verdict.
    """

    def __init__(self, policy: Optional[ProviderBillingPolicy] = None, *,
                 monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD,
                 warn_fraction: float = DEFAULT_WARN_FRACTION,
                 throttle_fraction: float = DEFAULT_THROTTLE_FRACTION,
                 conservative_unknown_pricing: bool = False):
        if monthly_budget_usd < 0:
            raise ValueError("monthly_budget_usd must be >= 0")
        if not 0.0 < warn_fraction <= throttle_fraction <= 1.0:
            raise ValueError(
                "require 0 < warn_fraction <= throttle_fraction <= 1")
        self.policy = policy or IBKR_SNAPSHOT_POLICY_561227
        self.monthly_budget_usd = float(monthly_budget_usd)
        self.warn_fraction = warn_fraction
        self.throttle_fraction = throttle_fraction
        self.conservative_unknown_pricing = bool(conservative_unknown_pricing)

    # ---------------- pricing one venue ----------------

    def _policy_for(self, exchange: str) -> Optional[ExchangeBillingPolicy]:
        """
        The venue's policy, or the conservative stand-in if allowed.

        The stand-in is the dearest known per-request price WITH NO CAP.
        It exists so an operator who must proceed can, while paying the
        most pessimistic arithmetic available for the privilege.
        """
        found = self.policy.policy_for(exchange)
        if found is not None or not self.conservative_unknown_pricing:
            return found
        prices = [p.cost_per_request_usd
                  for p in self.policy.exchange_policies.values()
                  if p.cost_per_request_usd is not None]
        if not prices:
            return None
        return ExchangeBillingPolicy(
            exchange=exchange or UNKNOWN_EXCHANGE,
            cost_per_request_usd=max(prices),
            note="conservative stand-in: dearest known rate, NO cap assumed")

    def incremental_cost(self, plan: BillableRequestPlan, usage: BillingUsage,
                         period: BillingPeriod
                         ) -> Tuple[Optional[float], Tuple[str, ...],
                                    Tuple[ExchangeBillingState, ...]]:
        """
        What this plan ADDS this period, given what is already spent.

        The incremental cost of a request on a capped-out venue is
        genuinely 0.00, and that is the point of the cap. It is computed
        as (cost after) - (cost before) rather than asserted, so the
        arithmetic holds at the boundary too.
        """
        total, unpriceable, states = 0.0, [], []
        for exchange, count in sorted(plan.requests_by_exchange.items()):
            venue_policy = self._policy_for(exchange)
            already = usage.count_for(exchange, period)
            state = ExchangeBillingState(
                exchange=exchange, period=period, request_count=already,
                policy=venue_policy)
            states.append(state)
            if venue_policy is None or not venue_policy.is_priceable:
                unpriceable.append(exchange)
                continue
            before = venue_policy.cost_for(already) or 0.0
            after = venue_policy.cost_for(already + count) or 0.0
            total += max(0.0, after - before)
        if unpriceable:
            return None, tuple(sorted(set(unpriceable))), tuple(states)
        return round(total, 6), (), tuple(states)

    # ---------------- judging ----------------

    def assess(self, plan: BillableRequestPlan, usage: BillingUsage,
               period: BillingPeriod, *,
               other_committed_usd: float = 0.0) -> BillingVerdict:
        """
        ALLOW / WARN / THROTTLE / BLOCK for one plan.

        BLOCK for anything unpriceable, and BLOCK for anything that
        would breach the budget. When the refused plan serves an open
        position's safety, the verdict carries `safety_conflict` so the
        caller escalates instead of falling back to delayed data.
        """
        incremental, unpriceable, states = self.incremental_cost(
            plan, usage, period)

        if incremental is None:
            return self._blocked(
                states, unpriceable, plan,
                reason=("cannot price requests for "
                        + ", ".join(unpriceable)
                        + "; an unpriced venue is not a free one"),
                conflict_code=SAFETY_DATA_REQUIRED)

        if self.monthly_budget_usd <= 0:
            return self._blocked(
                states, (), plan, incremental=incremental,
                reason="no market-data budget is configured",
                conflict_code=BUDGET_CONFLICT)

        spent, spent_unpriceable = usage.total_variable_cost_usd(period)
        if spent is None:
            return self._blocked(
                states, spent_unpriceable, plan, incremental=incremental,
                reason=("this period already contains requests on unpriced "
                        "venues: " + ", ".join(spent_unpriceable)),
                conflict_code=SAFETY_DATA_REQUIRED)

        fixed = self.policy.fixed_monthly_subscription_cost_usd or 0.0
        projected = round(spent + fixed + float(other_committed_usd)
                          + incremental, 6)

        if projected > self.monthly_budget_usd:
            return self._blocked(
                states, (), plan, incremental=incremental, projected=projected,
                reason=(f"plan would reach {projected} USD against a budget of "
                        f"{self.monthly_budget_usd} USD"),
                conflict_code=BUDGET_CONFLICT)

        used = projected / self.monthly_budget_usd
        if used >= self.throttle_fraction:
            verdict_policy = BudgetPolicy.THROTTLE
            reason = f"{used:.0%} of the monthly budget would be committed"
        elif used >= self.warn_fraction:
            verdict_policy = BudgetPolicy.WARN
            reason = f"{used:.0%} of the monthly budget would be committed"
        else:
            verdict_policy, reason = BudgetPolicy.ALLOW, ""
        return BillingVerdict(
            policy=verdict_policy, incremental_cost_usd=incremental,
            projected_period_cost_usd=projected,
            budget_usd=self.monthly_budget_usd, reason=reason,
            per_exchange=states)

    def _blocked(self, states, unpriceable, plan: BillableRequestPlan, *,
                 incremental: Optional[float] = None,
                 projected: Optional[float] = None,
                 reason: str = "", conflict_code: str = "") -> BillingVerdict:
        """A refusal, escalated when an open position needed it."""
        safety = plan.priority.is_position_safety
        if safety:
            reason = (f"{reason}. This plan serves {plan.priority.value}; "
                      "delayed data is NOT an acceptable substitute -- "
                      "escalate to Portfolio/Risk/human governance")
        return BillingVerdict(
            policy=BudgetPolicy.BLOCK, incremental_cost_usd=incremental,
            projected_period_cost_usd=projected,
            budget_usd=self.monthly_budget_usd, reason=reason,
            per_exchange=tuple(states),
            unpriceable_exchanges=tuple(unpriceable),
            safety_conflict=safety,
            conflict_code=conflict_code if safety else "")

    # ---------------- forecasting (spec section 44) ----------------

    def forecast_month(self, exchanges: Sequence[str]) -> Dict[str, object]:
        """
        Worst-case monthly cost if every named venue reached its cap.

        Computed from the venues passed in -- which a caller derives
        from the CURRENT universe -- so a dynamic universe that adds a
        venue changes the forecast without any constant being edited.
        """
        names = tuple(sorted({str(e).strip().upper() for e in exchanges}))
        ceiling, unbounded = self.policy.maximum_known_variable_cost_usd(names)
        fixed = self.policy.fixed_monthly_subscription_cost_usd
        total: Optional[float] = None
        if ceiling is not None and fixed is not None:
            total = round(ceiling + fixed, 6)
        elif ceiling is not None and fixed is None:
            # No subscription is configured and none is purchased, so
            # the variable ceiling IS the total under the current plan.
            total = ceiling
        return {
            "exchanges": list(names),
            "exchange_count": len(names),
            "variable_ceiling_usd": ceiling,
            "unbounded_exchanges": list(unbounded),
            "fixed_subscription_usd": fixed,
            "total_worst_case_usd": total,
            "budget_usd": self.monthly_budget_usd,
            "fits_budget": (None if total is None
                            else total <= self.monthly_budget_usd),
            "confidence": self.policy.confidence.value,
            "evidence_source": self.policy.evidence_source,
            "evidence_date": self.policy.evidence_date,
        }

    # ---------------- exposure (spec section 26) ----------------

    def project_exposure(self, instruments_by_exchange: Mapping[str, int], *,
                         minutes: float, interval_seconds: float = 60.0,
                         calls_per_cycle: int = 1) -> ExposureProjection:
        """
        What a bounded test would cost, under both counting bases.

        `instruments_by_exchange` comes from `ExchangeMap.distribution`,
        so the arithmetic follows the real universe rather than any
        number written in a spec or a prior message.
        """
        if minutes < 0 or interval_seconds <= 0 or calls_per_cycle < 0:
            raise ValueError("minutes >= 0, interval_seconds > 0, calls >= 0")
        cycles = int(minutes * 60.0 // interval_seconds)

        per_instrument = {str(e).strip().upper(): cycles * int(n)
                          for e, n in instruments_by_exchange.items()}
        # One batched call touches every venue present in the batch, so
        # under the per-call basis each venue accrues the CALL count,
        # not a share of it.
        per_call = {str(e).strip().upper(): cycles * calls_per_cycle
                    for e, n in instruments_by_exchange.items() if int(n) > 0}

        capped, uncapped_inst, uncapped_call = 0.0, 0.0, 0.0
        unpriceable: List[str] = []
        minutes_to_cap: Dict[str, Optional[float]] = {}
        requests_to_cap: Dict[str, Optional[int]] = {}

        for exchange, instrument_count in sorted(instruments_by_exchange.items()):
            name = str(exchange).strip().upper()
            venue_policy = self._policy_for(name)
            if venue_policy is None or not venue_policy.is_priceable:
                unpriceable.append(name)
                minutes_to_cap[name] = None
                requests_to_cap[name] = None
                continue
            price = float(venue_policy.cost_per_request_usd or 0.0)
            capped += venue_policy.cost_for(per_instrument.get(name, 0)) or 0.0
            uncapped_inst += per_instrument.get(name, 0) * price
            uncapped_call += per_call.get(name, 0) * price
            threshold = venue_policy.request_threshold
            requests_to_cap[name] = threshold
            if threshold is None or int(instrument_count) <= 0:
                minutes_to_cap[name] = None
            else:
                # Conservative basis: the universe bills per instrument,
                # so the cap arrives soonest. Reported as the fast case.
                cycles_needed = threshold / float(instrument_count)
                minutes_to_cap[name] = round(
                    cycles_needed * interval_seconds / 60.0, 2)

        return ExposureProjection(
            minutes=float(minutes), cycles=cycles,
            instruments_by_exchange={str(e).strip().upper(): int(n)
                                     for e, n in instruments_by_exchange.items()},
            requests_by_exchange_per_instrument=per_instrument,
            requests_by_exchange_per_call=per_call,
            capped_cost_usd=(None if unpriceable else round(capped, 6)),
            uncapped_cost_per_instrument_usd=(
                None if unpriceable else round(uncapped_inst, 6)),
            uncapped_cost_per_call_usd=(
                None if unpriceable else round(uncapped_call, 6)),
            minutes_until_cap=minutes_to_cap,
            requests_until_cap=requests_to_cap,
            unpriceable_exchanges=tuple(sorted(set(unpriceable))),
            counting_basis=self.policy.counting_basis)

    # ---------------- comparison (spec section 45) ----------------

    def compare_plans(self, exchanges: Sequence[str], *,
                      subscription_cost_usd: Optional[float] = None,
                      account_verified: bool = False) -> PlanComparison:
        """
        Plan A (capped snapshots) vs Plan B (a subscription). No advice.

        `subscription_cost_usd` defaults to None and is reported as
        UNKNOWN rather than filled from a stale figure; the #561227
        reply quoted no price.
        """
        names = tuple(sorted({str(e).strip().upper() for e in exchanges}))
        ceiling, unbounded = self.policy.maximum_known_variable_cost_usd(names)
        unknowns: List[str] = []
        if ceiling is None:
            unknowns.append("capped snapshot cost: unbounded or unpriced venues "
                            + ", ".join(unbounded))
        if subscription_cost_usd is None:
            unknowns.append("subscription price: not verified, not configured")
        if not account_verified:
            unknowns.append("cap behaviour on this account: never observed")
        return PlanComparison(
            capped_snapshot_cost_usd=ceiling,
            subscription_cost_usd=subscription_cost_usd,
            budget_usd=self.monthly_budget_usd, exchanges=names,
            unknowns=tuple(unknowns), account_verified=account_verified)

    # ---------------- diagnostics ----------------

    def describe(self) -> Dict[str, object]:
        """Prices and policy only. No account, no credential, no token."""
        return {
            "provider": self.policy.provider,
            "policy_version": self.policy.policy_version,
            "confidence": self.policy.confidence.value,
            "account_verified": (self.policy.confidence
                                 is PricingConfidence.ACCOUNT_VERIFIED),
            "evidence_source": self.policy.evidence_source,
            "evidence_date": self.policy.evidence_date,
            "counting_basis": self.policy.counting_basis.value,
            "billing_timezone": self.policy.billing_timezone,
            "billing_timezone_assumed": self.policy.billing_timezone_assumed,
            "known_exchanges": list(self.policy.known_exchanges),
            "monthly_budget_usd": self.monthly_budget_usd,
            "fixed_subscription_usd":
                self.policy.fixed_monthly_subscription_cost_usd,
            "conservative_unknown_pricing": self.conservative_unknown_pricing,
            "warn_fraction": self.warn_fraction,
            "throttle_fraction": self.throttle_fraction,
            "active": False,   # governed only when the flags are enabled
        }


def entitlement_expected_after_cap(policy: ProviderBillingPolicy,
                                   exchange: str) -> Optional[EntitlementStatus]:
    """
    What the PROVIDER said to expect once a venue caps out.

    An expectation, never an observation. The return value is the reason
    to go and LOOK, and `src/adaptive/entitlement.py` is what looks.
    """
    venue = policy.policy_for(exchange)
    if venue is None or venue.entitlement_expected_after_cap is None:
        return None
    if venue.entitlement_expected_after_cap is MarketDataState.HOT:
        return EntitlementStatus.REALTIME_VERIFIED
    return None
