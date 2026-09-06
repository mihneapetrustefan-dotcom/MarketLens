"""
src/domain/experiment_models.py
---------------------------------------
A hypothesis, a baseline, a candidate, and a rule for deciding — fixed
before the answer is known.

WHAT MAKES THIS A LABORATORY RATHER THAN A SEARCH
-----------------------------------------------------
A search asks "which configuration made the most money". A laboratory
asks "which hypothesis survived a fair test". The difference is
entirely procedural, and three rules carry it:

  1. **Acceptance criteria are part of the definition**, hashed into
     the configuration fingerprint with everything else. §46 says they
     must be defined before running; here they *cannot* be defined
     after, because changing them changes the fingerprint and an
     experiment that has started refuses a changed fingerprint.

  2. **PASS does not mean profitable.** It means the predefined
     criteria were met (§4, §47). A candidate that made money and
     missed its criteria is a FAIL, and one that met them on a tiny
     effect is still flagged for economic significance.

  3. **Every experiment knows how many siblings it has.** A hypothesis
     tested fifty ways will produce a winner by chance, so the family
     count travels with the decision (§41, §42) and the verdict says so
     in words.

WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN
------------------------------------------------
No backtester — Phase 12's is reused (§36). No walk-forward splitter —
Phase 9's `WalkForwardSplitter` with its purge and embargo is reused
(§40). No sensitivity harness — Phase 12's `RobustnessHarness` is
reused. Rebuilding any of the three would create a second answer to a
question that already has one.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Bump when the MEANING of an experiment or its evaluation changes.
#: Part of the configuration fingerprint, so a change produces a new
#: experiment rather than silently reinterpreting an old result.
EXPERIMENT_METHOD_VERSION = "v1"


class ExperimentStatus(str, Enum):
    """
    Lifecycle (§4).

    `PASSED` and `REJECTED` are terminal research verdicts, distinct
    from `COMPLETED`: an experiment can complete cleanly and still have
    no verdict, which is what `INCONCLUSIVE` is for.
    """
    DRAFT = "draft"
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCONCLUSIVE = "inconclusive"
    PASSED = "passed"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (ExperimentStatus.COMPLETED, ExperimentStatus.FAILED,
                        ExperimentStatus.CANCELLED, ExperimentStatus.INCONCLUSIVE,
                        ExperimentStatus.PASSED, ExperimentStatus.REJECTED)

    @property
    def is_started(self) -> bool:
        """Once started, the definition is frozen (§32, §73)."""
        return self not in (ExperimentStatus.DRAFT, ExperimentStatus.PLANNED,
                            ExperimentStatus.QUEUED)


class ExperimentType(str, Enum):
    """§23. Each names the component the candidate changes."""
    FEATURE = "feature"
    MODEL = "model"
    SIGNAL = "signal"
    STRATEGY = "strategy"
    REGIME = "regime"
    LABEL = "label"
    PORTFOLIO = "portfolio"
    EXECUTION = "execution"
    RISK = "risk"


class HypothesisSource(str, Enum):
    """
    Where the question came from (§20).

    Recorded because provenance changes how a result should be read: a
    hypothesis mined from the same memory it is tested against is more
    exposed to data snooping than one a person brought from outside,
    and a reader deserves to know which they are looking at.
    """
    MEMORY_PATTERN = "memory_pattern"
    ERROR_ATTRIBUTION = "error_attribution"
    RESEARCHER = "researcher"
    MODEL_ANALYSIS = "model_analysis"
    SIGNAL_ANALYSIS = "signal_analysis"
    REGIME_ANALYSIS = "regime_analysis"
    EVENT_ANALYSIS = "event_analysis"


class Decision(str, Enum):
    """§47. PASS means the predefined criteria were met. Nothing more."""
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ======================================================================
# Hypothesis (§5)
# ======================================================================

@dataclass
class Hypothesis:
    """
    The question, stated before the answer is known.

    `mechanism` is required and is not decoration. A statement with no
    proposed reason is a pattern-match, and pattern-matches are what
    data snooping produces in quantity. Requiring a mechanism does not
    make a hypothesis true, but it does make it falsifiable in a second
    way — the mechanism can be wrong even when the correlation holds.
    """
    statement: str
    mechanism: str
    expected_effect: str
    population: str
    conditions: Dict[str, Any] = field(default_factory=dict)
    metric: str = "directional_accuracy"
    minimum_detectable_effect: Optional[float] = None
    source: HypothesisSource = HypothesisSource.RESEARCHER
    source_reference: Optional[str] = None
    family_id: Optional[str] = None

    def validate(self) -> None:
        """
        Refuse a hypothesis that cannot be tested.

        Called before an experiment can leave DRAFT. Each of these
        absences produces a different kind of untestable experiment,
        and the messages say which.
        """
        problems = []
        if not (self.statement or "").strip():
            problems.append("no statement: there is nothing to test")
        if not (self.mechanism or "").strip():
            problems.append(
                "no mechanism: a statement with no proposed reason is a "
                "pattern-match, and a pattern-match cannot be wrong in an "
                "interesting way")
        if not (self.expected_effect or "").strip():
            problems.append(
                "no expected effect: without one, any result can be read as "
                "confirmation")
        if not (self.population or "").strip():
            problems.append("no population: the claim has no scope")
        if not (self.metric or "").strip():
            problems.append("no metric: nothing would decide it")
        if problems:
            raise ValueError("This hypothesis cannot be tested — "
                             + "; ".join(problems))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "statement": self.statement, "mechanism": self.mechanism,
            "expected_effect": self.expected_effect,
            "population": self.population, "conditions": self.conditions,
            "metric": self.metric,
            "minimum_detectable_effect": self.minimum_detectable_effect,
            "source": self.source.value,
            "source_reference": self.source_reference,
            "family_id": self.family_id,
        }


# ======================================================================
# Baseline and candidate (§6, §7)
# ======================================================================

@dataclass
class ArmSpec:
    """
    One side of the comparison — the control or the candidate.

    `parameters` is a plain dict and `evaluator` is a REGISTERED NAME,
    never a callable or a code string. §80 forbids arbitrary code
    execution through any interface, and the way to forbid it
    structurally is for the configuration to be unable to express code
    at all.
    """
    name: str
    evaluator: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    #: How many moving parts this arm has (§49). A candidate that adds
    #: 84 features for a 0.3% gain should lose to a simpler one, and it
    #: can only lose if complexity is measured.
    complexity: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "evaluator": self.evaluator,
                "parameters": self.parameters, "description": self.description,
                "complexity": self.complexity}

    def identity(self) -> Dict[str, Any]:
        """
        The part of an arm that defines WHAT IS TESTED (§32).

        `description` is deliberately absent. It is prose written for a
        reader, and editing a sentence must not be able to invalidate a
        running experiment -- the fingerprint exists to catch a changed
        threshold, evaluator or parameter, not a reworded comment.

        This is also what makes the fingerprint survive storage: every
        field here has a column behind it, so an experiment recomputes
        to the same fingerprint after being loaded back.
        """
        return {"name": self.name, "evaluator": self.evaluator,
                "parameters": self.parameters,
                "complexity": self.complexity}


@dataclass
class AcceptanceCriteria:
    """
    What would count as success — fixed before the result is seen (§46).

    Part of the configuration fingerprint. An experiment that has
    started refuses a changed fingerprint, so these cannot be relaxed
    after the fact; that is the structural version of §46 rather than a
    convention someone has to remember.

    Every field has a defensible default and every one can be tightened.
    `min_effect` exists so that statistical significance alone cannot
    produce a PASS on an economically meaningless difference (§48).
    """
    #: The candidate must beat the baseline on the hypothesis metric by
    #: at least this much, out of sample.
    min_effect: float = 0.02
    #: Below this many out-of-sample observations, nothing is decided.
    min_sample: int = 30
    #: The improvement must survive in this fraction of robustness
    #: slices (time windows, regimes) before it counts as robust.
    min_robust_fraction: float = 0.6
    #: A candidate may not be more than this many times as complex as
    #: the baseline for a marginal gain.
    max_complexity_ratio: float = 5.0
    #: The bootstrap interval on the effect must exclude zero.
    require_interval_excludes_zero: bool = True
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "min_effect": self.min_effect, "min_sample": self.min_sample,
            "min_robust_fraction": self.min_robust_fraction,
            "max_complexity_ratio": self.max_complexity_ratio,
            "require_interval_excludes_zero": self.require_interval_excludes_zero,
            "notes": self.notes,
        }


@dataclass
class DatasetSnapshot:
    """
    A fixed view of the data (§10).

    `as_of` is the point-in-time anchor. Everything the experiment sees
    — features, outcomes, memory — must have been available at or
    before it, and `data_cutoff` records the last moment of evidence
    the snapshot contains.

    Never run an experiment against a silently changing dataset: the
    snapshot id is derived from its own contents, so the same
    description always names the same data and a different description
    is a different dataset.
    """
    as_of: Optional[str] = None
    data_cutoff: Optional[str] = None
    universe: str = "all"
    filters: Dict[str, Any] = field(default_factory=dict)
    dataset_version: str = ""
    feature_version: str = ""
    label_version: str = ""
    observation_count: int = 0
    instrument_count: int = 0
    created_at: Optional[str] = None

    @property
    def snapshot_id(self) -> str:
        payload = json.dumps({
            "as_of": self.as_of, "cutoff": self.data_cutoff,
            "universe": self.universe, "filters": self.filters,
            "dataset": self.dataset_version, "features": self.feature_version,
            "labels": self.label_version}, sort_keys=True, default=str)
        return f"ds-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id, "as_of": self.as_of,
            "data_cutoff": self.data_cutoff, "universe": self.universe,
            "filters": self.filters, "dataset_version": self.dataset_version,
            "feature_version": self.feature_version,
            "label_version": self.label_version,
            "observation_count": self.observation_count,
            "instrument_count": self.instrument_count,
            "created_at": self.created_at,
        }


@dataclass
class EvaluationProtocol:
    """
    How the comparison will be judged — fixed in advance (§37, §38, §39).

    `holdout_fraction` splits BY TIME, never at random. A random split
    of financial observations leaks: two rows from the same day land on
    opposite sides and the test set learns the training set's answer.

    `embargo_days` and `label_horizon_days` feed Phase 9's
    `WalkForwardSplitter`, which does the purging. They are recorded
    here so the protocol is reconstructable without reading code.
    """
    #: 'holdout' — one chronological split; 'walk_forward' — Phase 9's
    #: splitter with purge and embargo.
    method: str = "holdout"
    holdout_fraction: float = 0.3
    label_horizon_days: float = 5.0
    embargo_days: float = 1.0
    train_months: int = 3
    test_months: int = 1
    step_months: int = 1
    expanding: bool = True
    bootstrap_iterations: int = 2000
    random_seed: int = 20260906

    def as_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method, "holdout_fraction": self.holdout_fraction,
            "label_horizon_days": self.label_horizon_days,
            "embargo_days": self.embargo_days,
            "train_months": self.train_months, "test_months": self.test_months,
            "step_months": self.step_months, "expanding": self.expanding,
            "bootstrap_iterations": self.bootstrap_iterations,
            "random_seed": self.random_seed,
        }


@dataclass
class ResourceLimits:
    """
    Ceilings on what one experiment may consume (§57, §81).

    Not theoretical: an evaluator that scans an unbounded cohort, or a
    sensitivity sweep with a hundred points, will happily run until
    something else breaks. Every limit is checked by the engine and a
    breach fails the run with a reason rather than being killed from
    outside.
    """
    max_runtime_seconds: float = 300.0
    max_rows: int = 200_000
    max_variants: int = 25
    max_bootstrap_iterations: int = 10_000

    def as_dict(self) -> Dict[str, Any]:
        return {"max_runtime_seconds": self.max_runtime_seconds,
                "max_rows": self.max_rows, "max_variants": self.max_variants,
                "max_bootstrap_iterations": self.max_bootstrap_iterations}


# ======================================================================
# The experiment
# ======================================================================

@dataclass
class Experiment:
    """
    One question, one control, one candidate, one rule for deciding.

    The configuration fingerprint covers the hypothesis, both arms, the
    dataset snapshot, the protocol, the acceptance criteria and the
    methodology version. Once the experiment has started, a changed
    fingerprint is refused — which is §32 and §73 enforced by
    arithmetic rather than by discipline.
    """
    experiment_id: str
    name: str
    experiment_type: ExperimentType
    hypothesis: Hypothesis
    baseline: ArmSpec
    candidate: ArmSpec
    dataset: DatasetSnapshot = field(default_factory=DatasetSnapshot)
    protocol: EvaluationProtocol = field(default_factory=EvaluationProtocol)
    criteria: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    description: str = ""
    created_by: str = ""
    status: ExperimentStatus = ExperimentStatus.DRAFT

    method_version: str = EXPERIMENT_METHOD_VERSION
    code_version: str = ""
    model_version: str = ""
    strategy_version: str = ""
    configuration_version: str = "1"

    created_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    @property
    def fingerprint(self) -> str:
        """
        Everything that defines the experiment, hashed.

        Deliberately includes the acceptance criteria. Moving the goal
        posts changes the fingerprint, and a started experiment refuses
        a changed fingerprint — so §46 holds structurally.
        """
        payload = json.dumps({
            "type": self.experiment_type.value,
            "hypothesis": self.hypothesis.as_dict(),
            "baseline": self.baseline.identity(),
            "candidate": self.candidate.identity(),
            "dataset": self.dataset.as_dict(),
            "protocol": self.protocol.as_dict(),
            "criteria": self.criteria.as_dict(),
            "method_version": self.method_version,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    @property
    def comparison_fingerprint(self) -> str:
        """
        What is actually being measured, ignoring how it is worded.

        The full fingerprint covers the hypothesis text, so two
        experiments asking different questions of the SAME two arms
        over the SAME cohort hash differently -- and the lab counts
        them as two pieces of research when only one comparison was
        run.

        That is not hypothetical. The first proposal sweep generated
        three "responses to recurring errors" -- one each for
        signal_error, prediction_error and magnitude_error -- which
        resolved to an identical baseline, candidate and dataset and
        returned byte-identical results. Reported as three findings
        they would treble the apparent evidence, and the multiple-
        testing correction in §41 would not catch it, because that
        counts experiments within a family and these sat in three
        different families.

        So the comparison gets its own identity, and
        `api.integrity_check` reports how many experiments share one.
        """
        def arm(spec):
            # The NAME is excluded as well as the description. Two arms
            # that call the same evaluator with the same parameters are
            # the same arm however they are labelled -- and labelling
            # was the only thing separating the three duplicates that
            # prompted this property.
            return {"evaluator": spec.evaluator, "parameters": spec.parameters}

        payload = json.dumps({
            "baseline": arm(self.baseline),
            "candidate": arm(self.candidate),
            "dataset": self.dataset.as_dict(),
            "metric": self.hypothesis.metric,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    @property
    def changed_variables(self) -> List[str]:
        """
        Which parameters differ between the arms (§7, §8).

        §8 prefers one variable at a time and requires the rest to be
        documented when more change. This computes the list rather than
        trusting a description, so an experiment cannot claim to change
        one thing while changing four.
        """
        changed = []
        keys = set(self.baseline.parameters) | set(self.candidate.parameters)
        for key in sorted(keys):
            if self.baseline.parameters.get(key) != self.candidate.parameters.get(key):
                changed.append(key)
        if self.baseline.evaluator != self.candidate.evaluator:
            changed.append("evaluator")
        return changed

    def validate(self) -> None:
        """Refuse an experiment that could not produce a fair answer."""
        self.hypothesis.validate()
        problems = []
        if not self.baseline.evaluator:
            problems.append("the baseline names no evaluator")
        if not self.candidate.evaluator:
            problems.append("the candidate names no evaluator")
        if not self.changed_variables:
            problems.append(
                "the candidate is identical to the baseline — there is no "
                "change to attribute a difference to")
        if self.criteria.min_sample < 1:
            problems.append("min_sample must be at least 1")
        if not 0.0 < self.protocol.holdout_fraction < 1.0:
            problems.append("holdout_fraction must be between 0 and 1")
        if (self.protocol.bootstrap_iterations
                > self.limits.max_bootstrap_iterations):
            problems.append(
                f"bootstrap_iterations {self.protocol.bootstrap_iterations} "
                f"exceeds the resource limit "
                f"{self.limits.max_bootstrap_iterations}")
        if problems:
            raise ValueError("This experiment cannot run — " + "; ".join(problems))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "name": self.name,
            "type": self.experiment_type.value, "status": self.status.value,
            "hypothesis": self.hypothesis.as_dict(),
            "baseline": self.baseline.as_dict(),
            "candidate": self.candidate.as_dict(),
            "dataset": self.dataset.as_dict(),
            "protocol": self.protocol.as_dict(),
            "criteria": self.criteria.as_dict(),
            "limits": self.limits.as_dict(),
            "fingerprint": self.fingerprint,
            "changed_variables": self.changed_variables,
            "method_version": self.method_version,
            "code_version": self.code_version,
        }


@dataclass
class ArmMetrics:
    """One arm's measured performance on one slice."""
    label: str = ""
    sample_size: int = 0
    hits: int = 0
    misses: int = 0
    neutrals: int = 0
    directional_accuracy: Optional[float] = None
    mean_return: Optional[float] = None
    median_return: Optional[float] = None
    stdev_return: Optional[float] = None
    mean_mfe: Optional[float] = None
    mean_mae: Optional[float] = None
    instrument_count: int = 0

    @property
    def decided(self) -> int:
        return self.hits + self.misses

    def metric(self, name: str) -> Optional[float]:
        return {
            "directional_accuracy": self.directional_accuracy,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "mean_mfe": self.mean_mfe,
            "mean_mae": self.mean_mae,
        }.get(name)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label, "sample_size": self.sample_size,
            "hits": self.hits, "misses": self.misses,
            "neutrals": self.neutrals,
            "directional_accuracy": self.directional_accuracy,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "stdev_return": self.stdev_return,
            "mean_mfe": self.mean_mfe, "mean_mae": self.mean_mae,
            "instrument_count": self.instrument_count,
        }


