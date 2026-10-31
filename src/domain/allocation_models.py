"""
src/domain/allocation_models.py
------------------------------------------
Capital allocation contracts (Phase 25.9I-D). Shadow only.

THE SENTENCE THIS MODULE EXISTS TO ENFORCE
----------------------------------------------
Capital existing is not a reason to deploy it. An opportunity existing
is not a reason to fund it. Every type here is built so that "allocated
nothing, held cash" is an ordinary, well-formed answer rather than an
error state someone has to special-case.

WHAT IS REUSED, NOT REBUILT
-------------------------------
`CapitalAllocationProposal`, `CapitalAllocationDecision`, `CashReserve`,
`RiskBudget`, `AllocationConstraint`, `AllocationRejectionReason` and
`ExecutionEconomics` already exist in `adaptive_models` from 25.9I-A,
and `AllocationProposal` / `RiskDecision` / `OrderIntent` are the
canonical Phase 11 production path. Nothing here replaces either set.
What this module adds is the vocabulary those two layers did not have:
the capital state an allocator reads, the desirability it computes, the
risk it estimates per position, and the plan it emits for a human or a
later phase to look at.

CAPITAL AND RISK ARE DIFFERENT QUANTITIES
---------------------------------------------
Two positions of $20 can carry $1 and $6 of downside. `PositionRiskEstimate`
keeps notional and capital-at-risk apart, and its `status` records
whether the estimate is real. UNKNOWN risk is never zero risk: a plan
containing an unknown is PARTIAL, and the allocator caps what it will
commit to something it cannot measure.

NOTHING HERE IS EXECUTABLE
------------------------------
No type carries a venue, an account, an order id, a share quantity or an
`is_executable` flag. Allocations are stated in notional currency
deliberately (§22): converting to shares needs an execution-grade price,
and this phase has none. The path to a broker remains Portfolio -> Risk
-> OrderIntent -> Execution, and no field here shortens it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.adaptive_models import (
    AllocationConstraint, AllocationRejectionReason, CashReserve,
    DecisionLineage, ExecutionEconomics, RiskBudget, require_utc,
)


def _non_negative(value: Optional[float], name: str,
                  allow_none: bool = False) -> Optional[float]:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite (got {value!r})")
    if number < 0:
        raise ValueError(f"{name} must be >= 0 (got {number})")
    return number


def _fraction(value: Optional[float], name: str,
              allow_none: bool = True) -> Optional[float]:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1 (got {number})")
    return number


# ======================================================================
# Capital state
# ======================================================================


class CapitalStateStatus(str, Enum):
    """Whether this state can be allocated against at all."""
    COMPLETE = "complete"
    PARTIAL = "partial"                 # usable, with stated gaps
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class ExistingExposure:
    """What the portfolio already holds, per instrument."""
    instrument_id: str
    notional: float
    sector_id: Optional[str] = None
    capital_at_risk: Optional[float] = None

    def __post_init__(self):
        _non_negative(self.notional, "notional")
        _non_negative(self.capital_at_risk, "capital_at_risk", allow_none=True)


@dataclass(frozen=True)
class PortfolioCapitalState:
    """
    The money side of the decision, read once and never mutated.

    Equity, cash and deployable capital are kept as three separate
    numbers because they answer three different questions and this
    project has been bitten by systems that conflate them. There is no
    buying-power field and no leverage: the repository has no leverage
    policy, and inventing one here would be a policy decision disguised
    as a data model (§6).
    """
    as_of: datetime
    account_equity: float
    cash: float
    base_currency: str = "USD"
    existing_exposures: Tuple[ExistingExposure, ...] = ()
    status: CapitalStateStatus = CapitalStateStatus.COMPLETE
    gaps: Tuple[str, ...] = ()
    source: str = ""

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        _non_negative(self.account_equity, "account_equity")
        _non_negative(self.cash, "cash")
        if self.status is not CapitalStateStatus.COMPLETE and not self.gaps:
            raise ValueError(f"a {self.status.value} capital state must state its gaps")
        seen = [e.instrument_id for e in self.existing_exposures]
        if len(set(seen)) != len(seen):
            raise ValueError("an instrument may appear once in existing exposures")

    @property
    def deployed_notional(self) -> float:
        return round(sum(e.notional for e in self.existing_exposures), 6)

    @property
    def existing_capital_at_risk(self) -> Optional[float]:
        """None when any holding's risk is unmeasured: unknown is not zero."""
        values = [e.capital_at_risk for e in self.existing_exposures]
        if any(v is None for v in values):
            return None
        return round(sum(values), 6)

    def exposure_of(self, instrument_id: str) -> float:
        for exposure in self.existing_exposures:
            if exposure.instrument_id == instrument_id:
                return exposure.notional
        return 0.0

    def sector_exposure(self) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        for exposure in self.existing_exposures:
            key = exposure.sector_id or "unclassified"
            totals[key] = round(totals.get(key, 0.0) + exposure.notional, 6)
        return dict(sorted(totals.items()))

    @property
    def is_allocatable(self) -> bool:
        """A state with no equity or unknown cash funds nothing (§30)."""
        return (self.status is not CapitalStateStatus.INSUFFICIENT_DATA
                and self.account_equity > 0 and self.cash > 0)


