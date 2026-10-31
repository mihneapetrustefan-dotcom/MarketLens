"""
src/domain/adaptive_models.py
------------------------------------------
Contracts for the adaptive decision layer (Phase 25.9I-A).

NOTHING HERE DECIDES ANYTHING YET
-------------------------------------
This module is types and validation only. No selector runs, no
subscription is opened, no capital is allocated, no order is built.
Phase 25.9I-A exists so that 25.9I-B..H can implement behaviour against
a settled vocabulary instead of inventing one under time pressure, and
so that the invariants below are enforced by construction rather than
by everyone remembering them.

THE FOUR RULES THESE TYPES ENFORCE
--------------------------------------
1. CAPACITY IS NOT A TARGET. `SessionUniverse` accepts 0..max members.
   There is no validation anywhere that pushes the count toward the
   maximum, and `unused_capacity` is a normal, healthy number.

2. CAPITAL IS NOT REQUIRED TO BE DEPLOYED. `CapitalAllocationProposal`
   treats cash as a first-class line. Allocations must not EXCEED the
   capital available; they are never required to reach it, and an
   allocation of exactly zero is valid rather than a rejection.

3. RESEARCH ADMISSION IS NOT CAPITAL ALLOCATION. An instrument can be
   eligible, be admitted to the session universe, score well as an
   opportunity, and still receive nothing. Four separate types, on
   purpose: collapsing them is what makes a system spend money because
   it found something interesting.

4. DELAYED DATA MAY ANALYSE, NEVER EXECUTE. `MarketDataRequirement`
   distinguishes what a decision needs from what the feed provides;
   see `src/adaptive/freshness.py` for the policy that reads it.

WHAT IS REUSED RATHER THAN REDEFINED
----------------------------------------
`DataFreshness` and `FreshnessPolicy` (paper_models, Phase 13) and
`MarketDataAvailability` (market_data_models, Phase 25.7) already say
how old data is and whether a venue served it live. This module does
not restate them. What it adds is the orthogonal question they never
answered: how fresh a given DECISION requires its inputs to be, and
what the system is willing to pay to make them that fresh.

`AllocationProposal`, `RiskDecision` and `OrderIntent`
(portfolio_models, Phase 11) remain the production path. The proposals
here are inputs to that path, never a way around it: no type in this
module carries a venue, an account, or an executable flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.domain.market_data_models import MarketDataAvailability
from src.domain.paper_models import DataFreshness

# ======================================================================
# Shared validation
# ======================================================================


def require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    """Timestamps are UTC-aware or absent. A naive datetime is a bug."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime (got {type(value).__name__})")
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _require_finite(value: Optional[float], name: str,
                    allow_none: bool = True) -> Optional[float]:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite (got {value!r})")
    return number


def _require_non_negative(value: Optional[float], name: str,
                          allow_none: bool = True) -> Optional[float]:
    number = _require_finite(value, name, allow_none)
    if number is not None and number < 0:
        raise ValueError(f"{name} must be >= 0 (got {number})")
    return number


def _require_fraction(value: Optional[float], name: str,
                      allow_none: bool = True) -> Optional[float]:
    number = _require_finite(value, name, allow_none)
    if number is not None and not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1 (got {number})")
    return number


# ======================================================================
# Universe and opportunity
# ======================================================================


class OpportunityStatus(str, Enum):
    """Where a candidate stands. Rejection is a first-class outcome."""
    PROPOSED = "proposed"            # scored, not yet judged
    QUALIFIED = "qualified"          # good enough to admit
    REJECTED = "rejected"            # judged and declined, with reasons
    EXPIRED = "expired"              # was qualified; its validity ran out
    WITHDRAWN = "withdrawn"          # superseded before it was acted on


class AdmissionOutcome(str, Enum):
    ADMITTED = "admitted"
    DECLINED = "declined"
    #: Qualified, but the session was already at capacity. Recorded
    #: distinctly from DECLINED: the candidate was good enough, and the
    #: reason it is absent is a limit rather than its own quality.
    CAPACITY_REACHED = "capacity_reached"


