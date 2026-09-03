"""
src/execution/governance.py
--------------------------------
Execution levels, promotion gates, readiness and human approval
(Phase 16, spec §5, §7, §8, §9, §41, §82, §83).

WHAT THIS LAYER IS FOR
--------------------------
Deciding whether the system is ALLOWED to trade at a given level, and
recording who decided. It is separate from the safety switches
(`safety.py`) on purpose: those answer "is execution switched on right
now", this answers "has this system earned the right to operate at
this level at all".

The two fail differently and should be readable separately. A kill
switch is a moment; a promotion is a judgement about accumulated
evidence.

EIGHT LEVELS, AND THE ONE THAT DOES NOT EXIST
-------------------------------------------------
Levels 0 through 3 are implemented and reachable. Levels 4 through 7
describe real-money operation, and this repository has **no adapter
that can place a real-money order**. So they are:

  - fully specified, with gates that evaluate honestly
  - reachable in the GOVERNANCE model
  - unreachable in execution, because no venue adapter accepts them

That is deliberate and it is the safest arrangement available. The
gate machinery can be built, tested and argued about now, while the
irreversible step — an adapter that can lose money — remains a
separate, explicit decision nobody has taken.

WHY PROFITABILITY IS NOT A GATE
-----------------------------------
It is deliberately absent from `DEFAULT_GATES`. A strategy can be
profitable over a short paper period by luck, and promoting on that
basis is how capital gets committed to noise. What the gates measure
instead is whether the SYSTEM is trustworthy: does it reconcile, does
it recover, does it stay connected, does it execute at the prices it
expected. Those are properties a short sample CAN establish.

NOTHING PROMOTES ITSELF
---------------------------
`PromotionRequest` requires a human actor and an explicit approval,
and `ExecutionGovernor.effective_level` will not return a level above
`PAPER` without an approval that is APPROVED and unexpired. Spec §83
requires this and it is enforced in the type rather than by
convention.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.broker_models import ExecutionEnvironment, finite_or_none


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC")
    return value


class ExecutionLevel(int, Enum):
    """
    The staged path from research to production (spec §7).

    An int enum so levels compare and order naturally — "is the
    requested level above the approved one" is the question this model
    exists to answer, and it should be one comparison.
    """
    RESEARCH = 0
    BACKTEST = 1
    PAPER = 2
    BROKER_PAPER = 3          # IBKR paper, end to end
    LIVE_PREPARATION = 4      # controlled-live readiness, still no money
    MICRO_CAPITAL_LIVE = 5
    RESTRICTED_LIVE = 6
    PRODUCTION_LIVE = 7

    @property
    def is_real_money(self) -> bool:
        return self >= ExecutionLevel.MICRO_CAPITAL_LIVE

    @property
    def is_implemented(self) -> bool:
        """
        Whether an execution path for this level exists in the code.

        Levels 4+ are specified and gated but have no adapter that
        accepts them. Saying so here keeps the CLI and dashboard from
        presenting a level as available merely because the enum can
        name it.
        """
        return self <= ExecutionLevel.BROKER_PAPER

    @property
    def requires_approval(self) -> bool:
        """Anything past ordinary paper needs a human on the record."""
        return self > ExecutionLevel.PAPER

    @property
    def label(self) -> str:
        return {
            ExecutionLevel.RESEARCH: "Research",
            ExecutionLevel.BACKTEST: "Backtest",
            ExecutionLevel.PAPER: "Paper",
            ExecutionLevel.BROKER_PAPER: "IBKR paper, end to end",
            ExecutionLevel.LIVE_PREPARATION: "Controlled-live preparation",
            ExecutionLevel.MICRO_CAPITAL_LIVE: "Micro-capital live",
            ExecutionLevel.RESTRICTED_LIVE: "Restricted live",
            ExecutionLevel.PRODUCTION_LIVE: "Production live",
        }[self]

    @property
    def environment(self) -> ExecutionEnvironment:
        """The execution environment this level implies."""
        if self <= ExecutionLevel.BACKTEST:
            return ExecutionEnvironment.SIMULATION
        if self <= ExecutionLevel.BROKER_PAPER:
            return ExecutionEnvironment.PAPER
        if self is ExecutionLevel.LIVE_PREPARATION:
            return ExecutionEnvironment.DEMO
        return ExecutionEnvironment.LIVE


class ReadinessCategory(str, Enum):
    """The eleven things that must be sound before capital moves (§9)."""
    MODEL = "model"
    SIGNAL = "signal"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    EXECUTION = "execution"
    BROKER = "broker"
    DATA = "data"
    SYSTEM = "system"
    SECURITY = "security"
    RECONCILIATION = "reconciliation"
    OPERATIONS = "operations"


class ReadinessVerdict(str, Enum):
    PASS = "pass"
    CONDITIONAL = "conditional"
    FAIL = "fail"
    UNKNOWN = "unknown"

    @property
    def blocks_promotion(self) -> bool:
        """
        UNKNOWN blocks, deliberately.

        "We did not measure it" is not evidence of soundness, and a
        readiness model that treated absence as a pass would give its
        highest scores to the least-instrumented systems.
        """
        return self in (ReadinessVerdict.FAIL, ReadinessVerdict.UNKNOWN)


class ApprovalState(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class GateResult:
    """One promotion criterion, measured."""
    gate_id: str
    description: str
    passed: bool
    observed: Optional[float] = None
    threshold: Optional[float] = None
    detail: str = ""
    #: False when the gate could not be measured at all. Distinct from
    #: failing: unmeasured means the evidence is missing, and that
    #: blocks promotion just as a failure does.
    measured: bool = True

    @property
    def blocks(self) -> bool:
        return not self.passed or not self.measured

    def explain(self) -> str:
        if not self.measured:
            return f"{self.description}: not measured ({self.detail or 'no data'})"
        if self.passed:
            return f"{self.description}: pass"
        return (f"{self.description}: FAIL "
                f"(observed {self.observed}, requires {self.threshold})"
                + (f" — {self.detail}" if self.detail else ""))


@dataclass
class PromotionGate:
    """
    One measurable criterion for moving up a level.

    `direction` says which way is good: `min` means the observation
    must be at least the threshold, `max` means at most. Written out
    rather than inferred from the name, because a gate called
    `max_drawdown` compared the wrong way round would pass exactly the
    systems it exists to stop.
    """
    gate_id: str
    description: str
    metric: str
    threshold: float
    direction: str = "min"
    #: The lowest level this gate applies from.
    applies_from: ExecutionLevel = ExecutionLevel.LIVE_PREPARATION
    required: bool = True

    def evaluate(self, metrics: Dict[str, Any]) -> GateResult:
        raw = metrics.get(self.metric)
        observed = finite_or_none(raw) if raw is not None else None
        if observed is None:
            return GateResult(
                self.gate_id, self.description, passed=False, measured=False,
                threshold=self.threshold,
                detail=f"metric {self.metric!r} was not supplied")
        passed = (observed >= self.threshold if self.direction == "min"
                  else observed <= self.threshold)
        return GateResult(self.gate_id, self.description, passed=passed,
                          observed=observed, threshold=self.threshold)


#: The default promotion criteria (spec §8).
#:
#: Note what is NOT here: profitability, return, Sharpe. A short paper
#: period cannot distinguish skill from luck, and promoting on that
#: basis commits capital to noise. These measure whether the SYSTEM is
#: trustworthy — properties a short sample genuinely can establish.
DEFAULT_GATES: Tuple[PromotionGate, ...] = (
    PromotionGate("paper_days", "Paper trading duration",
                  "paper_days", 30.0, "min"),
    PromotionGate("paper_trades", "Completed paper trades",
                  "paper_trades", 30.0, "min"),
    PromotionGate("max_drawdown", "Maximum drawdown within limit",
                  "max_drawdown_pct", 0.20, "max"),
    PromotionGate("daily_loss", "Worst daily loss within limit",
                  "worst_daily_loss_pct", 0.05, "max"),
    PromotionGate("concentration", "Position concentration within limit",
                  "max_position_weight", 0.25, "max"),
    PromotionGate("leverage", "Leverage within limit",
                  "max_leverage", 1.0, "max"),
    PromotionGate("execution_errors", "Execution error rate",
                  "execution_error_rate", 0.02, "max"),
    PromotionGate("slippage", "Median slippage within tolerance (bps)",
                  "median_slippage_bps", 25.0, "max"),
    PromotionGate("rejection_rate", "Broker rejection rate",
                  "rejection_rate", 0.10, "max"),
    PromotionGate("reconciliation", "Reconciliation mismatch rate",
                  "reconciliation_mismatch_rate", 0.01, "max"),
    PromotionGate("unknown_states", "Unresolved unknown-order rate",
                  "unknown_state_rate", 0.0, "max"),
    PromotionGate("uptime", "Broker session uptime",
                  "broker_uptime", 0.95, "min"),
    PromotionGate("signal_stability", "Signal generation stability",
                  "signal_stability", 0.70, "min"),
    PromotionGate("model_stability", "Model prediction stability",
                  "model_stability", 0.70, "min"),
)


@dataclass
class ReadinessAssessment:
    """
    The eleven-category readiness view (spec §9).

    `is_ready` requires every category to pass. There is deliberately
    no weighted score that could let a strong execution record
    compensate for a failing security review — those are not
    substitutable, and a single number would imply they are.
    """
    at: Optional[datetime] = None
    verdicts: Dict[ReadinessCategory, ReadinessVerdict] = field(default_factory=dict)
    notes: Dict[ReadinessCategory, str] = field(default_factory=dict)

    def __post_init__(self):
        self.at = _require_utc(self.at, "at")
        for category in ReadinessCategory:
            self.verdicts.setdefault(category, ReadinessVerdict.UNKNOWN)

    def record(self, category: ReadinessCategory, verdict: ReadinessVerdict,
               note: str = "") -> None:
        self.verdicts[category] = verdict
        if note:
            self.notes[category] = note

    @property
    def blocking(self) -> List[ReadinessCategory]:
        return [c for c, v in self.verdicts.items() if v.blocks_promotion]

    @property
    def conditional(self) -> List[ReadinessCategory]:
        return [c for c, v in self.verdicts.items()
                if v is ReadinessVerdict.CONDITIONAL]

    @property
    def is_ready(self) -> bool:
        return not self.blocking and not self.conditional

    def render(self) -> str:
        width = max(len(c.value) for c in ReadinessCategory)
        lines = []
        for category in ReadinessCategory:
            verdict = self.verdicts[category]
            note = self.notes.get(category, "")
            lines.append(f"{category.value.upper():<{width}}  "
                         f"{verdict.value.upper()}"
                         + (f"   {note}" if note else ""))
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "verdicts": {c.value: v.value for c, v in self.verdicts.items()},
            "notes": {c.value: n for c, n in self.notes.items()},
            "blocking": [c.value for c in self.blocking],
            "conditional": [c.value for c in self.conditional],
            "is_ready": self.is_ready,
        }


@dataclass
class PromotionRequest:
    """
    A request to operate at a level, and its approval (spec §41).

    Both `requested_by` and `approved_by` are required at their
    respective moments, and they are compared: the same actor cannot
    approve their own request. That is the cheapest possible form of
    four-eyes control, and it is worth having even in a single-operator
    system because it forces the approval to be a deliberate second
    act rather than a continuation of the first.
    """
    request_id: str
    level: ExecutionLevel
    requested_by: str
    requested_at: Optional[datetime]
    reason: str = ""
    state: ApprovalState = ApprovalState.REQUESTED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    decision_note: str = ""
    #: Snapshot of the evidence at approval time, so a later reader can
    #: see what the decision was actually based on.
    gate_snapshot: Dict[str, Any] = field(default_factory=dict)
    readiness_snapshot: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("requested_at", "approved_at", "expires_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))
        if not self.requested_by:
            raise ValueError("a promotion request must name who requested it")

    def is_active(self, now: datetime) -> bool:
        if self.state is not ApprovalState.APPROVED:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True

    def approve(self, actor: str, now: datetime,
                ttl: Optional[timedelta] = None, note: str = "") -> None:
        if not actor:
            raise ValueError("approval requires an actor")
        if actor == self.requested_by:
            raise ValueError(
                "the actor who requested a promotion may not approve it; "
                "approval must be a separate, deliberate act")
        if self.state is not ApprovalState.REQUESTED:
            raise ValueError(
                f"only a REQUESTED promotion can be approved "
                f"(this one is {self.state.value})")
        self.state = ApprovalState.APPROVED
        self.approved_by = actor
        self.approved_at = _require_utc(now, "now")
        self.decision_note = note
        # Approvals expire by default. A standing permission to trade
        # real money that nobody has to renew is how a temporary
        # decision becomes permanent by inattention.
        self.expires_at = now + (ttl if ttl is not None else timedelta(days=1))

    def reject(self, actor: str, now: datetime, note: str = "") -> None:
        if not actor:
            raise ValueError("rejection requires an actor")
        self.state = ApprovalState.REJECTED
        self.approved_by = actor
        self.approved_at = _require_utc(now, "now")
        self.decision_note = note

    def revoke(self, actor: str, now: datetime, note: str = "") -> None:
        """Withdraw an approval that has already been granted."""
        if not actor:
            raise ValueError("revocation requires an actor")
        self.state = ApprovalState.REVOKED
        self.decision_note = note
        self.approved_at = _require_utc(now, "now")


@dataclass
class PromotionEvaluation:
    """The full answer to 'may we operate at this level'."""
    level: ExecutionLevel
    at: Optional[datetime]
    gates: List[GateResult] = field(default_factory=list)
    readiness: Optional[ReadinessAssessment] = None
    approval: Optional[PromotionRequest] = None
    #: Set when the level itself has no execution path in this code.
    unimplemented: bool = False

    @property
    def failing_gates(self) -> List[GateResult]:
        return [g for g in self.gates if g.blocks]

    @property
    def gates_pass(self) -> bool:
        return not self.failing_gates

    @property
    def approved(self) -> bool:
        if not self.level.requires_approval:
            return True
        return (self.approval is not None
                and self.at is not None
                and self.approval.is_active(self.at)
                and self.approval.level >= self.level)

    @property
    def permitted(self) -> bool:
        """
        Every condition, together. Never a majority.

        An unimplemented level can satisfy every gate and still be
        refused, because gates measure evidence and implementation is
        a fact about the code.
        """
        if self.unimplemented:
            return False
        if not self.gates_pass:
            return False
        if self.readiness is not None and not self.readiness.is_ready:
            return False
        return self.approved

    def explain(self) -> str:
        if self.permitted:
            return f"Level {int(self.level)} ({self.level.label}) is permitted."
        reasons: List[str] = []
        if self.unimplemented:
            reasons.append(
                f"level {int(self.level)} has no execution path in this "
                f"repository — no adapter accepts a real-money environment")
        for gate in self.failing_gates:
            reasons.append(gate.explain())
        if self.readiness is not None and self.readiness.blocking:
            reasons.append("readiness blocked on: "
                           + ", ".join(c.value for c in self.readiness.blocking))
        if self.readiness is not None and self.readiness.conditional:
            reasons.append("readiness conditional on: "
                           + ", ".join(c.value for c in self.readiness.conditional))
        if not self.approved:
            if self.approval is None:
                reasons.append("no human approval exists for this level")
            elif self.approval.state is not ApprovalState.APPROVED:
                reasons.append(f"the approval is {self.approval.state.value}")
            else:
                reasons.append("the approval has expired")
        return " | ".join(reasons)


class ExecutionGovernor:
    """
    Holds the approved level and evaluates promotion.

    The single question everything else asks it: `permits(level)`.
    """

    def __init__(self, gates: Sequence[PromotionGate] = DEFAULT_GATES,
                 baseline: ExecutionLevel = ExecutionLevel.PAPER):
        self.gates = list(gates)
        #: The highest level reachable without an approval. Paper by
        #: default, because paper needs no permission to be safe.
        self.baseline = baseline
        self.requests: Dict[str, PromotionRequest] = {}

    # ---------------- approvals ----------------

    def request(self, level: ExecutionLevel, actor: str, now: datetime,
                reason: str = "") -> PromotionRequest:
        request = PromotionRequest(
            request_id=f"promo-{uuid.uuid4().hex[:16]}", level=level,
            requested_by=actor, requested_at=now, reason=reason)
        self.requests[request.request_id] = request
        return request

    def active_approval(self, now: datetime) -> Optional[PromotionRequest]:
        """The highest currently-valid approval, if any."""
        active = [r for r in self.requests.values() if r.is_active(now)]
        if not active:
            return None
        return max(active, key=lambda r: int(r.level))

    def effective_level(self, now: datetime) -> ExecutionLevel:
        """
        The level the system may actually operate at right now.

        Never above `baseline` without a live approval, and never
        above an unimplemented boundary — so this cannot return a
        real-money level in a repository that has no real-money
        adapter.
        """
        approval = self.active_approval(now)
        level = self.baseline if approval is None else max(self.baseline,
                                                           approval.level)
        while not level.is_implemented and level > ExecutionLevel.RESEARCH:
            level = ExecutionLevel(int(level) - 1)
        return level

    # ---------------- evaluation ----------------

    def evaluate(self, level: ExecutionLevel, now: datetime,
                 metrics: Optional[Dict[str, Any]] = None,
                 readiness: Optional[ReadinessAssessment] = None
                 ) -> PromotionEvaluation:
        metrics = metrics or {}
        applicable = [g for g in self.gates if level >= g.applies_from]
        results = [g.evaluate(metrics) for g in applicable]
        return PromotionEvaluation(
            level=level, at=now, gates=results, readiness=readiness,
            approval=self.active_approval(now),
            unimplemented=not level.is_implemented)

    def permits(self, level: ExecutionLevel, now: datetime,
                metrics: Optional[Dict[str, Any]] = None,
                readiness: Optional[ReadinessAssessment] = None) -> bool:
        return self.evaluate(level, now, metrics, readiness).permitted

    def state(self, now: datetime) -> Dict[str, Any]:
        approval = self.active_approval(now)
        return {
            "baseline_level": int(self.baseline),
            "effective_level": int(self.effective_level(now)),
            "effective_label": self.effective_level(now).label,
            "active_approval": (approval.request_id if approval else None),
            "approved_level": (int(approval.level) if approval else None),
            "approved_by": (approval.approved_by if approval else None),
            "approval_expires_at": (approval.expires_at.isoformat()
                                    if approval and approval.expires_at else None),
            "levels": [
                {"level": int(l), "label": l.label,
                 "implemented": l.is_implemented,
                 "real_money": l.is_real_money,
                 "requires_approval": l.requires_approval}
                for l in ExecutionLevel],
            "real_money_reachable": False,
        }


def assess_readiness(now: datetime, *,
                     execution: Optional[Dict[str, Any]] = None,
                     broker: Optional[Dict[str, Any]] = None,
                     reconciliation: Optional[Dict[str, Any]] = None,
                     data: Optional[Dict[str, Any]] = None,
                     risk: Optional[Dict[str, Any]] = None,
                     security_reviewed: bool = False,
                     operations_ready: bool = False) -> ReadinessAssessment:
    """
    Build a readiness assessment from observed state.

    Anything not supplied stays UNKNOWN — which blocks. That is the
    point: a readiness model that treated silence as a pass would give
    its best scores to the least-instrumented systems.
    """
    assessment = ReadinessAssessment(at=now)

    def verdict(ok: Optional[bool], note: str = "") -> Tuple[ReadinessVerdict, str]:
        if ok is None:
            return ReadinessVerdict.UNKNOWN, note or "not measured"
        return (ReadinessVerdict.PASS if ok else ReadinessVerdict.FAIL), note

    if execution is not None:
        errors = execution.get("error_rate")
        ok = errors is not None and errors <= 0.02
        assessment.record(ReadinessCategory.EXECUTION, *verdict(
            ok, f"error rate {errors}" if errors is not None else ""))

    if broker is not None:
        connected = broker.get("connected")
        assessment.record(ReadinessCategory.BROKER, *verdict(
            connected, broker.get("detail", "")))

    if reconciliation is not None:
        clean = reconciliation.get("clean")
        assessment.record(ReadinessCategory.RECONCILIATION, *verdict(
            clean, reconciliation.get("detail", "")))

    if data is not None:
        fresh = data.get("fresh")
        assessment.record(ReadinessCategory.DATA, *verdict(
            fresh, data.get("detail", "")))

    if risk is not None:
        healthy = risk.get("healthy")
        assessment.record(ReadinessCategory.RISK, *verdict(
            healthy, risk.get("detail", "")))

    assessment.record(
        ReadinessCategory.SECURITY,
        ReadinessVerdict.PASS if security_reviewed else ReadinessVerdict.UNKNOWN,
        "audited this phase" if security_reviewed else "no audit recorded")
    assessment.record(
        ReadinessCategory.OPERATIONS,
        ReadinessVerdict.PASS if operations_ready else ReadinessVerdict.CONDITIONAL,
        "runbook present" if operations_ready
        else "operator procedures not confirmed")

    return assessment
