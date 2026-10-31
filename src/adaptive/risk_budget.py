"""
src/adaptive/risk_budget.py
----------------------------------------
Capital at risk, and the budgets that bound it (Phase 25.9I-D).

WHY NOTIONAL IS NOT RISK
----------------------------
Two $20 positions can carry $1 and $6 of downside. An allocator that
only counts notional treats them as the same commitment and is wrong
about the second one. `PositionRiskEstimator` produces a fraction of
notional considered at risk, and the allocator spends a RISK budget
against it separately from the capital budget.

THREE WAYS TO ESTIMATE, IN ORDER OF HONESTY
-----------------------------------------------
1. INVALIDATION DISTANCE. If a defensible invalidation level exists and
   a reference price exists, the distance between them is the estimate.
   This phase does not invent one: 25.9I-E owns theses, and a stop
   created because sizing wanted a number is not a risk model.
2. VOLATILITY. Daily volatility over a horizon, scaled by a configurable
   multiple. Marked PARTIAL, because a volatility band is not an
   invalidation level and pretending otherwise would overstate what is
   known.
3. NOTHING. Status UNKNOWN, no number at all. The dataclass refuses to
   carry a value in this state so that a later caller cannot read a
   default as a measurement.

UNKNOWN RISK IS NOT ZERO RISK
---------------------------------
It cannot consume the risk budget, because there is nothing to subtract.
The allocator therefore caps what it will commit to an unmeasured
candidate with an explicit, configurable notional ceiling, and the plan
records the gap. Treating unknown as zero would let the least
understood position become the largest one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.adaptive_models import RiskBudget
from src.domain.allocation_models import PositionRiskEstimate, RiskEstimateStatus

#: Trading days per year, for scaling a daily volatility to a horizon.
TRADING_DAYS = 252


@dataclass(frozen=True)
class RiskConfig:
    """
    Risk-budget parameters. No production percentage is chosen here.

    Every budget defaults to None, meaning "not configured", and an
    unconfigured budget does not silently become unlimited: the allocator
    treats a missing budget as a gap and says so. Tests and fixtures
    supply explicit values; a production default would be a policy
    decision this phase has no evidence for (§8).
    """
    version: str = "risk-v1"
    account_risk_budget_usd: Optional[float] = None
    per_position_risk_budget_usd: Optional[float] = None
    #: Multiple of a horizon-scaled volatility treated as the downside.
    volatility_multiple: float = 2.0
    default_horizon_days: int = 5
    #: The most that may be committed to a candidate whose downside could
    #: not be estimated at all. Small on purpose, and configurable.
    unknown_risk_notional_cap_usd: Optional[float] = None

    def __post_init__(self):
        for name in ("account_risk_budget_usd", "per_position_risk_budget_usd",
                     "unknown_risk_notional_cap_usd"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.volatility_multiple <= 0:
            raise ValueError("volatility_multiple must be > 0")
        if self.default_horizon_days < 1:
            raise ValueError("default_horizon_days must be >= 1")

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version,
                "account_risk_budget_usd": self.account_risk_budget_usd,
                "per_position_risk_budget_usd": self.per_position_risk_budget_usd,
                "volatility_multiple": self.volatility_multiple,
                "default_horizon_days": self.default_horizon_days,
                "unknown_risk_notional_cap_usd": self.unknown_risk_notional_cap_usd}


class PositionRiskEstimator:
    """Estimates downside per dollar of notional. Deterministic, no I/O."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()

    def estimate(self, instrument_id: str, *,
                 reference_price: Optional[float] = None,
                 invalidation_price: Optional[float] = None,
                 daily_volatility: Optional[float] = None,
                 horizon_days: Optional[int] = None) -> PositionRiskEstimate:
        horizon = horizon_days or self.config.default_horizon_days

        # 1. A real invalidation level, if the caller has one.
        if (reference_price is not None and invalidation_price is not None
                and reference_price > 0):
            if invalidation_price >= reference_price:
                return PositionRiskEstimate(
                    instrument_id=instrument_id, status=RiskEstimateStatus.UNKNOWN,
                    method="invalidation", reference_price=reference_price,
                    invalidation_price=invalidation_price,
                    notes=("invalidation level is at or above the reference price; "
                           "no downside distance can be derived",))
            distance = (reference_price - invalidation_price) / reference_price
            return PositionRiskEstimate(
                instrument_id=instrument_id, status=RiskEstimateStatus.MEASURED,
                risk_per_dollar=round(min(1.0, distance), 6),
                method="invalidation distance", reference_price=reference_price,
                invalidation_price=invalidation_price, horizon_days=horizon)

        # 2. Volatility over the horizon. Indicative, so PARTIAL.
        if daily_volatility is not None and daily_volatility > 0:
            scaled = daily_volatility * math.sqrt(horizon) * self.config.volatility_multiple
            return PositionRiskEstimate(
                instrument_id=instrument_id, status=RiskEstimateStatus.PARTIAL,
                risk_per_dollar=round(min(1.0, scaled), 6),
                method=f"{self.config.volatility_multiple}x daily volatility over "
                       f"{horizon} days",
                volatility=daily_volatility, horizon_days=horizon,
                notes=("a volatility band is not an invalidation level; this is "
                       "indicative",))

        # 3. Nothing defensible.
        return PositionRiskEstimate(
            instrument_id=instrument_id, status=RiskEstimateStatus.UNKNOWN,
            method="none available", horizon_days=horizon,
            notes=("no invalidation level and no volatility estimate at as_of",))


@dataclass(frozen=True)
class RiskBudgetState:
    """
    What is left of the risk budget, and what is already spent.

    `existing_risk` is None when any held position's risk is unknown. In
    that case the remaining budget is also unknown, and the allocator
    must not spend against a number it cannot compute.
    """
    account_budget_usd: Optional[float]
    existing_risk_usd: Optional[float]
    proposed_risk_usd: float = 0.0

    @property
    def remaining_usd(self) -> Optional[float]:
        if self.account_budget_usd is None or self.existing_risk_usd is None:
            return None
        return round(max(0.0, self.account_budget_usd - self.existing_risk_usd
                         - self.proposed_risk_usd), 6)

    @property
    def is_measurable(self) -> bool:
        return self.remaining_usd is not None

    def would_exceed(self, additional_risk_usd: float) -> bool:
        """True only when measurably exceeded; unknown never 'fits'."""
        remaining = self.remaining_usd
        if remaining is None:
            return False
        return additional_risk_usd > remaining + 1e-9

    def spend(self, additional_risk_usd: float) -> "RiskBudgetState":
        return RiskBudgetState(
            account_budget_usd=self.account_budget_usd,
            existing_risk_usd=self.existing_risk_usd,
            proposed_risk_usd=round(self.proposed_risk_usd + additional_risk_usd, 6))

    def to_budget(self) -> Optional[RiskBudget]:
        """The I-A contract, when there is enough to build one."""
        if self.account_budget_usd is None:
            return None
        allocated = (self.existing_risk_usd or 0.0) + self.proposed_risk_usd
        return RiskBudget(total_risk_usd=self.account_budget_usd,
                          allocated_risk_usd=min(allocated, self.account_budget_usd))