@dataclass(frozen=True)
class EligibleInstrument:
    """
    A member of the parent universe: allowed to be considered, nothing more.

    Eligibility is a slow-moving property (listed, liquid enough, mapped
    to a broker contract). It says nothing about today.
    """
    instrument_id: str
    symbol: Optional[str] = None
    sector: Optional[str] = None
    eligible_from: Optional[datetime] = None
    eligible_until: Optional[datetime] = None
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        object.__setattr__(self, "eligible_from",
                           require_utc(self.eligible_from, "eligible_from"))
        object.__setattr__(self, "eligible_until",
                           require_utc(self.eligible_until, "eligible_until"))
        if (self.eligible_from and self.eligible_until
                and self.eligible_until < self.eligible_from):
            raise ValueError("eligible_until must not precede eligible_from")

    def is_eligible_at(self, moment: datetime) -> bool:
        moment = require_utc(moment, "moment")
        if self.eligible_from and moment < self.eligible_from:
            return False
        if self.eligible_until and moment > self.eligible_until:
            return False
        return True


@dataclass(frozen=True)
class OpportunityScore:
    """
    A score and the components behind it.

    The components are kept because a single number cannot be argued
    with later: attribution in 25.9I-G needs to know WHICH part of the
    score was right, and a scalar throws that away at the moment it is
    computed.

    `unavailable` names the components that could not be measured, which
    is not the same as measuring zero. 25.9I-B added it because this
    project has no qualified predictive model: an expected-return
    component that silently defaulted to 0.0 would look like a measured
    opinion of "no edge" rather than the absence of an opinion.
    """
    overall: float
    components: Dict[str, float] = field(default_factory=dict)
    method_version: str = ""
    weights: Dict[str, float] = field(default_factory=dict)
    unavailable: Tuple[str, ...] = ()

    def __post_init__(self):
        _require_finite(self.overall, "overall", allow_none=False)
        for name, value in self.components.items():
            _require_finite(value, f"components[{name}]", allow_none=False)
        for name, value in self.weights.items():
            _require_non_negative(value, f"weights[{name}]", allow_none=False)
        overlap = set(self.components) & set(self.unavailable)
        if overlap:
            raise ValueError(
                f"a component cannot be both measured and unavailable: "
                f"{sorted(overlap)}")

    def component(self, name: str, default: Optional[float] = None) -> Optional[float]:
        return self.components.get(name, default)

    @property
    def completeness(self) -> float:
        """Share of the components this scorer wanted that it actually got."""
        wanted = len(self.components) + len(self.unavailable)
        return round(len(self.components) / wanted, 6) if wanted else 0.0


class EligibilityStatus(str, Enum):
    """
    Whether an instrument may be evaluated at all.

    INSUFFICIENT_DATA is deliberately not a synonym for INELIGIBLE. One
    says "we looked and it does not qualify", the other says "we could
    not tell", and a system that merges them eventually treats silence
    as approval.
    """
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class EligibilityDecision:
    """One instrument judged against the filters, with the numbers used."""
    instrument_id: str
    status: EligibilityStatus
    as_of: datetime
    reasons: Tuple[str, ...] = ()
    measurements: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        if self.status is not EligibilityStatus.ELIGIBLE and not self.reasons:
            raise ValueError(
                f"a {self.status.value} decision must carry reasons")

    @property
    def is_eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


