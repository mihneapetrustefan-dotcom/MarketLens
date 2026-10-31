"""
src/adaptive/allocator.py
--------------------------------------
The shadow capital allocator (Phase 25.9I-D).

IT PROPOSES. IT DOES NOT DECIDE, AND IT CANNOT TRADE
--------------------------------------------------------
Output is a `PortfolioAllocationPlan`: a record with notional amounts,
reasons and constraint results. It contains no order, no share quantity,
no venue and no account. The production path is unchanged and unshortened
-- canonical Portfolio, then the canonical Risk engine as the hard gate,
then OrderIntent, then Execution. This module imports none of them.

THE ALGORITHM, AND WHY IT IS NEITHER OF THE TWO OBVIOUS ONES
----------------------------------------------------------------
Equal weighting ignores that candidates differ. All-in on the top score
ignores that a score is not a forecast -- and with no promoted model,
this one especially is not. So:

  1. Candidates below `minimum_desirability` get nothing, with a reason.
  2. Each survivor gets a desirability SHARE of deployable capital:
     d_i^k / sum(d^k), with k configurable. k=1 is proportional; higher
     k concentrates. This is the "unequal but not winner-take-all" step.
  3. Every share is then cut by whichever limits bind, in a fixed order,
     and each cut is recorded as an `AllocationConstraintResult`:
     per-position cap, sector cap, correlation cap, per-position risk
     budget, account risk budget, unknown-risk cap, remaining capital.
  4. Whatever is not allocated stays in cash. It is NEVER redistributed
     to weaker candidates to "use up" the money.

Step 4 is the phase's whole point. Redistribution is how a system ends
up funding its worst idea with the capital its best idea could not take.

DETERMINISM
---------------
Candidates are sorted by (-desirability, instrument_id) before anything
is allocated, so the result does not depend on input order, dict order
or wall-clock time. Two runs over the same inputs produce the same plan
id and the same lines.

UNKNOWNS DEGRADE, THEY DO NOT DEFAULT
-----------------------------------------
Unknown downside cannot consume the risk budget, so it is capped by
`unknown_risk_notional_cap_usd` and the plan is PARTIAL with a stated
gap. Unknown correlation does not count as diversification. Unknown
costs are not zero costs. A plan whose inputs are missing says so in
`status` and `gaps` instead of looking complete.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.adaptive.correlation import AsOfCorrelationProvider
from src.adaptive.risk_budget import RiskBudgetState, RiskConfig
from src.domain.adaptive_models import (
    AllocationConstraint, AllocationRejectionReason, CashReserve, DecisionLineage,
    require_utc,
)
from src.domain.allocation_models import (
    AllocationCandidate, AllocationConstraintResult, AllocationLine,
    AllocationLineStatus, AllocationPlanStatus, CapitalStateStatus,
    PortfolioAllocationPlan, PortfolioCapitalState, RiskEstimateStatus,
)


@dataclass(frozen=True)
class AllocationConfig:
    """
    Every limit is a CEILING. None is a target.

    Defaults are deliberately conservative and, where a number would be a
    policy claim this repository cannot support, absent: the risk budgets
    live in `RiskConfig` and default to unset.
    """
    version: str = "allocation-v1"
    #: Ceiling on deployed capital as a fraction of equity. Not a target.
    max_deployment_fraction: float = 0.60
    max_position_fraction: float = 0.20
    max_sector_fraction: float = 0.40
    #: Cash held back before anything is allocated.
    minimum_cash_fraction: float = 0.10
    opportunity_reserve_fraction: float = 0.0
    #: Below this, an allocation is too small to be worth its costs.
    minimum_allocation_usd: float = 5.0
    #: What to do when a candidate's proportional share falls below the
    #: minimum useful size. "skip" zeroes it; "concentrate" raises it to
    #: the minimum in rank order while capital lasts, and zeroes the
    #: rest. Default "concentrate": a $100 account with 31 candidates
    #: gets ~$1.94 each, so "skip" funds nothing at all, and spreading
    #: capital below a viable size is not a better answer than holding
    #: fewer, larger positions. It is NOT a fill rule -- every candidate
    #: still has to clear minimum desirability and every cap, and
    #: leftover capital still stays in cash.
    min_size_policy: str = "concentrate"
    #: Concentration exponent over desirability. 1.0 is proportional.
    concentration_exponent: float = 1.0
    #: Off by default: no evidence exists for a particular cut-off (§15).
    max_pairwise_correlation: Optional[float] = None
    correlation_penalty_fraction: float = 0.5

    def __post_init__(self):
        for name in ("max_deployment_fraction", "max_position_fraction",
                     "max_sector_fraction", "minimum_cash_fraction",
                     "opportunity_reserve_fraction", "correlation_penalty_fraction"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.minimum_cash_fraction + self.opportunity_reserve_fraction > 1.0:
            raise ValueError("reserves cannot exceed the whole account")
        if self.minimum_allocation_usd < 0:
            raise ValueError("minimum_allocation_usd must be >= 0")
        if self.concentration_exponent <= 0:
            raise ValueError("concentration_exponent must be > 0")
        if self.min_size_policy not in ("skip", "concentrate"):
            raise ValueError("min_size_policy must be 'skip' or 'concentrate'")
        if self.max_pairwise_correlation is not None and not (
                -1.0 <= self.max_pairwise_correlation <= 1.0):
            raise ValueError("max_pairwise_correlation must be within [-1, 1]")

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version,
                "max_deployment_fraction": self.max_deployment_fraction,
                "max_position_fraction": self.max_position_fraction,
                "max_sector_fraction": self.max_sector_fraction,
                "minimum_cash_fraction": self.minimum_cash_fraction,
                "opportunity_reserve_fraction": self.opportunity_reserve_fraction,
                "minimum_allocation_usd": self.minimum_allocation_usd,
                "concentration_exponent": self.concentration_exponent,
                "max_pairwise_correlation": self.max_pairwise_correlation}


def _plan_id(as_of: datetime, config_version: str, revision: int,
             candidate_ids: Sequence[str]) -> str:
    payload = "|".join([as_of.isoformat(), config_version, str(revision),
                        ",".join(sorted(candidate_ids))])
    return "alloc-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SmartCapitalAllocator:
    """
    Deterministic, constrained, explainable. Shadow only.

    Holds no connection, no clock and no transport: every input arrives
    as an argument, which is what makes the plan reproducible and the
    safety properties checkable by reading the imports.
    """

    def __init__(self, config: Optional[AllocationConfig] = None,
                 risk_config: Optional[RiskConfig] = None,
                 minimum_desirability: float = 0.25):
        self.config = config or AllocationConfig()
        self.risk_config = risk_config or RiskConfig()
        self.minimum_desirability = minimum_desirability

    # ---------------- capital ----------------

    def deployable(self, state: PortfolioCapitalState) -> Tuple[float, List[CashReserve]]:
        """
        Cash, less the reserves, less anything above the deployment ceiling.

        The ceiling applies to TOTAL deployment including what is already
        held, so a portfolio that is already at the limit has nothing
        deployable rather than a fresh allowance.
        """
        config = self.config
        reserves: List[CashReserve] = []
        minimum_cash = round(state.account_equity * config.minimum_cash_fraction, 6)
        if minimum_cash > 0:
            reserves.append(CashReserve(
                amount_usd=min(minimum_cash, state.cash),
                reason=f"minimum cash reserve "
                       f"({config.minimum_cash_fraction:.0%} of equity)"))
        opportunity = round(state.account_equity * config.opportunity_reserve_fraction, 6)
        if opportunity > 0:
            reserves.append(CashReserve(
                amount_usd=min(opportunity, max(0.0, state.cash - minimum_cash)),
                reason="reserved for opportunities later in the session"))

        reserved = sum(r.amount_usd for r in reserves)
        ceiling = state.account_equity * config.max_deployment_fraction
        headroom = max(0.0, ceiling - state.deployed_notional)
        return round(max(0.0, min(state.cash - reserved, headroom)), 6), reserves

    # ---------------- the plan ----------------

    def allocate(self, candidates: Sequence[AllocationCandidate],
                 state: PortfolioCapitalState, as_of: datetime, *,
                 correlations: Optional[AsOfCorrelationProvider] = None,
                 lineage: Optional[DecisionLineage] = None,
                 revision: int = 0,
                 previous_plan_id: Optional[str] = None) -> PortfolioAllocationPlan:
        as_of = require_utc(as_of, "as_of")
        config = self.config
        gaps: List[str] = []
        notes: List[str] = []

        deployable, reserves = self.deployable(state)

        # ---- preconditions (§30: fail safely) --------------------------
        if not state.is_allocatable:
            return self._empty_plan(
                as_of, state, 0.0, reserves, AllocationPlanStatus.BLOCKED,
                gaps=tuple(state.gaps) or ("capital state provides no allocatable "
                                           "equity or cash",),
                revision=revision, previous_plan_id=previous_plan_id,
                lineage=lineage)
        if state.status is CapitalStateStatus.PARTIAL:
            gaps.extend(state.gaps)
        if not candidates:
            return self._empty_plan(
                as_of, state, deployable, reserves,
                AllocationPlanStatus.COMPLETE if not gaps
                else AllocationPlanStatus.PARTIAL,
                gaps=tuple(gaps) or ("no candidates were supplied; holding cash",),
                revision=revision, previous_plan_id=previous_plan_id,
                lineage=lineage, notes=("no candidates: 100% cash is a valid plan",))

        risk_state = RiskBudgetState(
            account_budget_usd=self.risk_config.account_risk_budget_usd,
            existing_risk_usd=state.existing_capital_at_risk)
        if self.risk_config.account_risk_budget_usd is None:
            gaps.append("no account risk budget is configured")
        elif state.existing_capital_at_risk is None:
            gaps.append("existing positions carry unmeasured risk, so remaining "
                        "risk capacity is unknown")

        # ---- ranking ---------------------------------------------------
        ranked = sorted(candidates,
                        key=lambda c: (-c.desirability.overall, c.instrument_id))
        eligible = [c for c in ranked
                    if c.desirability.overall >= self.minimum_desirability]
        rejected = [c for c in ranked
                    if c.desirability.overall < self.minimum_desirability]

        shares = self._desirability_shares(eligible)

        # ---- allocation ------------------------------------------------
        lines: List[AllocationLine] = []
        remaining = deployable
        sector_allocated: Dict[str, float] = dict(state.sector_exposure())
        funded_ids: List[str] = [e.instrument_id for e in state.existing_exposures]

        for candidate in eligible:
            share = round(shares.get(candidate.instrument_id, 0.0) * deployable, 6)
            requested = share
            reasons: List[str] = []
            if (config.min_size_policy == "concentrate"
                    and 0 < share < config.minimum_allocation_usd
                    and remaining >= config.minimum_allocation_usd):
                requested = config.minimum_allocation_usd
                reasons.append(
                    f"proportional share {share:.2f} is below the minimum useful "
                    f"size; raised to {config.minimum_allocation_usd:.2f} in rank "
                    f"order rather than spreading capital too thin")
            allocated = requested
            applied: List[AllocationConstraintResult] = []
            rejections: List[AllocationRejectionReason] = []

            def cut(new_value: float, name: str, scope: str, limit: Optional[float],
                    detail: str) -> float:
                nonlocal allocated
                if new_value < allocated - 1e-9:
                    applied.append(AllocationConstraintResult(
                        constraint=name, scope=scope, limited_to=limit,
                        removed=round(allocated - new_value, 6), detail=detail))
                    allocated = max(0.0, round(new_value, 6))
                return allocated

            # position cap, counting what is already held
            position_cap = state.account_equity * config.max_position_fraction
            existing = state.exposure_of(candidate.instrument_id)
            cut(max(0.0, position_cap - existing), "max_position_fraction",
                "position", round(position_cap, 6),
                f"position ceiling {config.max_position_fraction:.0%} of equity"
                + (f", {existing:.2f} already held" if existing else ""))

            # sector cap, counting existing sector exposure
            sector = candidate.sector_id or "unclassified"
            sector_cap = state.account_equity * config.max_sector_fraction
            used = sector_allocated.get(sector, 0.0)
            cut(max(0.0, sector_cap - used), "max_sector_fraction", "sector",
                round(sector_cap, 6),
                f"sector {sector} ceiling {config.max_sector_fraction:.0%} of equity"
                + (f", {used:.2f} already exposed" if used else ""))

            # correlation, only when a threshold was configured
            if correlations is not None and config.max_pairwise_correlation is not None:
                result = correlations.max_correlation_with(
                    candidate.instrument_id, funded_ids)
                if result.is_measured and result.value > config.max_pairwise_correlation:
                    cut(allocated * (1.0 - config.correlation_penalty_fraction),
                        "max_pairwise_correlation", "correlation",
                        config.max_pairwise_correlation,
                        f"correlation {result.value:.2f} with {result.b or result.a} "
                        f"above the limit")
                elif not result.is_measured and funded_ids:
                    reasons.append(f"correlation unmeasured ({result.reason}); "
                                   f"no diversification credit given")

            # risk budgets
            if candidate.risk.is_usable:
                per_position = self.risk_config.per_position_risk_budget_usd
                if per_position is not None and candidate.risk.risk_per_dollar:
                    affordable = per_position / candidate.risk.risk_per_dollar
                    cut(affordable, "per_position_risk_budget", "position",
                        round(per_position, 6),
                        f"downside {candidate.risk.risk_per_dollar:.1%} of notional "
                        f"caps size at {affordable:.2f}")
                remaining_risk = risk_state.remaining_usd
                if remaining_risk is not None and candidate.risk.risk_per_dollar:
                    affordable = remaining_risk / candidate.risk.risk_per_dollar
                    cut(affordable, "account_risk_budget", "portfolio",
                        round(remaining_risk, 6),
                        f"remaining account risk budget {remaining_risk:.2f}")
            else:
                cap = self.risk_config.unknown_risk_notional_cap_usd
                if cap is None:
                    cut(0.0, "unknown_risk", "position", None,
                        "downside could not be estimated and no cap for unknown "
                        "risk is configured")
                    rejections.append(AllocationRejectionReason.STALE_INPUTS)
                else:
                    cut(cap, "unknown_risk_notional_cap", "position", round(cap, 6),
                        "downside could not be estimated; capped rather than "
                        "treated as risk-free")
                reasons.append("risk status UNKNOWN: unknown downside is not zero "
                               "downside")

            # capital actually left
            cut(remaining, "remaining_capital", "portfolio", round(remaining, 6),
                "deployable capital already committed to higher-ranked candidates")

            # minimum size
            if 0 < allocated < config.minimum_allocation_usd:
                cut(0.0, "minimum_allocation", "position",
                    config.minimum_allocation_usd,
                    f"below the minimum useful allocation of "
                    f"{config.minimum_allocation_usd}")
                rejections.append(AllocationRejectionReason.BELOW_MINIMUM_SIZE)

            capital_at_risk = candidate.risk.capital_at_risk(allocated)
            if allocated <= 0:
                status = AllocationLineStatus.NOT_FUNDED
                # The binding reason goes FIRST. Any note explaining how the
                # request was sized is context, and a reader asking "why did
                # this get nothing" must not be handed that instead.
                if applied:
                    reasons.insert(0, "reduced to zero by "
                                   + ", ".join(a.constraint for a in applied))
                elif not reasons and not rejections:
                    reasons.append("no capital remained after higher-ranked "
                                   "candidates were funded")
            elif applied and allocated < requested - 1e-9:
                status = AllocationLineStatus.REDUCED
            else:
                status = AllocationLineStatus.FUNDED

            lines.append(AllocationLine(
                instrument_id=candidate.instrument_id, status=status,
                requested_notional=requested, allocated_notional=allocated,
                desirability=candidate.desirability, risk=candidate.risk,
                sector_id=candidate.sector_id, capital_at_risk=capital_at_risk,
                portfolio_weight=(round(allocated / state.account_equity, 6)
                                  if state.account_equity else None),
                constraints_applied=tuple(applied),
                rejection_reasons=tuple(rejections), reasons=tuple(reasons),
                session_snapshot_id=candidate.session_snapshot_id,
                admission_decision_id=candidate.admission_decision_id))

            if allocated > 0:
                remaining = round(remaining - allocated, 6)
                sector_allocated[sector] = round(used + allocated, 6)
                funded_ids.append(candidate.instrument_id)
                if capital_at_risk is not None:
                    risk_state = risk_state.spend(capital_at_risk)

        # candidates that never cleared the quality bar
        for candidate in rejected:
            lines.append(AllocationLine(
                instrument_id=candidate.instrument_id,
                status=AllocationLineStatus.NOT_FUNDED,
                requested_notional=0.0, allocated_notional=0.0,
                desirability=candidate.desirability, risk=candidate.risk,
                sector_id=candidate.sector_id,
                reasons=(f"desirability {candidate.desirability.overall:.3f} is "
                         f"below the minimum of {self.minimum_desirability}",),
                rejection_reasons=(AllocationRejectionReason.INSUFFICIENT_EXPECTED_EDGE,),
                session_snapshot_id=candidate.session_snapshot_id,
                admission_decision_id=candidate.admission_decision_id))

        # ---- status ----------------------------------------------------
        if any(not line.risk.is_usable for line in lines if line.allocated_notional > 0):
            gaps.append("at least one funded line has an unmeasured downside")
        if any(line.desirability.completeness < 1.0 for line in lines):
            gaps.append("desirability inputs are incomplete: "
                        "no promoted model supplies expected return or confidence")
        status = (AllocationPlanStatus.PARTIAL if gaps
                  else AllocationPlanStatus.COMPLETE)

        # Unallocated capital is an outcome, not a leftover to be spent.
        if remaining > 0:
            notes.append(f"{remaining:.2f} of deployable capital was not allocated "
                         f"and stays in cash; no candidate was promoted to use it")

        plan_id = _plan_id(as_of, config.version, revision,
                           [c.instrument_id for c in candidates])
        return PortfolioAllocationPlan(
            plan_id=plan_id, as_of=as_of, status=status, capital_state=state,
            deployable_capital=deployable, lines=tuple(lines),
            cash_reserves=tuple(reserves), risk_budget=risk_state.to_budget(),
            constraints=self._constraints(), gaps=tuple(dict.fromkeys(gaps)),
            config_version=config.version, lineage=lineage, revision=revision,
            previous_plan_id=previous_plan_id, notes=tuple(notes))

    # ---------------- helpers ----------------

    def _desirability_shares(self, candidates: Sequence[AllocationCandidate]
                             ) -> Dict[str, float]:
        """
        Share of deployable capital per candidate, before constraints.

        Proportional to desirability raised to `concentration_exponent`.
        Not equal weight (candidates differ), not winner-take-all (a
        score is not a forecast, least of all without a model).
        """
        if not candidates:
            return {}
        exponent = self.config.concentration_exponent
        raw = {c.instrument_id: c.desirability.overall ** exponent
               for c in candidates}
        total = sum(raw.values())
        if total <= 0:
            return {instrument_id: 0.0 for instrument_id in raw}
        return {instrument_id: value / total for instrument_id, value in raw.items()}

    def _constraints(self) -> Tuple[AllocationConstraint, ...]:
        config = self.config
        return (
            AllocationConstraint("max_deployment_fraction", "portfolio",
                                 config.max_deployment_fraction,
                                 note="ceiling on deployed capital, never a target"),
            AllocationConstraint("max_position_fraction", "position",
                                 config.max_position_fraction),
            AllocationConstraint("max_sector_fraction", "sector",
                                 config.max_sector_fraction),
            AllocationConstraint("minimum_cash_fraction", "portfolio",
                                 config.minimum_cash_fraction,
                                 note="held back before anything is allocated"),
        )

    def _empty_plan(self, as_of: datetime, state: PortfolioCapitalState,
                    deployable: float, reserves: Sequence[CashReserve],
                    status: AllocationPlanStatus, *, gaps: Tuple[str, ...],
                    revision: int, previous_plan_id: Optional[str],
                    lineage: Optional[DecisionLineage],
                    notes: Tuple[str, ...] = ()) -> PortfolioAllocationPlan:
        return PortfolioAllocationPlan(
            plan_id=_plan_id(as_of, self.config.version, revision, ()),
            as_of=as_of, status=status, capital_state=state,
            deployable_capital=deployable, lines=(),
            cash_reserves=tuple(reserves), constraints=self._constraints(),
            gaps=gaps, config_version=self.config.version, lineage=lineage,
            revision=revision, previous_plan_id=previous_plan_id, notes=notes)


# ======================================================================
# Baselines, for structural comparison only
# ======================================================================


def equal_weight_baseline(candidates: Sequence[AllocationCandidate],
                          deployable: float) -> Dict[str, float]:
    """
    The naive policy, kept as a COMPARISON only (§11, §25).

    It is not the smart allocator and must never be presented as one. It
    exists so a report can show how the constrained plan differs in
    concentration, deployment and risk -- never to claim either is more
    profitable, which needs returns this phase does not have.
    """
    if not candidates or deployable <= 0:
        return {}
    share = round(deployable / len(candidates), 6)
    return {c.instrument_id: share for c in candidates}


def all_cash_baseline(candidates: Sequence[AllocationCandidate],
                      deployable: float) -> Dict[str, float]:
    """Deploy nothing. A valid policy, and the correct one often enough."""
    return {c.instrument_id: 0.0 for c in candidates}
