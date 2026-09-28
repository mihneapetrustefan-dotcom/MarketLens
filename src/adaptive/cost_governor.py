"""
src/adaptive/cost_governor.py
------------------------------------------
Market-data cost accounting (Phase 25.9I-A). Inactive; contract only.

WHY THIS IS PROVIDER-AGNOSTIC, AND WHY THAT IS NOT PEDANTRY
---------------------------------------------------------------
On 2026-09-24 IBKR's own assistant answered the same billing question
two ways within an hour: first that ~250,000 monthly snapshot requests
were covered by the subscription, then that each one is charged at USD
0.01. Their subscription page carries the second reading in its fine
print, and a human support ticket (#561227) was still open when this
module was written.

The uncertainty is not about WHICH subscription. It is about whether a
VARIABLE cost sits on top of a fixed one:

    fixed    ~USD 14.50/month   two bundles, flat, volume-independent
    variable ~USD 2,500/month   IF each API snapshot bills at USD 0.01,
                                at ~250,000 requests a month

That is why `MarketDataCostModel` splits the two rather than carrying a
single price: `fixed_monthly_subscription_cost_usd` is knowable today,
while `per_snapshot_cost_usd` stays None until someone confirms it.

So no price is hard-coded here, and there is no default that quietly
assumes the cheap answer. `MarketDataCostModel` is configuration, its
unset state is UNKNOWN rather than free, and an unknown price makes the
governor refuse rather than approve. A cost model that guesses low is
indistinguishable from one that is right, until the statement arrives.

WHAT THE GOVERNOR IS FOR
----------------------------
"No resource should be consumed merely because it exists" (§3.J). The
governor turns that into an arithmetic question a later phase can ask
before escalating an instrument to HOT: what would this cost, what is
left this month, and is the answer ALLOW, WARN, THROTTLE or BLOCK.

A second principle it will eventually serve: a position should not be
opened if the system cannot afford the data needed to monitor and exit
it safely. That policy is NOT active in this phase; the accounting it
would need is what this module provides.

NOTHING HERE SUBSCRIBES TO ANYTHING
---------------------------------------
No transport is selected, no subscription is bought, no capture
behaviour changes. The governor computes numbers and returns verdicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Mapping, Optional, Tuple

from src.domain.adaptive_models import (
    BudgetPolicy, MarketDataBudgetState, MarketDataCostEstimate,
)

ENV_PREFIX = "MARKETLENS_MDCOST_"
ENV_BUDGET = ENV_PREFIX + "MONTHLY_BUDGET_USD"
ENV_FIXED = ENV_PREFIX + "FIXED_MONTHLY_USD"
ENV_PER_SNAPSHOT = ENV_PREFIX + "PER_SNAPSHOT_USD"
ENV_PER_REQUEST = ENV_PREFIX + "PER_REQUEST_USD"
ENV_STREAMING = ENV_PREFIX + "STREAMING_MARGINAL_USD"
ENV_FREE_ALLOWANCE = ENV_PREFIX + "FREE_ALLOWANCE_USD"

#: The working assumption for design and testing, not a law. The user's
#: stated ceiling is about USD 20 a month; it is a default, overridable
#: per call and by environment, and no other module may import it as a
#: constant to compare against.
DEFAULT_MONTHLY_BUDGET_USD = 20.0

#: Fractions of the budget at which the verdict changes. Deliberately
#: plain numbers with no calibration claimed behind them.
DEFAULT_WARN_FRACTION = 0.70
DEFAULT_THROTTLE_FRACTION = 0.90


class CostConfigurationError(ValueError):
    """The cost model cannot be trusted, so nothing may be spent on it."""


def _number(raw: Optional[str], name: str) -> Optional[float]:
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise CostConfigurationError(f"{name}={raw!r} is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise CostConfigurationError(f"{name}={raw!r} is not finite")
    if value < 0:
        raise CostConfigurationError(f"{name}={raw!r} must be >= 0")
    return value


@dataclass(frozen=True)
class MarketDataCostModel:
    """
    What data costs, as far as anyone has actually confirmed.

    A price left as None means UNKNOWN, never zero. `can_price` reports
    whether a given plan is priceable at all, and the governor blocks
    what it cannot price.
    """
    monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD
    fixed_monthly_subscription_cost_usd: float = 0.0
    per_snapshot_cost_usd: Optional[float] = None
    per_request_cost_usd: Optional[float] = None
    streaming_marginal_cost_usd: Optional[float] = None
    free_allowance_usd: float = 0.0
    provider: str = ""
    model_version: str = "unconfirmed"
    source_note: str = ""

    def __post_init__(self):
        for name in ("monthly_budget_usd", "fixed_monthly_subscription_cost_usd",
                     "free_allowance_usd"):
            value = getattr(self, name)
            if value is None:
                raise CostConfigurationError(f"{name} is required")
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise CostConfigurationError(f"{name} must be finite")
            if number < 0:
                raise CostConfigurationError(f"{name} must be >= 0")
        for name in ("per_snapshot_cost_usd", "per_request_cost_usd",
                     "streaming_marginal_cost_usd"):
            value = getattr(self, name)
            if value is None:
                continue
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                raise CostConfigurationError(f"{name} must be finite")
            if number < 0:
                raise CostConfigurationError(f"{name} must be >= 0")

    def can_price(self, *, snapshots: int = 0, requests: int = 0,
                  streaming_instruments: int = 0) -> bool:
        if snapshots and self.per_snapshot_cost_usd is None:
            return False
        if requests and self.per_request_cost_usd is None:
            return False
        if streaming_instruments and self.streaming_marginal_cost_usd is None:
            return False
        return True

    def unknown_prices(self) -> Tuple[str, ...]:
        return tuple(name for name in ("per_snapshot_cost_usd", "per_request_cost_usd",
                                       "streaming_marginal_cost_usd")
                     if getattr(self, name) is None)

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None,
                         **overrides) -> "MarketDataCostModel":
        """
        Build from the environment. An unparseable value raises.

        Raising is the fail-closed behaviour HERE, unlike the feature
        flags: a flag that cannot be read should leave the system as it
        was, but a PRICE that cannot be read must never be treated as a
        price, and the caller has to decide what to do about it.
        """
        env = os.environ if environ is None else environ
        budget = _number(env.get(ENV_BUDGET), ENV_BUDGET)
        model = cls(
            monthly_budget_usd=(budget if budget is not None
                                else DEFAULT_MONTHLY_BUDGET_USD),
            fixed_monthly_subscription_cost_usd=(
                _number(env.get(ENV_FIXED), ENV_FIXED) or 0.0),
            per_snapshot_cost_usd=_number(env.get(ENV_PER_SNAPSHOT), ENV_PER_SNAPSHOT),
            per_request_cost_usd=_number(env.get(ENV_PER_REQUEST), ENV_PER_REQUEST),
            streaming_marginal_cost_usd=_number(env.get(ENV_STREAMING), ENV_STREAMING),
            free_allowance_usd=(_number(env.get(ENV_FREE_ALLOWANCE),
                                        ENV_FREE_ALLOWANCE) or 0.0),
        )
        return replace(model, **overrides) if overrides else model


@dataclass(frozen=True)
class CostVerdict:
    """A priced plan, a policy, and the reason for it."""
    policy: BudgetPolicy
    estimate: MarketDataCostEstimate
    reason: str = ""
    projected_month_end_usd: Optional[float] = None

    @property
    def permits_spending(self) -> bool:
        return self.policy in (BudgetPolicy.ALLOW, BudgetPolicy.WARN)


class MarketDataCostGovernor:
    """
    Prices a plan and judges it against a budget. Deterministic.

    Holds no clock, no database and no transport: every input is passed
    in, so the same arguments always give the same verdict and a test
    needs no fixtures.
    """

    def __init__(self, model: Optional[MarketDataCostModel] = None, *,
                 warn_fraction: float = DEFAULT_WARN_FRACTION,
                 throttle_fraction: float = DEFAULT_THROTTLE_FRACTION):
        if not 0.0 < warn_fraction <= throttle_fraction <= 1.0:
            raise CostConfigurationError(
                "require 0 < warn_fraction <= throttle_fraction <= 1")
        self.model = model or MarketDataCostModel()
        self.warn_fraction = warn_fraction
        self.throttle_fraction = throttle_fraction

    # ---------------- pricing ----------------

    def estimate(self, *, instrument_count: int = 0, snapshots: int = 0,
                 requests: int = 0, streaming_instruments: int = 0,
                 include_fixed: bool = False) -> MarketDataCostEstimate:
        """
        What a plan would cost. Unknown prices are named, never assumed.

        `include_fixed` adds the monthly subscription: a caller pricing
        an incremental escalation does not pay it twice, and a caller
        pricing the whole month does.
        """
        for name, value in (("instrument_count", instrument_count),
                            ("snapshots", snapshots), ("requests", requests),
                            ("streaming_instruments", streaming_instruments)):
            if value < 0:
                raise CostConfigurationError(f"{name} must be >= 0")

        assumptions = []
        variable = 0.0
        if snapshots:
            price = self.model.per_snapshot_cost_usd
            if price is None:
                assumptions.append("per-snapshot price unknown")
            else:
                variable += snapshots * price
        if requests:
            price = self.model.per_request_cost_usd
            if price is None:
                assumptions.append("per-request price unknown")
            else:
                variable += requests * price
        if streaming_instruments:
            price = self.model.streaming_marginal_cost_usd
            if price is None:
                assumptions.append("streaming marginal price unknown")
            else:
                variable += streaming_instruments * price

        if self.model.free_allowance_usd:
            variable = max(0.0, variable - self.model.free_allowance_usd)
            assumptions.append(
                f"free allowance of {self.model.free_allowance_usd} USD applied")

        fixed = (self.model.fixed_monthly_subscription_cost_usd
                 if include_fixed else 0.0)
        return MarketDataCostEstimate(
            instrument_count=instrument_count, requests=requests,
            snapshots=snapshots, fixed_cost_usd=round(fixed, 6),
            variable_cost_usd=round(variable, 6),
            model_version=self.model.model_version,
            assumptions=tuple(assumptions))

    # ---------------- judging ----------------

    def assess(self, budget: MarketDataBudgetState,
               estimate: MarketDataCostEstimate) -> CostVerdict:
        """
        ALLOW / WARN / THROTTLE / BLOCK for one priced plan.

        BLOCK when the plan cannot be priced, or when it would exceed
        what is left. An unpriceable plan is not "probably fine": it is
        exactly the case that produced the USD 2,500 question.
        """
        if estimate.assumptions and any("unknown" in a for a in estimate.assumptions):
            return CostVerdict(
                BudgetPolicy.BLOCK, estimate,
                reason="cost model cannot price this plan: "
                       + "; ".join(a for a in estimate.assumptions if "unknown" in a))

        if budget.budget_usd <= 0:
            return CostVerdict(BudgetPolicy.BLOCK, estimate,
                               reason="no market-data budget is configured")

        projected = round(budget.spent_usd + budget.committed_usd
                          + estimate.total_usd, 6)
        if projected > budget.budget_usd:
            return CostVerdict(
                BudgetPolicy.BLOCK, estimate,
                reason=f"plan would reach {projected} USD against a budget of "
                       f"{budget.budget_usd} USD",
                projected_month_end_usd=projected)

        used = projected / budget.budget_usd
        if used >= self.throttle_fraction:
            policy, reason = BudgetPolicy.THROTTLE, (
                f"{used:.0%} of the monthly budget would be committed")
        elif used >= self.warn_fraction:
            policy, reason = BudgetPolicy.WARN, (
                f"{used:.0%} of the monthly budget would be committed")
        else:
            policy, reason = BudgetPolicy.ALLOW, ""
        return CostVerdict(policy, estimate, reason=reason,
                           projected_month_end_usd=projected)

    def budget_state(self, month: str, *, spent_usd: float = 0.0,
                     committed_usd: float = 0.0,
                     as_of: Optional[datetime] = None) -> MarketDataBudgetState:
        """A budget state for this governor's configured budget."""
        state = MarketDataBudgetState(
            month=month, budget_usd=self.model.monthly_budget_usd,
            spent_usd=spent_usd, committed_usd=committed_usd, as_of=as_of)
        verdict_policy = BudgetPolicy.ALLOW
        if state.is_exhausted:
            verdict_policy = BudgetPolicy.BLOCK
        elif state.budget_usd and (
                (state.spent_usd + state.committed_usd) / state.budget_usd
                >= self.throttle_fraction):
            verdict_policy = BudgetPolicy.THROTTLE
        elif state.budget_usd and (
                (state.spent_usd + state.committed_usd) / state.budget_usd
                >= self.warn_fraction):
            verdict_policy = BudgetPolicy.WARN
        return MarketDataBudgetState(
            month=state.month, budget_usd=state.budget_usd,
            spent_usd=state.spent_usd, committed_usd=state.committed_usd,
            projected_month_end_usd=state.spent_usd + state.committed_usd,
            policy=verdict_policy, as_of=state.as_of)

    def describe(self) -> Dict[str, object]:
        """Diagnostics. Prices only; no account or credential."""
        return {
            "model_version": self.model.model_version,
            "provider": self.model.provider or "unspecified",
            "monthly_budget_usd": self.model.monthly_budget_usd,
            "fixed_monthly_usd": self.model.fixed_monthly_subscription_cost_usd,
            "unknown_prices": list(self.model.unknown_prices()),
            "warn_fraction": self.warn_fraction,
            "throttle_fraction": self.throttle_fraction,
            "active": False,          # 25.9I-A: accounting only, nothing governed
        }