class AdmissionVerdict(str, Enum):
    """
    What the admission layer decided, kept separate from the score.

    A high score is not admission: thresholds, capacity, concentration
    and data completeness all sit between them, and collapsing the two
    would make "why is this in the session" unanswerable.
    """
    PASS = "pass"
    WATCH = "watch"
    REJECT = "reject"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class OpportunityValidity:
    """
    When an assessment stops being usable.

    A 7-day thesis does not become a 14-day thesis because the target
    was not reached (§10). Expiry is set when the assessment is made,
    and extending it requires a new assessment, not a new deadline.
    """
    as_of: datetime
    expires_at: Optional[datetime] = None
    max_age: Optional[timedelta] = None
    conditions: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        object.__setattr__(self, "expires_at",
                           require_utc(self.expires_at, "expires_at"))
        if self.expires_at and self.expires_at < self.as_of:
            raise ValueError("expires_at must not precede as_of")
        if self.max_age is not None and self.max_age.total_seconds() <= 0:
            raise ValueError("max_age must be positive")

    def deadline(self) -> Optional[datetime]:
        """The earliest of the explicit expiry and the max age."""
        by_age = (self.as_of + self.max_age) if self.max_age else None
        candidates = [d for d in (self.expires_at, by_age) if d is not None]
        return min(candidates) if candidates else None

    def is_expired_at(self, moment: datetime) -> bool:
        moment = require_utc(moment, "moment")
        deadline = self.deadline()
        return deadline is not None and moment > deadline


@dataclass(frozen=True)
class OpportunityAssessment:
    """One instrument judged once, with the reasons on both sides."""
    assessment_id: str
    instrument_id: str
    session_date: str
    score: OpportunityScore
    validity: OpportunityValidity
    status: OpportunityStatus = OpportunityStatus.PROPOSED
    selection_reasons: Tuple[str, ...] = ()
    rejection_reasons: Tuple[str, ...] = ()
    sector: Optional[str] = None
    source_lineage: Tuple[str, ...] = ()
    generated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.assessment_id:
            raise ValueError("assessment_id is required")
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        object.__setattr__(self, "generated_at",
                           require_utc(self.generated_at, "generated_at"))
        if self.status is OpportunityStatus.REJECTED and not self.rejection_reasons:
            # A rejection nobody can explain is indistinguishable from a
            # bug, and it is the kind that survives for months.
            raise ValueError("a REJECTED assessment must carry rejection_reasons")

    @property
    def is_qualified(self) -> bool:
        return self.status is OpportunityStatus.QUALIFIED


