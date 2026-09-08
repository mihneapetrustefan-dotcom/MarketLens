"""
src/domain/challenger_models.py
-----------------------------------------
Phase 24 — the vocabulary of a fair comparison.

WHAT A CHALLENGER IS, AND WHAT A CANDIDATE IS NOT
-----------------------------------------------------
A Phase 23 **candidate** is a promising research idea: one experiment,
one chronological split, one held-out window, and a review note listing
why to be sceptical.

A Phase 24 **challenger** is that idea turned into a fully specified,
independently versioned implementation put through a harder test
against a *named, versioned baseline*: walk-forward rather than a
single split, robustness across time and instrument and horizon,
sensitivity around its parameter, complexity measured against the
baseline's, and economic significance.

Not every candidate deserves one (§3). `validate_candidate` refuses the
ones whose evidence cannot support the extra work.

THE SCORECARD IS NOT A SCORE
--------------------------------
§37 is explicit: do not collapse everything into one profitability
number. `Scorecard` therefore has six named dimensions and **no total**.
There is deliberately no `overall` property and no `__lt__`, because
the moment a challenger can be sorted, somebody sorts a hundred of them
and reads the top row — which is the winner's-curse failure this phase
exists to prevent.

CONTEXT DEPENDENCE IS A RESULT, NOT A FAILURE
-------------------------------------------------
§39: a challenger may beat the baseline in one regime and lose in
another. `ChallengerDecision.CONTEXT_DEPENDENT` exists so that outcome
survives, instead of being averaged into a global number that describes
neither case.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Bumped when the meaning of a stored challenger row changes. Rows
#: carry it so two methodologies coexist rather than overwrite (§68).
CHALLENGER_METHOD_VERSION = "v1"

from src.domain.model_models import ModelEvaluation  # noqa: E402

#: The one sample threshold this project has used since Phase 9,
#: imported rather than restated so the layers cannot disagree.
MIN_CHALLENGER_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


# ======================================================================
# Enumerations
# ======================================================================

class VariantType(str, Enum):
    """
    §5. Declared in full; implemented where the infrastructure exists.

    Only `SIGNAL` can currently be evaluated end to end, because the
    registered evaluators are signal-level. The rest are registered so
    the gap is visible and named rather than silently missing — the
    pattern Phases 22 and 23 both settled on.
    """
    MODEL = "model"
    FEATURE = "feature"
    SIGNAL = "signal"
    STRATEGY = "strategy"
    REGIME = "regime"
    PORTFOLIO = "portfolio"
    RISK = "risk"
    EXECUTION = "execution"


class BaselineKind(str, Enum):
    """§6. What a challenger is being compared against."""
    ACTIVE_MODEL = "active_model"
    CURRENT_STRATEGY = "current_strategy"
    CURRENT_SIGNAL_RULE = "current_signal_rule"
    CURRENT_PORTFOLIO_RULE = "current_portfolio_rule"
    CURRENT_RISK_POLICY = "current_risk_policy"


class ChallengerStatus(str, Enum):
    """
    §33. `PAPER_CANDIDATE` is the furthest this phase can reach.

    There is no PRODUCTION or ACTIVE member, and no transition into
    one. Phase 25 may add paper execution; production remains a
    separate human decision under the Phase 18 gate.
    """
    PROPOSED = "proposed"
    VALIDATING = "validating"
    TESTING = "testing"
    PROMISING = "promising"
    PAPER_CANDIDATE = "paper_candidate"
    REQUIRES_REVIEW = "requires_review"
    REJECTED = "rejected"
    RETIRED = "retired"

    @property
    def is_started(self) -> bool:
        """Once started, the definition is frozen (§10)."""
        return self is not ChallengerStatus.PROPOSED

    @property
    def is_terminal(self) -> bool:
        return self in (ChallengerStatus.REJECTED, ChallengerStatus.RETIRED,
                        ChallengerStatus.PAPER_CANDIDATE)


class RunEnvironment(str, Enum):
    """§31. Never confused, never defaulted to the permissive one."""
    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChallengerDecision(str, Enum):
    """
    §38. Five outcomes, and only one of them says the change is better.

    `SUPERIOR` is reachable only through `decide()`, which requires
    every dimension to hold — never by a metric moving pleasantly.
    """
    SUPERIOR = "superior"
    INFERIOR = "inferior"
    INCONCLUSIVE = "inconclusive"
    CONTEXT_DEPENDENT = "context_dependent"
    REQUIRES_REVIEW = "requires_review"


class ReviewOutcome(str, Enum):
    APPROVED_FOR_PAPER = "approved_for_paper"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class Actor(str, Enum):
    HUMAN = "human"
    SYSTEM = "system"


# ======================================================================
# Baseline
# ======================================================================

@dataclass
class BaselineSpec:
    """
    The thing a challenger must beat, pinned so it cannot move (§6).

    `version` is required. A baseline that changes silently during
    evaluation turns a comparison into an anecdote, and the cheapest
    defence is to make the version part of the challenger's identity —
    so a changed baseline is a different challenger rather than the
    same one with different numbers.
    """
    kind: BaselineKind
    name: str
    version: str
    evaluator: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    complexity: int = 1

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("a baseline must be named")
        if not self.version.strip():
            raise ValueError(
                "a baseline must be versioned (§6). An unversioned baseline "
                "can change under a running comparison and nobody would be "
                "able to tell from the result.")

    def identity(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "name": self.name,
                "version": self.version, "evaluator": self.evaluator,
                "parameters": self.parameters, "complexity": self.complexity}

    def as_dict(self) -> Dict[str, Any]:
        return self.identity()


# ======================================================================
# Change definition
# ======================================================================

#: §8. A challenger must answer "what changed?" in terms the system can
#: check, not in prose. Each kind names a shape the evaluators or the
#: backtester can actually express.
CHANGE_KINDS = (
    "feature_added", "feature_removed", "threshold_changed",
    "model_family_changed", "horizon_changed", "regime_filter_added",
    "signal_filter_added", "position_sizing_changed",
    "execution_policy_changed",
)


@dataclass
class ChangeDefinition:
    """
    Exactly what this challenger does differently (§8).

    `kind` is constrained and `parameters` is a plain dict of values —
    never code, never a callable, never a string to be executed (§72).
    A vague variant is refused: "improved signal logic" is not a change
    definition, it is a hope.
    """
    kind: str
    summary: str
    evaluator: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    complexity: int = 1

    def validate(self) -> None:
        if self.kind not in CHANGE_KINDS:
            raise ValueError(
                "change kind %r is not one of the defined shapes: %s. A "
                "variant the system cannot express is a variant it cannot "
                "test fairly." % (self.kind, ", ".join(CHANGE_KINDS)))
        if not self.summary.strip():
            raise ValueError("a change definition must say what changed")
        if not self.evaluator and not (self.added or self.removed
                                       or self.parameters):
            raise ValueError(
                "a challenger that names no evaluator, no parameter and no "
                "added or removed component does not describe a change")

    def identity(self) -> Dict[str, Any]:
        return {"kind": self.kind, "evaluator": self.evaluator,
                "parameters": self.parameters,
                "added": sorted(self.added), "removed": sorted(self.removed),
                "complexity": self.complexity}

    def as_dict(self) -> Dict[str, Any]:
        data = self.identity()
        data["summary"] = self.summary
        return data


# ======================================================================
# Evaluation protocol and limits
# ======================================================================

@dataclass
class EvaluationPlan:
    """
    How the comparison will be judged — fixed before it runs (§10).

    Part of the fingerprint, so a started challenger cannot have its
    protocol relaxed after the numbers are visible. Same discipline as
    Phase 22's acceptance criteria, for the same reason.
    """
    holdout_fraction: float = 0.3
    walk_forward: bool = True
    walk_forward_folds: int = 3
    robustness_slices: int = 3
    sensitivity_values: List[Any] = field(default_factory=list)
    bootstrap_iterations: int = 2000
    seed: int = 20260907

    #: What must hold for SUPERIOR. Fixed in advance (§38).
    min_effect: float = 0.02
    min_sample: int = MIN_CHALLENGER_SAMPLE
    min_robust_fraction: float = 0.6
    max_complexity_ratio: float = 5.0
    require_interval_excludes_zero: bool = True
    min_economic_effect: float = 0.01

    def validate(self) -> None:
        if not 0.05 <= self.holdout_fraction <= 0.6:
            raise ValueError("holdout fraction must lie between 0.05 and 0.6")
        if self.min_effect <= 0:
            raise ValueError(
                "a minimum effect of zero makes every outcome a success")
        if self.min_sample < MIN_CHALLENGER_SAMPLE:
            raise ValueError(
                "minimum sample below %d contradicts every other layer"
                % MIN_CHALLENGER_SAMPLE)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "holdout_fraction": self.holdout_fraction,
            "walk_forward": self.walk_forward,
            "walk_forward_folds": self.walk_forward_folds,
            "robustness_slices": self.robustness_slices,
            "sensitivity_values": list(self.sensitivity_values),
            "bootstrap_iterations": self.bootstrap_iterations,
            "seed": self.seed, "min_effect": self.min_effect,
            "min_sample": self.min_sample,
            "min_robust_fraction": self.min_robust_fraction,
            "max_complexity_ratio": self.max_complexity_ratio,
            "require_interval_excludes_zero":
                self.require_interval_excludes_zero,
            "min_economic_effect": self.min_economic_effect,
        }


@dataclass
class ChallengerLimits:
    """§50, §51. Refusals, never silent truncation."""
    max_runs_per_challenger: int = 10
    max_challengers_per_family: int = 8
    max_variants: int = 12
    max_rows: int = 200000
    max_runtime_seconds: float = 900.0
    max_concurrent_jobs: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_runs_per_challenger": self.max_runs_per_challenger,
            "max_challengers_per_family": self.max_challengers_per_family,
            "max_variants": self.max_variants, "max_rows": self.max_rows,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_concurrent_jobs": self.max_concurrent_jobs,
        }


class LimitExceeded(Exception):
    """A limit was reached. Refused with a reason."""


class ChallengerChanged(Exception):
    """
    The definition moved after evaluation began (§9, §10).

    The most important refusal in this phase. A challenger whose
    baseline, dataset or parameters can change once numbers exist is
    not a comparison.
    """


# ======================================================================
# Challenger
# ======================================================================

@dataclass
class Challenger:
    """A versioned, independently testable competitor to a baseline."""
    challenger_id: str
    version: int
    variant_type: VariantType
    name: str
    baseline: BaselineSpec
    change: ChangeDefinition
    plan: EvaluationPlan

    # Provenance (§7). None of these is optional: a challenger with no
    # candidate behind it is an idea somebody had, and the whole point
    # of the chain is that every link resolves.
    candidate_id: str = ""
    hypothesis_id: str = ""
    experiment_id: str = ""
    conclusion_id: str = ""
    family_id: str = ""

    # Versions (§68).
    dataset_cutoff: str = ""
    dataset_version: str = ""
    feature_version: str = ""
    label_version: str = ""
    model_version: str = ""
    strategy_version: str = ""
    code_version: str = ""
    method_version: str = CHALLENGER_METHOD_VERSION

    status: ChallengerStatus = ChallengerStatus.PROPOSED
    experimental_basis: bool = True
    notes: List[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("a challenger must be named")
        self.baseline.validate()
        self.change.validate()
        self.plan.validate()
        missing = [field_name for field_name in
                   ("candidate_id", "hypothesis_id", "experiment_id",
                    "conclusion_id")
                   if not getattr(self, field_name)]
        if missing:
            raise ValueError(
                "a challenger must reference its %s (§7). An orphan "
                "challenger cannot be traced back to the evidence that "
                "justified it." % ", ".join(missing))

    @property
    def fingerprint(self) -> str:
        """
        Identity of the comparison: what is being compared, against
        what version, over how much record, judged how.

        The baseline version and the dataset cutoff are both inside it.
        A moved baseline or a grown record is a DIFFERENT challenger,
        not the same one with different numbers — the lesson Phase 23.5
        paid for when a grown dataset hashed identically and a stale
        cached result came back as current research.
        """
        return _digest({
            "variant_type": self.variant_type.value,
            "baseline": self.baseline.identity(),
            "change": self.change.identity(),
            "plan": self.plan.as_dict(),
            "dataset_cutoff": self.dataset_cutoff,
            "method_version": self.method_version,
        })

    @property
    def complexity_ratio(self) -> Optional[float]:
        if not self.baseline.complexity:
            return None
        return round(self.change.complexity / self.baseline.complexity, 4)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "challenger_id": self.challenger_id, "version": self.version,
            "variant_type": self.variant_type.value, "name": self.name,
            "baseline": self.baseline.as_dict(), "change": self.change.as_dict(),
            "plan": self.plan.as_dict(), "candidate_id": self.candidate_id,
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "conclusion_id": self.conclusion_id, "family_id": self.family_id,
            "dataset_cutoff": self.dataset_cutoff,
            "code_version": self.code_version,
            "method_version": self.method_version,
            "status": self.status.value,
            "experimental_basis": self.experimental_basis,
            "fingerprint": self.fingerprint,
            "complexity_ratio": self.complexity_ratio,
            "notes": self.notes, "created_by": self.created_by,
            "created_at": self.created_at,
        }


# ======================================================================
# Scorecard
# ======================================================================

@dataclass
class Dimension:
    """One axis of a comparison, with the reason it reads that way."""
    name: str
    value: Optional[float]
    verdict: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value,
                "verdict": self.verdict, "detail": self.detail}


@dataclass
class Scorecard:
    """
    Six dimensions, reported separately (§37).

    THERE IS DELIBERATELY NO TOTAL. No `overall`, no `__lt__`, no
    ordering. A scorecard that can be sorted gets sorted, and the top
    of a list of a hundred challengers is where the noise collects —
    which is the winner's curse this phase exists to prevent.

    A reader who wants to rank must decide the trade-off themselves,
    in the open.
    """
    performance: Dimension
    risk: Dimension
    robustness: Dimension
    stability: Dimension
    complexity: Dimension
    evidence: Dimension

    def dimensions(self) -> List[Dimension]:
        return [self.performance, self.risk, self.robustness,
                self.stability, self.complexity, self.evidence]

    def failing(self) -> List[str]:
        return [d.name for d in self.dimensions() if d.verdict == "worse"]

    def unknown(self) -> List[str]:
        """
        Dimensions that could not be measured at all.

        Kept separate from `failing` because they mean something
        different -- and treated as blocking anyway. The first version
        of `decide` only counted "worse", so a challenger reached
        SUPERIOR while its stability was never measured, and the
        reasons list printed "met: stability - walk-forward could not
        be run". An unmeasured dimension is weak evidence, and §38 says
        not to force SUPERIOR on weak evidence.
        """
        return [d.name for d in self.dimensions() if d.verdict == "unknown"]

    def as_dict(self) -> Dict[str, Any]:
        return {d.name: d.as_dict() for d in self.dimensions()}


# ======================================================================
# Result
# ======================================================================

@dataclass
class SliceResult:
    """One context — a time window, an instrument, a horizon."""
    kind: str
    label: str
    baseline_metric: Optional[float]
    challenger_metric: Optional[float]
    sample_size: int

    @property
    def effect(self) -> Optional[float]:
        if self.baseline_metric is None or self.challenger_metric is None:
            return None
        return self.challenger_metric - self.baseline_metric

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "label": self.label,
                "baseline_metric": self.baseline_metric,
                "challenger_metric": self.challenger_metric,
                "sample_size": self.sample_size, "effect": self.effect}


@dataclass
class ChallengerResult:
    """The comparison, its caveats and its verdict."""
    run_id: str
    challenger_id: str
    challenger_version: int
    metric: str = "directional_accuracy"

    baseline_out_of_sample: Dict[str, Any] = field(default_factory=dict)
    challenger_out_of_sample: Dict[str, Any] = field(default_factory=dict)
    effect: Optional[float] = None
    effect_in_sample: Optional[float] = None
    effect_low: Optional[float] = None
    effect_high: Optional[float] = None

    walk_forward_folds: int = 0
    walk_forward_folds_favourable: int = 0
    robust_slices: int = 0
    robust_slices_favourable: int = 0
    slices: List[SliceResult] = field(default_factory=list)
    sensitivity: Dict[str, Any] = field(default_factory=dict)

    complexity_ratio: Optional[float] = None
    economically_significant: Optional[bool] = None
    economic_note: str = ""

    family_challenger_count: int = 1
    family_run_count: int = 1
    window_reuse_count: int = 0
    warnings: List[str] = field(default_factory=list)

    scorecard: Optional[Scorecard] = None
    decision: ChallengerDecision = ChallengerDecision.INCONCLUSIVE
    reasons: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    computed_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.reasons:
            raise ValueError(
                "a challenger decision with no reasons is an assertion. "
                "Every check that produced it must be recorded, including "
                "the ones that passed.")

    @property
    def overfitting_gap(self) -> Optional[float]:
        if self.effect is None or self.effect_in_sample is None:
            return None
        return self.effect_in_sample - self.effect

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "challenger_id": self.challenger_id,
            "challenger_version": self.challenger_version,
            "metric": self.metric,
            "baseline_out_of_sample": self.baseline_out_of_sample,
            "challenger_out_of_sample": self.challenger_out_of_sample,
            "effect": self.effect, "effect_in_sample": self.effect_in_sample,
            "overfitting_gap": self.overfitting_gap,
            "effect_low": self.effect_low, "effect_high": self.effect_high,
            "walk_forward_folds": self.walk_forward_folds,
            "walk_forward_folds_favourable": self.walk_forward_folds_favourable,
            "robust_slices": self.robust_slices,
            "robust_slices_favourable": self.robust_slices_favourable,
            "slices": [s.as_dict() for s in self.slices],
            "sensitivity": self.sensitivity,
            "complexity_ratio": self.complexity_ratio,
            "economically_significant": self.economically_significant,
            "economic_note": self.economic_note,
            "family_challenger_count": self.family_challenger_count,
            "family_run_count": self.family_run_count,
            "window_reuse_count": self.window_reuse_count,
            "warnings": self.warnings,
            "scorecard": self.scorecard.as_dict() if self.scorecard else None,
            "decision": self.decision.value, "reasons": self.reasons,
            "limitations": self.limitations, "computed_at": self.computed_at,
        }


# ======================================================================
# The decision
# ======================================================================

def build_scorecard(result: ChallengerResult, plan: EvaluationPlan
                    ) -> Scorecard:
    """
    Six dimensions, each with the reason it reads that way.

    `evidence` is the dimension people would leave out and the one that
    most often decides the answer: a large effect measured on 41
    observations over eight days, in a family that has already been
    tested nine times, is weak evidence however large the effect.
    """
    sample = int(result.challenger_out_of_sample.get("sample_size") or 0)

    if result.effect is None:
        performance = Dimension("performance", None, "unknown",
                                "no out-of-sample effect could be measured")
    elif result.effect >= plan.min_effect:
        performance = Dimension(
            "performance", result.effect, "better",
            "out-of-sample effect %+0.4f meets the %+0.4f fixed in advance"
            % (result.effect, plan.min_effect))
    elif result.effect > 0:
        performance = Dimension(
            "performance", result.effect, "similar",
            "positive but below the %+0.4f required" % plan.min_effect)
    else:
        performance = Dimension(
            "performance", result.effect, "worse",
            "the challenger is behind its baseline out of sample")

    spans_zero = (result.effect_low is not None
                  and result.effect_high is not None
                  and result.effect_low <= 0 <= result.effect_high)
    if result.effect_low is None:
        risk = Dimension("risk", None, "unknown",
                         "no interval could be computed")
    elif spans_zero:
        risk = Dimension(
            "risk", result.effect_low, "worse",
            "the interval [%+0.4f, %+0.4f] includes zero, so the data does "
            "not separate the two" % (result.effect_low, result.effect_high))
    else:
        risk = Dimension(
            "risk", result.effect_low, "better",
            "the interval [%+0.4f, %+0.4f] excludes zero"
            % (result.effect_low, result.effect_high))

    if result.robust_slices:
        fraction = result.robust_slices_favourable / result.robust_slices
        robustness = Dimension(
            "robustness", round(fraction, 4),
            "better" if fraction >= plan.min_robust_fraction else "worse",
            "the challenger led in %d of %d slices"
            % (result.robust_slices_favourable, result.robust_slices))
    else:
        robustness = Dimension("robustness", None, "unknown",
                               "no slice could be measured")

    if result.walk_forward_folds:
        fold_fraction = (result.walk_forward_folds_favourable
                         / result.walk_forward_folds)
        stability = Dimension(
            "stability", round(fold_fraction, 4),
            "better" if fold_fraction >= plan.min_robust_fraction else "worse",
            "the challenger led in %d of %d walk-forward folds"
            % (result.walk_forward_folds_favourable,
               result.walk_forward_folds))
    else:
        stability = Dimension("stability", None, "unknown",
                              "walk-forward could not be run on this record")

    if result.complexity_ratio is None:
        complexity = Dimension("complexity", None, "unknown",
                               "the baseline declares no complexity")
    elif result.complexity_ratio <= 1.0:
        complexity = Dimension(
            "complexity", result.complexity_ratio, "better",
            "no more moving parts than the baseline")
    elif result.complexity_ratio <= plan.max_complexity_ratio:
        complexity = Dimension(
            "complexity", result.complexity_ratio, "similar",
            "%.1fx the baseline, within the %.1fx ceiling"
            % (result.complexity_ratio, plan.max_complexity_ratio))
    else:
        complexity = Dimension(
            "complexity", result.complexity_ratio, "worse",
            "%.1fx the baseline, above the %.1fx ceiling"
            % (result.complexity_ratio, plan.max_complexity_ratio))

    serious = [w for w in result.warnings
               if w in ("test_set_reuse", "single_parameter_peak",
                        "large_train_test_gap", "unstable_sign",
                        "single_instrument", "single_regime")]
    if sample < plan.min_sample:
        evidence = Dimension(
            "evidence", float(sample), "worse",
            "%d out-of-sample observations, below the %d fixed in advance"
            % (sample, plan.min_sample))
    elif serious or result.family_challenger_count > 3:
        evidence = Dimension(
            "evidence", float(sample), "similar",
            "%d observations; %d challenger(s) tried in this family%s"
            % (sample, result.family_challenger_count,
               "; warnings: " + ", ".join(serious) if serious else ""))
    else:
        evidence = Dimension(
            "evidence", float(sample), "better",
            "%d out-of-sample observations with no serious warning" % sample)

    return Scorecard(performance=performance, risk=risk,
                     robustness=robustness, stability=stability,
                     complexity=complexity, evidence=evidence)


def decide(result: ChallengerResult, plan: EvaluationPlan
           ) -> Tuple[ChallengerDecision, List[str]]:
    """
    Whether the evidence supports the change (§38).

    Order matters and is deliberate:

    1. **Not enough evidence** → INCONCLUSIVE, before anything else.
       A large effect on a small sample is not an inferior result, it
       is an unmeasured one, and the two must not be collapsed.
    2. **Slices disagree** → CONTEXT_DEPENDENT, before a global verdict.
       §39: a challenger that wins in one context and loses in another
       has told you something, and averaging destroys it.
    3. **Behind the baseline** → INFERIOR.
    4. **Every dimension holds** → SUPERIOR.
    5. Anything else → REQUIRES_REVIEW rather than a forced verdict.

    Reasons are returned in every branch, including the passing checks.
    """
    reasons: List[str] = []
    sample = int(result.challenger_out_of_sample.get("sample_size") or 0)

    if sample < plan.min_sample:
        return ChallengerDecision.INCONCLUSIVE, [
            "%d out-of-sample observations is below the %d fixed before the "
            "comparison. Nothing can be concluded in either direction — this "
            "is not a verdict against the challenger."
            % (sample, plan.min_sample)]

    if result.effect is None:
        return ChallengerDecision.INCONCLUSIVE, [
            "no out-of-sample effect could be measured"]

    measured = [s for s in result.slices if s.effect is not None]
    favourable = [s for s in measured if s.effect > 0]
    against = [s for s in measured if s.effect < 0]
    if len(measured) >= 3 and favourable and against:
        share = len(favourable) / len(measured)
        if 0.25 <= share <= 0.75:
            reasons.append(
                "the challenger leads in %d of %d contexts and trails in %d. "
                "That is a context-dependent result, not a global one, and "
                "averaging the two would describe neither: %s"
                % (len(favourable), len(measured), len(against),
                   "; ".join("%s %s %+0.4f" % (s.kind, s.label, s.effect)
                             for s in measured[:6])))
            reasons.append(
                "A context-dependent challenger is a real finding. It says "
                "where the change helps, which is more useful than a single "
                "number that hides it.")
            return ChallengerDecision.CONTEXT_DEPENDENT, reasons

    spans_zero = (result.effect_low is not None
                  and result.effect_high is not None
                  and result.effect_low <= 0 <= result.effect_high)

    if result.effect < 0:
        reasons.append(
            "out-of-sample effect %+0.4f: the challenger is behind its "
            "baseline" % result.effect)
        if spans_zero:
            reasons.append(
                "the interval [%+0.4f, %+0.4f] includes zero, so the "
                "shortfall itself is not established"
                % (result.effect_low, result.effect_high))
            return ChallengerDecision.INCONCLUSIVE, reasons
        return ChallengerDecision.INFERIOR, reasons

    scorecard = result.scorecard or build_scorecard(result, plan)
    failing = scorecard.failing()

    if result.effect < plan.min_effect:
        reasons.append(
            "out-of-sample effect %+0.4f is positive but below the %+0.4f "
            "fixed before the comparison" % (result.effect, plan.min_effect))
        return ChallengerDecision.INCONCLUSIVE, reasons

    if plan.require_interval_excludes_zero and spans_zero:
        reasons.append(
            "the interval [%+0.4f, %+0.4f] includes zero: the data does not "
            "separate the challenger from its baseline"
            % (result.effect_low, result.effect_high))
        return ChallengerDecision.INCONCLUSIVE, reasons

    unmeasured = scorecard.unknown()
    if unmeasured and not failing:
        reasons.append(
            "the effect clears its bar, but %s could not be measured on this "
            "record. A dimension that was never measured is not a dimension "
            "that passed, so this is a decision for a person rather than a "
            "verdict for the system."
            % (", ".join(unmeasured)))
        for dimension in scorecard.dimensions():
            reasons.append("%s: %s" % (dimension.name, dimension.detail))
        return ChallengerDecision.REQUIRES_REVIEW, reasons

    if failing:
        reasons.append(
            "the effect clears its bar, but %s %s worse than the baseline. "
            "A change that wins on return and loses on %s is a trade-off for "
            "a person to weigh, not a verdict for the system to issue."
            % (", ".join(failing), "reads" if len(failing) == 1 else "read",
               failing[0]))
        for dimension in scorecard.dimensions():
            reasons.append("%s: %s" % (dimension.name, dimension.detail))
        return ChallengerDecision.REQUIRES_REVIEW, reasons

    if result.economically_significant is False:
        reasons.append("economic significance: " + (result.economic_note or
                       "the effect is too small to matter in practice"))
        return ChallengerDecision.REQUIRES_REVIEW, reasons

    for dimension in scorecard.dimensions():
        reasons.append("met: %s — %s" % (dimension.name, dimension.detail))
    reasons.append(
        "every dimension was measured; none reads worse than the baseline")
    reasons.append(
        "SUPERIOR means the challenger beat its baseline on every dimension "
        "fixed before the comparison. It is not an approval to deploy, and "
        "nothing in production has changed.")
    return ChallengerDecision.SUPERIOR, reasons


# ======================================================================
# Candidate validation (§3)
# ======================================================================

def validate_candidate(candidate: Dict[str, Any]) -> List[str]:
    """
    Why this candidate should NOT become a challenger, if it should not.

    §3: do not automatically create a challenger from every candidate.
    Building one is expensive — walk-forward, slices, a sweep — and
    spending that on evidence which already cannot support it is how a
    research programme fills up with work nobody can act on.

    Returns the blocking reasons. Empty means it qualifies.
    """
    problems: List[str] = []
    if not candidate:
        return ["no candidate"]
    if candidate.get("status") == "rejected":
        problems.append("the candidate was rejected")
    for key in ("hypothesis_id", "conclusion_id", "experiment_id"):
        if not candidate.get(key):
            problems.append("the candidate does not reference its %s"
                            % key.replace("_", " "))
    if not candidate.get("base_version"):
        problems.append(
            "the candidate names no base version, so there is nothing to "
            "compare a challenger against")
    changes = candidate.get("changes") or {}
    if not changes.get("evaluator"):
        problems.append(
            "the candidate names no evaluator, so its change cannot be "
            "expressed as a testable variant")
    effect = candidate.get("effect")
    if effect is None:
        problems.append("the candidate carries no measured effect")
    return problems