@dataclass
class ExperimentResult:
    """
    The comparison, the caveats, and the verdict (§45).

    `reasons` explains the decision in full — every criterion that was
    met and every one that was not. A verdict without its reasoning is
    an assertion, and this phase's whole premise is that a research
    result must be arguable.
    """
    experiment_id: str
    run_id: str
    metric: str = "directional_accuracy"

    baseline_in_sample: ArmMetrics = field(default_factory=ArmMetrics)
    candidate_in_sample: ArmMetrics = field(default_factory=ArmMetrics)
    baseline_out_of_sample: ArmMetrics = field(default_factory=ArmMetrics)
    candidate_out_of_sample: ArmMetrics = field(default_factory=ArmMetrics)

    effect: Optional[float] = None
    effect_in_sample: Optional[float] = None
    effect_low: Optional[float] = None
    effect_high: Optional[float] = None
    interval_method: str = ""

    robust_slices: int = 0
    robust_slices_passing: int = 0
    robustness: Dict[str, Any] = field(default_factory=dict)
    sensitivity: Dict[str, Any] = field(default_factory=dict)
    ablation: Dict[str, Any] = field(default_factory=dict)

    complexity_ratio: Optional[float] = None
    economically_significant: Optional[bool] = None

    family_experiment_count: int = 1
    family_comparison_count: int = 1

    decision: Decision = Decision.INCONCLUSIVE
    reasons: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    computed_at: Optional[datetime] = None

    def __post_init__(self):
        if self.computed_at is None:
            self.computed_at = datetime.now(timezone.utc)

    @property
    def overfitting_gap(self) -> Optional[float]:
        """
        In-sample effect minus out-of-sample effect.

        A large positive gap is the signature of a candidate that fitted
        the training data. Reported rather than acted on: it is evidence
        for a reader, not a rule.
        """
        if self.effect is None or self.effect_in_sample is None:
            return None
        return self.effect_in_sample - self.effect

    def as_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "run_id": self.run_id,
            "metric": self.metric,
            "baseline_out_of_sample": self.baseline_out_of_sample.as_dict(),
            "candidate_out_of_sample": self.candidate_out_of_sample.as_dict(),
            "effect": self.effect, "effect_in_sample": self.effect_in_sample,
            "overfitting_gap": self.overfitting_gap,
            "effect_low": self.effect_low, "effect_high": self.effect_high,
            "interval_method": self.interval_method,
            "robust_slices": self.robust_slices,
            "robust_slices_passing": self.robust_slices_passing,
            "complexity_ratio": self.complexity_ratio,
            "economically_significant": self.economically_significant,
            "family_experiment_count": self.family_experiment_count,
            "decision": self.decision.value,
            "reasons": self.reasons, "limitations": self.limitations,
        }