@dataclass(frozen=True)
class OpportunityAdmissionDecision:
    """Whether a qualified assessment entered the session universe."""
    decision_id: str
    assessment_id: str
    instrument_id: str
    outcome: AdmissionOutcome
    decided_at: datetime
    reasons: Tuple[str, ...] = ()
    capacity_at_decision: Optional[int] = None
    occupied_at_decision: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(self, "decided_at", require_utc(self.decided_at, "decided_at"))
        for name in ("capacity_at_decision", "occupied_at_decision"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")

    @property
    def admitted(self) -> bool:
        return self.outcome is AdmissionOutcome.ADMITTED


@dataclass(frozen=True)
class SessionUniverse:
    """
    The instruments a session will actually watch: 0..max_capacity.

    THE POINT OF THIS TYPE. `max_capacity` is a ceiling, never a quota.
    Thirteen qualified candidates and eighteen unused slots is a correct
    outcome, and nothing here will ever suggest topping it up with
    weaker names. The only rule is that the count cannot EXCEED the
    ceiling.
    """
    session_date: str
    max_capacity: int
    members: Tuple[str, ...] = ()
    selected_at: Optional[datetime] = None
    parent_universe_size: Optional[int] = None
    method_version: str = ""
    lineage_id: Optional[str] = None

    def __post_init__(self):
        if self.max_capacity < 0:
            raise ValueError("max_capacity must be >= 0")
        if len(self.members) > self.max_capacity:
            raise ValueError(
                f"session universe holds {len(self.members)} members, above its "
                f"capacity of {self.max_capacity}")
        if len(set(self.members)) != len(self.members):
            raise ValueError("session universe members must be unique")
        object.__setattr__(self, "selected_at",
                           require_utc(self.selected_at, "selected_at"))
        if self.parent_universe_size is not None and self.parent_universe_size < 0:
            raise ValueError("parent_universe_size must be >= 0")

    @property
    def occupied(self) -> int:
        return len(self.members)

    @property
    def unused_capacity(self) -> int:
        """Healthy, not a deficiency. Nothing exists to 'fix' this number."""
        return self.max_capacity - self.occupied

    @property
    def is_empty(self) -> bool:
        """A session that admits nothing is a valid session."""
        return not self.members


# ======================================================================
# Market data state
# ======================================================================


class MarketDataState(str, Enum):
    """
    How closely an instrument is being watched, and what that costs.

    COLD  screening on delayed, daily or historical data. Cheap. No
          execution is authorised from this state.
    WARM  approaching a decision: the system may need a fresh price
          soon, and pays a little to keep the option open.
    HOT   execution or short-horizon monitoring: realtime-quality data
          is required, and the cost is accepted deliberately.

    The states are about WILLINGNESS TO PAY, not about what a feed
    happens to deliver. `MarketDataAvailability` answers the second
    question and is not duplicated here.
    """
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


class RealtimeActivationReason(str, Enum):
    ENTRY_CANDIDATE = "entry_candidate"
    EXIT_CANDIDATE = "exit_candidate"
    TARGET_PROXIMITY = "target_proximity"
    INVALIDATION_PROXIMITY = "invalidation_proximity"
    MATERIAL_POSITIVE_EVENT = "material_positive_event"
    MATERIAL_NEGATIVE_EVENT = "material_negative_event"
    VOLATILITY_SHOCK = "volatility_shock"
    REGIME_CHANGE = "regime_change"
    SHORT_HORIZON_POSITION = "short_horizon_position"
    PERIODIC_POSITION_VERIFICATION = "periodic_position_verification"
    RISK_OVERRIDE = "risk_override"


class RealtimeDeactivationReason(str, Enum):
    DECISION_COMPLETE = "decision_complete"
    OPPORTUNITY_EXPIRED = "opportunity_expired"
    POSITION_CLOSED = "position_closed"
    THESIS_INVALIDATED = "thesis_invalidated"
    HORIZON_LENGTHENED = "horizon_lengthened"
    BUDGET_EXHAUSTED = "budget_exhausted"
    COST_NOT_JUSTIFIED = "cost_not_justified"
    SESSION_ENDED = "session_ended"


#: Every legal move. Escalation to HOT is always allowed (a decision can
#: arrive at any time); everything else is explicit so an implementation
#: cannot invent a path by accident.
LEGAL_STATE_TRANSITIONS: Dict[MarketDataState, Tuple[MarketDataState, ...]] = {
    MarketDataState.COLD: (MarketDataState.WARM, MarketDataState.HOT),
    MarketDataState.WARM: (MarketDataState.COLD, MarketDataState.HOT),
    MarketDataState.HOT: (MarketDataState.WARM, MarketDataState.COLD),
}


@dataclass(frozen=True)
class MarketDataStateTransition:
    """
    One move between states, with its reason and its cost consequence.

    Escalation must name a `RealtimeActivationReason`, de-escalation a
    `RealtimeDeactivationReason`. "No resource is consumed merely
    because it exists" (§3.J) is enforced here: there is no way to reach
    HOT without recording why.
    """
    instrument_id: str
    previous_state: MarketDataState
    new_state: MarketDataState
    at: datetime
    activation_reason: Optional[RealtimeActivationReason] = None
    deactivation_reason: Optional[RealtimeDeactivationReason] = None
    note: str = ""
    lineage_id: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "at", require_utc(self.at, "at"))
        if self.previous_state is self.new_state:
            raise ValueError("a transition must change state")
        allowed = LEGAL_STATE_TRANSITIONS[self.previous_state]
        if self.new_state not in allowed:
            raise ValueError(
                f"{self.previous_state.value} -> {self.new_state.value} is not a "
                f"legal market-data state transition")
        if self.is_escalation and self.activation_reason is None:
            raise ValueError(
                "escalating market-data state requires an activation reason")
        if not self.is_escalation and self.deactivation_reason is None:
            raise ValueError(
                "de-escalating market-data state requires a deactivation reason")

    @property
    def is_escalation(self) -> bool:
        order = {MarketDataState.COLD: 0, MarketDataState.WARM: 1, MarketDataState.HOT: 2}
        return order[self.new_state] > order[self.previous_state]