# ======================================================================
# Desirability
# ======================================================================


@dataclass(frozen=True)
class CapitalDesirability:
    """
    How much this candidate deserves capital, and why.

    NOT an expected return, and deliberately not called one. 25.9I-B
    established that the session score ranks attention, liquidity and
    data quality, and that `expected_return` and `model_confidence` are
    unavailable because no model is promoted. That distinction survives
    here: `unavailable` names every input that could not be measured,
    and a caller reading `overall` alone is reading an attention-weighted
    preference, not a forecast.
    """
    instrument_id: str
    overall: float
    positives: Dict[str, float] = field(default_factory=dict)
    penalties: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    unavailable: Tuple[str, ...] = ()
    method_version: str = ""
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        _fraction(self.overall, "overall", allow_none=False)
        for group, name in ((self.positives, "positives"), (self.penalties, "penalties")):
            for key, value in group.items():
                if value != value or value in (float("inf"), float("-inf")):
                    raise ValueError(f"{name}[{key}] must be finite")
        overlap = (set(self.positives) | set(self.penalties)) & set(self.unavailable)
        if overlap:
            raise ValueError(f"measured and unavailable at once: {sorted(overlap)}")

    @property
    def completeness(self) -> float:
        measured = len(self.positives) + len(self.penalties)
        wanted = measured + len(self.unavailable)
        return round(measured / wanted, 6) if wanted else 0.0

    def contributions(self) -> List[Tuple[str, float]]:
        """Signed contribution per component, largest magnitude first."""
        rows = [(name, value * self.weights.get(name, 0.0))
                for name, value in self.positives.items()]
        rows += [(name, -value * self.weights.get(name, 0.0))
                 for name, value in self.penalties.items()]
        return sorted(rows, key=lambda kv: -abs(kv[1]))


# ======================================================================
# Risk
# ======================================================================