@dataclass
class ExperimentRun:
    """
    One execution of a definition (§33, §34).

    Separate from the experiment because the same question can be asked
    twice — a different seed, a rerun after a failure, a check that the
    result reproduces. Each run records its own seed and environment so
    two runs that disagree can be told apart.
    """
    run_id: str
    experiment_id: str
    status: RunStatus = RunStatus.QUEUED
    seed: int = 0
    environment: str = ""
    dataset_snapshot_id: str = ""
    code_version: str = ""
    fingerprint: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    rows_examined: int = 0
    cache_hit: bool = False
    cached_from_run: Optional[str] = None
    error: str = ""
    cancelled_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id, "experiment_id": self.experiment_id,
            "status": self.status.value, "seed": self.seed,
            "environment": self.environment,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "code_version": self.code_version, "fingerprint": self.fingerprint,
            "duration_seconds": self.duration_seconds,
            "rows_examined": self.rows_examined,
            "cache_hit": self.cache_hit, "cached_from_run": self.cached_from_run,
            "error": self.error, "cancelled_reason": self.cancelled_reason,
        }


# ======================================================================
# Statistics used by the decision
# ======================================================================

def bootstrap_difference(baseline: Sequence[float], candidate: Sequence[float],
                         *, iterations: int = 2000, level: float = 0.95,
                         seed: int = 20260906) -> Tuple[Optional[float], Optional[float], str]:
    """
    A percentile bootstrap interval on the DIFFERENCE of two means.

    Deterministic: the seed is explicit and stored on the run, so the
    same data and the same seed give the same interval (§62, §63). A
    research number that changes when you look at it twice is not a
    research number.

    Returns `(low, high, method)` — `(None, None, "")` when either arm
    is too small, because an interval computed on eight observations is
    not a cautious estimate, it is an invitation to read eight
    observations as evidence.
    """
    import random
    if len(baseline) < 30 or len(candidate) < 30:
        return None, None, ""
    rng = random.Random(seed)
    base_pool, cand_pool = list(baseline), list(candidate)
    n_base, n_cand = len(base_pool), len(cand_pool)
    differences = []
    for _ in range(iterations):
        b = sum(rng.choices(base_pool, k=n_base)) / n_base
        c = sum(rng.choices(cand_pool, k=n_cand)) / n_cand
        differences.append(c - b)
    differences.sort()
    tail = (1.0 - level) / 2.0

    def percentile(values, fraction):
        position = fraction * (len(values) - 1)
        low = int(math.floor(position))
        high = int(math.ceil(position))
        if low == high:
            return values[low]
        weight = position - low
        return values[low] * (1 - weight) + values[high] * weight

    return (percentile(differences, tail),
            percentile(differences, 1.0 - tail),
            f"percentile_bootstrap_{iterations}")