@dataclass(frozen=True)
class MarketDataRequirement:
    """
    What a decision needs from its inputs, independent of what it gets.

    Kept separate from `DataFreshness` deliberately: one describes the
    data, the other describes the decision. The comparison between them
    is a policy question and lives in `src/adaptive/freshness.py`.
    """
    state: MarketDataState
    max_age_seconds: Optional[float] = None
    requires_realtime_marker: bool = False
    permits_delayed: bool = True
    purpose: str = ""

    def __post_init__(self):
        _require_non_negative(self.max_age_seconds, "max_age_seconds")
        if self.state is MarketDataState.HOT and self.permits_delayed:
            # HOT exists precisely to mean "delayed is not good enough".
            raise ValueError("a HOT requirement cannot permit delayed data")


@dataclass(frozen=True)
class MarketDataCostEstimate:
    """
    What a plan would cost, under whatever cost model is configured.

    No provider's pricing is assumed here; see
    `src/adaptive/cost_governor.py`. This is the shape of an answer,
    not a claim about anyone's price list.
    """
    instrument_count: int
    requests: int = 0
    snapshots: int = 0
    fixed_cost_usd: float = 0.0
    variable_cost_usd: float = 0.0
    model_version: str = ""
    assumptions: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.instrument_count < 0:
            raise ValueError("instrument_count must be >= 0")
        if self.requests < 0 or self.snapshots < 0:
            raise ValueError("requests and snapshots must be >= 0")
        _require_non_negative(self.fixed_cost_usd, "fixed_cost_usd", allow_none=False)
        _require_non_negative(self.variable_cost_usd, "variable_cost_usd",
                              allow_none=False)

    @property
    def total_usd(self) -> float:
        return round(self.fixed_cost_usd + self.variable_cost_usd, 6)


class BudgetPolicy(str, Enum):
    """What a governor says about spending more."""
    ALLOW = "allow"
    WARN = "warn"
    THROTTLE = "throttle"
    BLOCK = "block"


@dataclass(frozen=True)
class MarketDataBudgetState:
    """Where the month stands. Deterministic; no clock of its own."""
    month: str                                  # "YYYY-MM"
    budget_usd: float
    spent_usd: float = 0.0
    committed_usd: float = 0.0
    projected_month_end_usd: Optional[float] = None
    policy: BudgetPolicy = BudgetPolicy.ALLOW
    as_of: Optional[datetime] = None

    def __post_init__(self):
        _require_non_negative(self.budget_usd, "budget_usd", allow_none=False)
        _require_non_negative(self.spent_usd, "spent_usd", allow_none=False)
        _require_non_negative(self.committed_usd, "committed_usd", allow_none=False)
        _require_non_negative(self.projected_month_end_usd, "projected_month_end_usd")
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))

    @property
    def remaining_usd(self) -> float:
        """Never negative: an overspend is reported as zero remaining."""
        return round(max(0.0, self.budget_usd - self.spent_usd - self.committed_usd), 6)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_usd <= 0.0


# ======================================================================
# Capital allocation
# ======================================================================


class AllocationRejectionReason(str, Enum):
    BELOW_MINIMUM_SIZE = "below_minimum_size"
    SECTOR_LIMIT = "sector_limit"
    CORRELATION_LIMIT = "correlation_limit"
    SINGLE_POSITION_LIMIT = "single_position_limit"
    ACCOUNT_RISK_LIMIT = "account_risk_limit"
    INSUFFICIENT_EXPECTED_EDGE = "insufficient_expected_edge"
    EXECUTION_COST_EXCEEDS_EDGE = "execution_cost_exceeds_edge"
    MARKET_DATA_COST_NOT_JUSTIFIED = "market_data_cost_not_justified"
    CASH_RESERVED = "cash_reserved"
    STALE_INPUTS = "stale_inputs"