class RiskEstimateStatus(str, Enum):
    """
    How the downside was established. UNKNOWN is not a small number.

    MEASURED     from a defensible invalidation level or volatility
    PARTIAL      one input was assumed; the estimate is indicative
    UNKNOWN      no defensible estimate exists at all
    """
    MEASURED = "measured"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PositionRiskEstimate:
    """
    What could be lost on a proposed position, distinct from its size.

    `risk_per_dollar` is the fraction of notional considered at risk, so
    a $20 position with 0.05 carries $1 and one with 0.30 carries $6.
    The allocator must treat those differently, and `status` decides
    whether it may rely on the number at all.
    """
    instrument_id: str
    status: RiskEstimateStatus
    risk_per_dollar: Optional[float] = None
    method: str = ""
    reference_price: Optional[float] = None
    invalidation_price: Optional[float] = None
    volatility: Optional[float] = None
    horizon_days: Optional[int] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.status is RiskEstimateStatus.UNKNOWN:
            if self.risk_per_dollar is not None:
                raise ValueError(
                    "an UNKNOWN risk estimate must not carry a number: a value "
                    "here would be read as a measurement")
            if not self.notes:
                raise ValueError("an UNKNOWN risk estimate must say why")
        else:
            value = _fraction(self.risk_per_dollar, "risk_per_dollar",
                              allow_none=False)
            if value == 0:
                raise ValueError(
                    "a measured risk of exactly zero is not credible; use "
                    "UNKNOWN if it could not be established")
        for name in ("reference_price", "invalidation_price", "volatility"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")

    @property
    def is_usable(self) -> bool:
        return self.status is not RiskEstimateStatus.UNKNOWN

    def capital_at_risk(self, notional: float) -> Optional[float]:
        """None when unknown: the caller must not substitute zero."""
        if not self.is_usable or self.risk_per_dollar is None:
            return None
        return round(notional * self.risk_per_dollar, 6)


# ======================================================================
# Candidates, constraints and the plan
# ======================================================================


@dataclass(frozen=True)
class AllocationCandidate:
    """
    One admitted opportunity, with everything the allocator may read.

    `admission_decision_id` and `session_snapshot_id` come from 25.9I-B
    and are what make the final plan traceable back to the universe
    decision that produced the candidate.
    """
    instrument_id: str
    as_of: datetime
    desirability: CapitalDesirability
    risk: PositionRiskEstimate
    sector_id: Optional[str] = None
    session_snapshot_id: Optional[str] = None
    admission_decision_id: Optional[str] = None
    opportunity_score: Optional[float] = None
    score_completeness: Optional[float] = None
    economics: Optional[ExecutionEconomics] = None
    liquidity_notional: Optional[float] = None
    horizon_days: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if self.desirability.instrument_id != self.instrument_id:
            raise ValueError("desirability belongs to a different instrument")
        if self.risk.instrument_id != self.instrument_id:
            raise ValueError("risk estimate belongs to a different instrument")


@dataclass(frozen=True)
class AllocationConstraintResult:
    """One limit, applied to one candidate, with the amount it removed."""
    constraint: str
    scope: str
    limited_to: Optional[float]
    removed: float
    detail: str = ""

    def __post_init__(self):
        _non_negative(self.removed, "removed")
        _non_negative(self.limited_to, "limited_to", allow_none=True)


class AllocationLineStatus(str, Enum):
    FUNDED = "funded"
    REDUCED = "reduced"                  # funded, below what was requested
    NOT_FUNDED = "not_funded"            # deliberately zero


@dataclass(frozen=True)
class AllocationLine:
    """
    One candidate's outcome: what it asked for, what it got, and why.

    The four questions §26 requires an answer to -- why this symbol, why
    this amount, why not more, why not zero -- are answerable from the
    recorded fields alone, never from prose written afterwards.
    """
    instrument_id: str
    status: AllocationLineStatus
    requested_notional: float
    allocated_notional: float
    desirability: CapitalDesirability
    risk: PositionRiskEstimate
    sector_id: Optional[str] = None
    capital_at_risk: Optional[float] = None
    portfolio_weight: Optional[float] = None
    constraints_applied: Tuple[AllocationConstraintResult, ...] = ()
    rejection_reasons: Tuple[AllocationRejectionReason, ...] = ()
    reasons: Tuple[str, ...] = ()
    session_snapshot_id: Optional[str] = None
    admission_decision_id: Optional[str] = None

    def __post_init__(self):
        _non_negative(self.requested_notional, "requested_notional")
        _non_negative(self.allocated_notional, "allocated_notional")
        _non_negative(self.capital_at_risk, "capital_at_risk", allow_none=True)
        if self.allocated_notional > self.requested_notional + 1e-9:
            raise ValueError("allocated more than was requested")
        if self.status is AllocationLineStatus.NOT_FUNDED:
            if self.allocated_notional > 0:
                raise ValueError("a NOT_FUNDED line cannot carry capital")
            if not (self.reasons or self.rejection_reasons):
                raise ValueError("a zero allocation must record why")
        if self.status is AllocationLineStatus.REDUCED and not self.constraints_applied:
            raise ValueError("a reduced line must name the constraint that cut it")
        if self.status is AllocationLineStatus.FUNDED and self.allocated_notional <= 0:
            raise ValueError("a FUNDED line must carry capital")

    @property
    def reduction(self) -> float:
        return round(self.requested_notional - self.allocated_notional, 6)


class AllocationPlanStatus(str, Enum):
    """
    Whether this plan could be relied on, if anything were relying on it.

    COMPLETE          every input the config asked for was present
    PARTIAL           usable, with named gaps (e.g. unknown risk)
    INSUFFICIENT_DATA no allocation could be justified
    BLOCKED           a hard precondition failed (no capital, stale state)
    """
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PortfolioAllocationPlan:
    """
    The whole shadow proposal: lines, cash, risk and constraints.

    THE INVARIANT: deployed + reserved + unallocated == deployable, and
    deployed never exceeds deployable. THE ABSENT INVARIANT, again
    deliberately: nothing requires deployed to reach deployable. A plan
    that funds nothing and holds everything in cash is valid and
    complete.
    """
    plan_id: str
    as_of: datetime
    status: AllocationPlanStatus
    capital_state: PortfolioCapitalState
    deployable_capital: float
    lines: Tuple[AllocationLine, ...] = ()
    cash_reserves: Tuple[CashReserve, ...] = ()
    risk_budget: Optional[RiskBudget] = None
    constraints: Tuple[AllocationConstraint, ...] = ()
    gaps: Tuple[str, ...] = ()
    config_version: str = ""
    lineage: Optional[DecisionLineage] = None
    revision: int = 0
    previous_plan_id: Optional[str] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        _non_negative(self.deployable_capital, "deployable_capital")
        if self.deployed > self.deployable_capital + 1e-9:
            raise ValueError(
                f"plan deploys {self.deployed} against deployable capital of "
                f"{self.deployable_capital}")
        if self.revision > 0 and not self.previous_plan_id:
            raise ValueError("a plan revision must name the plan it followed")
        seen = [line.instrument_id for line in self.lines]
        if len(set(seen)) != len(seen):
            raise ValueError("an instrument may appear once in a plan")
        if self.status in (AllocationPlanStatus.PARTIAL,
                           AllocationPlanStatus.INSUFFICIENT_DATA,
                           AllocationPlanStatus.BLOCKED) and not self.gaps:
            raise ValueError(f"a {self.status.value} plan must state its gaps")

    # ---------------- totals ----------------

    @property
    def deployed(self) -> float:
        return round(sum(line.allocated_notional for line in self.lines), 6)

    @property
    def reserved(self) -> float:
        return round(sum(reserve.amount_usd for reserve in self.cash_reserves), 6)

    @property
    def unallocated(self) -> float:
        """
        Deployable capital that no candidate earned.

        Reserves are NOT subtracted again here: `deployable_capital` is
        already net of them. Subtracting twice made this negative in the
        first smoke test, which is how the double-count was found.
        """
        return round(self.deployable_capital - self.deployed, 6)

    @property
    def capital_above_ceiling(self) -> float:
        """Cash excluded by the deployment ceiling rather than by choice."""
        return round(max(0.0, self.capital_state.cash - self.reserved
                         - self.deployable_capital), 6)

    @property
    def retained_cash(self) -> float:
        """Everything not deployed, including capital never made deployable."""
        return round(self.capital_state.cash - self.deployed, 6)

    def check_identity(self, tolerance: float = 1e-6) -> bool:
        """
        cash == deployed + unallocated + reserved + above-ceiling.

        Asserted by the tests rather than assumed: an allocator whose
        totals do not reconcile is one that has either invented or lost
        money somewhere.
        """
        total = (self.deployed + self.unallocated + self.reserved
                 + self.capital_above_ceiling)
        return abs(total - self.capital_state.cash) <= tolerance

    @property
    def deployment_fraction(self) -> Optional[float]:
        if self.capital_state.account_equity <= 0:
            return None
        return round(self.deployed / self.capital_state.account_equity, 6)

    @property
    def total_capital_at_risk(self) -> Optional[float]:
        """None when any funded line's risk is unknown."""
        values = [line.capital_at_risk for line in self.funded_lines]
        if any(value is None for value in values):
            return None
        return round(sum(values), 6)

    @property
    def funded_lines(self) -> Tuple[AllocationLine, ...]:
        return tuple(line for line in self.lines if line.allocated_notional > 0)

    @property
    def is_all_cash(self) -> bool:
        return self.deployed == 0.0

    def sector_allocation(self) -> Dict[str, float]:
        totals: Dict[str, float] = {}
        for line in self.funded_lines:
            key = line.sector_id or "unclassified"
            totals[key] = round(totals.get(key, 0.0) + line.allocated_notional, 6)
        return dict(sorted(totals.items()))

    def line(self, instrument_id: str) -> Optional[AllocationLine]:
        for line in self.lines:
            if line.instrument_id == instrument_id:
                return line
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id, "as_of": self.as_of.isoformat(),
            "status": self.status.value, "revision": self.revision,
            "account_equity": self.capital_state.account_equity,
            "cash": self.capital_state.cash,
            "deployable_capital": self.deployable_capital,
            "deployed": self.deployed, "reserved": self.reserved,
            "unallocated": self.unallocated, "retained_cash": self.retained_cash,
            "deployment_fraction": self.deployment_fraction,
            "total_capital_at_risk": self.total_capital_at_risk,
            "funded": len(self.funded_lines), "considered": len(self.lines),
            "sector_allocation": self.sector_allocation(),
            "gaps": list(self.gaps),
            "config_version": self.config_version,
        }