def multiple_testing_note(family_experiments: int, comparisons: int) -> str:
    """
    The selection-bias caveat, as text (§41, §42, §84).

    Returned so it can be attached to the result a reader is looking
    at. A caveat filed where nobody reads it is not a caveat.
    """
    if family_experiments <= 1 and comparisons <= 1:
        return ("This is the first experiment in its hypothesis family and "
                "the only comparison, so no selection-bias correction "
                "applies. That will stop being true as soon as a second is "
                "run.")
    expected_false = comparisons * 0.05
    return (
        f"This hypothesis family now contains {family_experiments} "
        f"experiment(s) and {comparisons} comparison(s). At the conventional "
        f"5% level roughly {expected_false:.1f} of them would look notable "
        f"with no effect present. A winner selected from a family of this "
        f"size is not the same evidence as a single pre-registered test, and "
        f"the number above should be read alongside any result that passed.")


def economic_significance(effect: Optional[float], metric: str,
                          *, threshold: float = 0.01) -> Tuple[Optional[bool], str]:
    """
    Is the effect large enough to be worth the complexity (§48)?

    Separate from statistical significance on purpose. A 0.2% edge that
    a bootstrap interval excludes zero for is real and may still be
    worthless once it has to survive costs, capacity and the
    maintenance burden of whatever produced it.
    """
    if effect is None:
        return None, "no effect could be measured"
    if abs(effect) < threshold:
        return False, (
            f"the effect is {effect:+.2%} on {metric}, smaller in magnitude "
            f"than the {threshold:.0%} economic threshold. It may be real and "
            f"still not worth the complexity it costs.")
    if effect < 0:
        # Large and in the WRONG direction. Reporting "above the
        # economic threshold" here would read as a point in the
        # candidate's favour, which is the opposite of what it means.
        return False, (
            f"the effect is {effect:+.2%} on {metric} — large enough to "
            f"matter and in the wrong direction: the candidate is materially "
            f"worse than the baseline out of sample.")
    return True, (f"the effect is {effect:+.2%} on {metric}, above the "
                  f"{threshold:.0%} economic threshold")