@dataclass(frozen=True)
class AllocationConstraint:
    """One limit the allocator must respect, named so it can be cited."""
    name: str
    scope: str                                   # "portfolio" | "sector" | "position"
    limit: float
    unit: str = "fraction"                       # "fraction" | "usd" | "count"
    note: str = ""

    def __post_init__(self):
        if not self.name:
            raise ValueError("constraint name is required")
        _require_finite(self.limit, "limit", allow_none=False)
        if self.unit == "fraction":
            _require_fraction(self.limit, "limit", allow_none=False)


@dataclass(frozen=True)
class CashReserve:
    """
    Cash held back on purpose.

    A first-class line, not a residual. The distinction matters because
    "what is left over" cannot be reviewed, while "what was reserved,
    and why" can.
    """
    amount_usd: float
    reason: str = ""
    released_at: Optional[datetime] = None

    def __post_init__(self):
        _require_non_negative(self.amount_usd, "amount_usd", allow_none=False)
        object.__setattr__(self, "released_at",
                           require_utc(self.released_at, "released_at"))


@dataclass(frozen=True)
class RiskBudget:
    """How much risk, not how much money, a decision is allowed to use."""
    total_risk_usd: float
    allocated_risk_usd: float = 0.0
    method_version: str = ""

    def __post_init__(self):
        _require_non_negative(self.total_risk_usd, "total_risk_usd", allow_none=False)
        _require_non_negative(self.allocated_risk_usd, "allocated_risk_usd",
                              allow_none=False)
        if self.allocated_risk_usd > self.total_risk_usd:
            raise ValueError("allocated risk exceeds the risk budget")

    @property
    def remaining_risk_usd(self) -> float:
        return round(self.total_risk_usd - self.allocated_risk_usd, 6)


@dataclass(frozen=True)
class CapitalAllocationDecision:
    """One instrument's share. Zero is a real answer, not a failure."""
    instrument_id: str
    amount_usd: float
    rejection_reasons: Tuple[AllocationRejectionReason, ...] = ()
    score: Optional[float] = None
    reasons: Tuple[str, ...] = ()
    constraints_applied: Tuple[str, ...] = ()

    def __post_init__(self):
        _require_non_negative(self.amount_usd, "amount_usd", allow_none=False)
        _require_finite(self.score, "score")

    @property
    def is_funded(self) -> bool:
        return self.amount_usd > 0.0


@dataclass(frozen=True)
class CapitalAllocationProposal:
    """
    A whole proposal: what each instrument gets, and what stays in cash.

    THE INVARIANT, AND THE ONE THAT IS DELIBERATELY ABSENT. Allocations
    plus reserved cash may not exceed the capital available — spending
    money that is not there is a bug. There is NO rule that they must
    equal it. A proposal that deploys 43 of 100 and reserves the rest is
    valid, and so is one that deploys nothing at all.
    """
    proposal_id: str
    as_of: datetime
    capital_available_usd: float
    decisions: Tuple[CapitalAllocationDecision, ...] = ()
    cash_reserves: Tuple[CashReserve, ...] = ()
    constraints: Tuple[AllocationConstraint, ...] = ()
    method_version: str = ""
    lineage_id: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "as_of", require_utc(self.as_of, "as_of"))
        _require_non_negative(self.capital_available_usd, "capital_available_usd",
                              allow_none=False)
        seen = [d.instrument_id for d in self.decisions]
        if len(set(seen)) != len(seen):
            raise ValueError("an instrument may appear only once in a proposal")
        committed = self.deployed_usd + self.reserved_usd
        if committed > self.capital_available_usd + 1e-9:
            raise ValueError(
                f"allocations plus reserves ({committed}) exceed capital available "
                f"({self.capital_available_usd})")

    @property
    def deployed_usd(self) -> float:
        return round(sum(d.amount_usd for d in self.decisions), 6)

    @property
    def reserved_usd(self) -> float:
        return round(sum(r.amount_usd for r in self.cash_reserves), 6)

    @property
    def uncommitted_usd(self) -> float:
        """Neither deployed nor explicitly reserved. Also valid."""
        return round(self.capital_available_usd - self.deployed_usd
                     - self.reserved_usd, 6)

    @property
    def funded_count(self) -> int:
        return sum(1 for d in self.decisions if d.is_funded)


# ======================================================================
# Execution economics
# ======================================================================


@dataclass(frozen=True)
class ExecutionEconomics:
    """
    What an idea is worth after what it costs to act on it.

    Every field is supplied by a caller that measured or modelled it.
    Nothing is invented here, and there are no default costs: a zero
    that was never estimated and a zero that was measured must not look
    the same, so absent components are None and `expected_net_edge`
    refuses to pretend otherwise.
    """
    expected_gross_edge: Optional[float] = None
    spread_cost: Optional[float] = None
    slippage_estimate: Optional[float] = None
    commission_estimate: Optional[float] = None
    market_data_cost_estimate: Optional[float] = None
    other_execution_cost: Optional[float] = None
    currency: str = "USD"
    method_version: str = ""

    COST_FIELDS = ("spread_cost", "slippage_estimate", "commission_estimate",
                   "market_data_cost_estimate", "other_execution_cost")

    def __post_init__(self):
        _require_finite(self.expected_gross_edge, "expected_gross_edge")
        for name in self.COST_FIELDS:
            _require_non_negative(getattr(self, name), name)

    @property
    def total_cost(self) -> Optional[float]:
        values = [getattr(self, name) for name in self.COST_FIELDS]
        present = [v for v in values if v is not None]
        if not present:
            return None
        return round(sum(present), 10)

    @property
    def expected_net_edge(self) -> Optional[float]:
        """None when the gross edge is unknown: absence is not zero."""
        if self.expected_gross_edge is None:
            return None
        return round(self.expected_gross_edge - (self.total_cost or 0.0), 10)

    @property
    def is_complete(self) -> bool:
        """True only when every component was actually supplied."""
        return (self.expected_gross_edge is not None
                and all(getattr(self, n) is not None for n in self.COST_FIELDS))


# ======================================================================
# Decision lineage
# ======================================================================


@dataclass(frozen=True)
class DecisionLineage:
    """
    The thread from a universe decision to an outcome.

    Attribution in 25.9I-G has to answer "which step created the value",
    and it can only do that if every step recorded which step it came
    from. The ids are carried, never derived: a reconstruction after the
    fact is a guess wearing an audit's clothes.
    """
    lineage_id: str
    session_date: Optional[str] = None
    universe_decision_id: Optional[str] = None
    opportunity_assessment_id: Optional[str] = None
    admission_decision_id: Optional[str] = None
    allocation_proposal_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    order_intent_id: Optional[str] = None
    execution_order_id: Optional[str] = None
    position_thesis_id: Optional[str] = None
    thesis_revision_id: Optional[str] = None
    outcome_id: Optional[str] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.lineage_id:
            raise ValueError("lineage_id is required")
        object.__setattr__(self, "created_at",
                           require_utc(self.created_at, "created_at"))

    def links(self) -> Dict[str, str]:
        """Only the steps that actually happened."""
        return {name: value for name, value in self.__dict__.items()
                if name.endswith("_id") and name != "lineage_id" and value}

    def with_step(self, **steps: Optional[str]) -> "DecisionLineage":
        """A new lineage with further steps recorded; the original stands."""
        unknown = set(steps) - set(self.__dict__)
        if unknown:
            raise ValueError(f"unknown lineage steps: {sorted(unknown)}")
        current = dict(self.__dict__)
        current.update({k: v for k, v in steps.items() if v is not None})
        return DecisionLineage(**current)
