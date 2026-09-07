"""
src/domain/autoresearch_models.py
---------------------------------------
Phase 23 — the vocabulary of a research programme.

WHY THIS PACKAGE IS CALLED autoresearch AND NOT research
------------------------------------------------------------
`src/research/` and the tables `research_observations`,
`research_features`, `research_labels` already exist: they are Phase
6's dataset builder, and they mean something entirely different. A
Phase 23 module dropped in beside them would be read as more of the
same, and a table called `research_observations` already holds rows.
So the autonomous researcher gets its own package and its own
`autoresearch_` table prefix. The name is uglier and unambiguous,
which is the correct trade at this point in a project's life.

WHAT A RESEARCHER IS, IN THIS CODEBASE
------------------------------------------
    observation (with evidence)
        -> question (triaged)
            -> hypothesis (falsifiable, quality-gated)
                -> experiment proposal (Phase 22, never a second engine)
                    -> run
                        -> conclusion
                            -> memory

The loop stops at memory. It does not close into production, and
§34 is the line: a result may become a CANDIDATE and never
automatically an ACTIVE anything.

THE PROPERTY THAT MAKES THIS A RESEARCHER RATHER THAN AN OPTIMISER
----------------------------------------------------------------------
A rejected hypothesis is a result. `ConclusionType` has six members
and only one of them is SUPPORTED; REJECTED, INCONCLUSIVE,
INSUFFICIENT_DATA and CONFLICTING_EVIDENCE are all first-class
outcomes that get stored, counted, and shown. Nothing in this file
scores a research programme by how many hypotheses survived, because
a programme that only reports its survivors is not reporting.

NO LLM IS USED
------------------
§47 permits an LLM as a reasoning layer; it does not require one.
Every generator here is deterministic: an observation maps to a
question by rule, a question maps to a hypothesis by template, and a
hypothesis maps to a Phase 22 experiment by registered evaluator
name. That keeps the "no LLM anywhere" property Phases 21 and 22
established and enforce by AST scan, keeps credentials out of the
research path entirely (§75), and means every claim traces to a row
rather than to a sentence somebody's model produced. If an LLM is
added later, §48 already says where it goes: beside the evidence,
never mixed into it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Bumped when the meaning of a stored research row changes. Rows
#: carry it so two methodologies coexist instead of overwriting each
#: other (§84) — the same discipline as Phases 19-22.
RESEARCH_METHOD_VERSION = "v1"

#: The one sample threshold this project has used since Phase 9.
#: Repeated here as an import rather than a new number, because a
#: second threshold with a different value would quietly mean the
#: research layer and the experiment layer disagree about what counts
#: as enough evidence.
from src.domain.model_models import ModelEvaluation  # noqa: E402

MIN_RESEARCH_SAMPLE = ModelEvaluation.MIN_EFFECTIVE_SAMPLE


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


# ======================================================================
# Enumerations
# ======================================================================

class ObservationKind(str, Enum):
    """What the researcher noticed (§4). Every member needs evidence."""
    RECURRING_ERROR = "recurring_error"
    RECURRING_SUCCESS = "recurring_success"
    MODEL_DEGRADATION = "model_degradation"
    REGIME_DEPENDENCE = "regime_dependence"
    SIGNAL_WEAKNESS = "signal_weakness"
    SIGNAL_STRENGTH_BEHAVIOUR = "signal_strength_behaviour"
    CONFIDENCE_BEHAVIOUR = "confidence_behaviour"
    EVENT_REACTION = "event_reaction"
    EXECUTION_BEHAVIOUR = "execution_behaviour"
    PORTFOLIO_BEHAVIOUR = "portfolio_behaviour"
    FEATURE_IMPORTANCE = "feature_importance"
    FEATURE_INSTABILITY = "feature_instability"
    OUTCOME_ANOMALY = "outcome_anomaly"
    EXPERIMENT_FAILURE = "experiment_failure"
    EXPERIMENT_SUCCESS = "experiment_success"


class QuestionSource(str, Enum):
    """Where a question came from (§5). Provenance, never inferred."""
    MEMORY_PATTERN = "memory_pattern"
    ERROR_PATTERN = "error_pattern"
    MODEL_ANALYSIS = "model_analysis"
    SIGNAL_ANALYSIS = "signal_analysis"
    REGIME_ANALYSIS = "regime_analysis"
    EVENT_ANALYSIS = "event_analysis"
    EXPERIMENT_RESULT = "experiment_result"
    HUMAN_INPUT = "human_input"


class TriageState(str, Enum):
    """
    Whether a question deserves an experiment (§6).

    TESTABLE is the only state that may become a hypothesis. The other
    seven exist so that "we looked at this and decided not to" is a
    recorded decision rather than a silence.
    """
    IGNORED = "ignored"
    LOW_PRIORITY = "low_priority"
    QUEUED = "queued"
    RESEARCHING = "researching"
    TESTABLE = "testable"
    UNTESTABLE = "untestable"
    DUPLICATE = "duplicate"
    INSUFFICIENT_DATA = "insufficient_data"


class QueueState(str, Enum):
    """Where an item sits in the research queue (§25)."""
    QUEUED = "queued"
    PRIORITIZED = "prioritized"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ConclusionType(str, Enum):
    """
    How a piece of research ended (§17, §43, §44).

    Five of the six are ways of not having found something, and that
    ratio is deliberate. `SUPPORTED` is reachable only through
    `research_quality_gate`, never by a metric moving in the pleasant
    direction.
    """
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    INSUFFICIENT_DATA = "insufficient_data"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class ResearchConfidence(str, Enum):
    """
    Confidence in a research conclusion (§40).

    Deliberately NOT the same scale as model confidence, signal
    confidence or memory confidence. Those describe a prediction; this
    describes how much weight a finding about the system can bear.
    """
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class CandidateType(str, Enum):
    FEATURE = "feature"
    MODEL = "model"
    SIGNAL = "signal"
    STRATEGY = "strategy"
    RISK = "risk"
    EXECUTION = "execution"


class CandidateStatus(str, Enum):
    """
    §55. `PROMOTED` exists in the vocabulary and is unreachable from
    this phase's code: there is no transition into it, and
    `tests/autoresearch/test_boundary_and_safety.py` fails if one
    appears. It is here so Phase 24 does not have to invent it, and so
    the absence of the transition is visible rather than implied.
    """
    PROPOSED = "proposed"
    TESTING = "testing"
    PROMISING = "promising"
    REJECTED = "rejected"
    READY_FOR_REVIEW = "ready_for_review"
    PROMOTED = "promoted"


class Actor(str, Enum):
    """Who took a research action (§77)."""
    HUMAN = "human"
    SYSTEM = "system"
    LLM = "llm"


class FamilyStatus(str, Enum):
    """§65, §66: a family that keeps failing, and one that revives."""
    ACTIVE = "active"
    LOW_PRIORITY = "low_priority"
    RESEARCH_DEPLETED = "research_depleted"
    REACTIVATED = "reactivated"


# ======================================================================
# Evidence
# ======================================================================

@dataclass
class Evidence:
    """
    A pointer to a row, never a restatement of it (§4, §37).

    `kind` names the table, `reference` the key, `detail` the number
    that made it worth citing. Anything the researcher asserts must
    hang off one of these, which is what makes §46 enforceable: an
    association can be evidenced, a cause cannot.
    """
    kind: str
    reference: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "reference": self.reference,
                "detail": self.detail}


# ======================================================================
# Observation
# ======================================================================

@dataclass
class ResearchObservation:
    """
    Something the researcher noticed, with the rows that show it.

    An observation is not yet a question and definitely not a finding.
    It is the raw material, and it carries `sample_size` so that
    everything downstream can refuse to build on eight rows.
    """
    observation_id: str
    kind: ObservationKind
    subject: str
    statement: str
    sample_size: int
    evidence: List[Evidence] = field(default_factory=list)
    measures: Dict[str, Any] = field(default_factory=dict)
    source_kind: QuestionSource = QuestionSource.MEMORY_PATTERN
    source_reference: str = ""
    method_version: str = RESEARCH_METHOD_VERSION
    observed_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.statement.strip():
            raise ValueError("an observation with no statement observes nothing")
        if not self.evidence:
            raise ValueError(
                "an observation must reference evidence (§4). An unevidenced "
                "observation is an opinion, and the whole point of the "
                "research trail is that every claim ends at a row.")

    @property
    def has_enough_sample(self) -> bool:
        return self.sample_size >= MIN_RESEARCH_SAMPLE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id, "kind": self.kind.value,
            "subject": self.subject, "statement": self.statement,
            "sample_size": self.sample_size,
            "evidence": [e.as_dict() for e in self.evidence],
            "measures": self.measures,
            "source_kind": self.source_kind.value,
            "source_reference": self.source_reference,
            "method_version": self.method_version,
            "observed_at": self.observed_at,
        }


# ======================================================================
# Question
# ======================================================================

@dataclass
class ResearchQuestion:
    """
    A question the record could answer (§5).

    `triage` is the interesting field. Most observations should not
    become experiments — §6 is explicit — so a question carries the
    decision not to pursue it, with a reason, rather than being
    dropped.
    """
    question_id: str
    title: str
    question: str
    description: str = ""
    source_type: QuestionSource = QuestionSource.MEMORY_PATTERN
    source_id: str = ""
    observation_id: str = ""
    triage: TriageState = TriageState.QUEUED
    triage_reason: str = ""
    priority: float = 0.0
    evidence: List[Evidence] = field(default_factory=list)
    sample_size: int = 0
    method_version: str = RESEARCH_METHOD_VERSION
    created_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.question.strip().endswith("?"):
            raise ValueError(
                "a research question must be phrased as a question. This is "
                "not pedantry: a statement dressed as a question is usually a "
                "conclusion someone has already reached.")
        if not self.evidence:
            raise ValueError("a question must carry the evidence that raised it")

    @property
    def is_testable(self) -> bool:
        return self.triage == TriageState.TESTABLE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id, "title": self.title,
            "question": self.question, "description": self.description,
            "source_type": self.source_type.value, "source_id": self.source_id,
            "observation_id": self.observation_id,
            "triage": self.triage.value, "triage_reason": self.triage_reason,
            "priority": self.priority, "sample_size": self.sample_size,
            "evidence": [e.as_dict() for e in self.evidence],
            "method_version": self.method_version, "created_at": self.created_at,
        }


# ======================================================================
# Hypothesis
# ======================================================================

@dataclass
class FalsifiabilityCriteria:
    """
    What would make this hypothesis fail (§10).

    Written down BEFORE the test, and carried into the Phase 22
    `AcceptanceCriteria` that goes inside the experiment fingerprint —
    so once the experiment starts, these cannot move either.

    `acceptable_degradation` is the field that stops a one-sided test.
    A hypothesis that can only be confirmed is not falsifiable; naming
    how much worse a secondary metric may get is what makes a
    "improves X" claim refutable rather than decorative.
    """
    expected_result: str
    minimum_effect: float = 0.02
    acceptable_degradation: float = 0.0
    minimum_sample: int = MIN_RESEARCH_SAMPLE
    require_interval_excludes_zero: bool = True
    evaluation_metric: str = "directional_accuracy"

    def validate(self) -> None:
        if not self.expected_result.strip():
            raise ValueError("falsifiability needs a stated expected result")
        if self.minimum_effect <= 0:
            raise ValueError(
                "a minimum effect of zero makes every outcome a success, "
                "which is the definition of an unfalsifiable claim")
        if self.minimum_sample < MIN_RESEARCH_SAMPLE:
            raise ValueError(
                "minimum_sample below %d contradicts every other layer in "
                "the project" % MIN_RESEARCH_SAMPLE)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "expected_result": self.expected_result,
            "minimum_effect": self.minimum_effect,
            "acceptable_degradation": self.acceptable_degradation,
            "minimum_sample": self.minimum_sample,
            "require_interval_excludes_zero": self.require_interval_excludes_zero,
            "evaluation_metric": self.evaluation_metric,
        }


#: Words that make a hypothesis untestable no matter what follows them.
#: §9's bad example -- "Maybe momentum is bad" -- fails on two counts,
#: and both are cheap to detect.
_VAGUE_TERMS = ("maybe", "perhaps", "might be", "seems", "probably",
                "somewhat", "generally better", "generally worse", "feels")


@dataclass
class ResearchHypothesis:
    """
    A specific, testable, falsifiable, measurable claim (§9).

    The quality gate is `validate()`, and it refuses more than it
    accepts on purpose. A hypothesis that survives it names a
    population, a condition, a direction, a metric, and what would
    count as being wrong.
    """
    hypothesis_id: str
    question_id: str
    statement: str
    mechanism: str
    population: str
    condition: Dict[str, Any]
    expected_direction: str
    falsifiability: FalsifiabilityCriteria
    family_id: str = ""
    family_name: str = ""
    source: QuestionSource = QuestionSource.MEMORY_PATTERN
    source_reference: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    sample_size: int = 0
    #: A registered Phase 22 evaluator name. Never a callable, never a
    #: code string -- §29 forbids arbitrary executable generation, and
    #: the structural way to forbid it is for the field to be unable to
    #: express it.
    evaluator: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    baseline: str = "all_signals"
    author: Actor = Actor.SYSTEM
    method_version: str = RESEARCH_METHOD_VERSION
    created_at: str = field(default_factory=utcnow)

    # -- quality ----------------------------------------------------
    def quality_problems(self) -> List[str]:
        """
        Every reason this hypothesis is not yet testable.

        Returns a list rather than raising so a caller can show a
        researcher all of the problems at once, and so triage can
        record *why* something was marked UNTESTABLE.
        """
        problems: List[str] = []
        text = self.statement.strip()
        if not text:
            problems.append("no statement")
        lowered = text.lower()
        for term in _VAGUE_TERMS:
            if term in lowered:
                problems.append("vague wording: %r is not measurable" % term)
        if not self.mechanism.strip():
            problems.append(
                "no proposed mechanism: without one this is a pattern match, "
                "not a hypothesis")
        if not self.population.strip():
            problems.append("no population: the claim does not say about what")
        if not self.condition:
            problems.append(
                "no condition: nothing distinguishes the candidate from the "
                "baseline, so there is nothing to test")
        if self.expected_direction not in ("increase", "decrease", "change"):
            problems.append(
                "expected direction must be increase, decrease or change")
        if not self.evaluator:
            problems.append("no registered evaluator, so this cannot be run")
        try:
            self.falsifiability.validate()
        except ValueError as exc:
            problems.append(str(exc))
        return problems

    def validate(self) -> None:
        problems = self.quality_problems()
        if problems:
            raise ValueError("; ".join(problems))

    @property
    def is_quality(self) -> bool:
        return not self.quality_problems()

    # -- identity ---------------------------------------------------
    @property
    def claim_fingerprint(self) -> str:
        """
        What this hypothesis CLAIMS, ignoring how it is worded (§13).

        Two hypotheses proposing the same condition over the same
        population, measured the same way and expected to move the same
        direction, are the same claim however differently they are
        written. Phase 22 learned this the expensive way: its proposal
        generator produced three "different" experiments that turned
        out to be one comparison, and the per-family multiple-testing
        correction did not catch them because they sat in three
        different families.

        So deduplication keys on the claim, not the prose.
        """
        return _digest({
            "population": self.population.strip().lower(),
            "condition": self.condition,
            "direction": self.expected_direction,
            "metric": self.falsifiability.evaluation_metric,
            "evaluator": self.evaluator,
            "parameters": self.parameters,
            "baseline": self.baseline,
        })

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id, "question_id": self.question_id,
            "statement": self.statement, "mechanism": self.mechanism,
            "population": self.population, "condition": self.condition,
            "expected_direction": self.expected_direction,
            "falsifiability": self.falsifiability.as_dict(),
            "family_id": self.family_id, "family_name": self.family_name,
            "source": self.source.value,
            "source_reference": self.source_reference,
            "evidence": [e.as_dict() for e in self.evidence],
            "sample_size": self.sample_size, "evaluator": self.evaluator,
            "parameters": self.parameters, "baseline": self.baseline,
            "author": self.author.value,
            "claim_fingerprint": self.claim_fingerprint,
            "method_version": self.method_version, "created_at": self.created_at,
        }


# ======================================================================
# Priority and cost
# ======================================================================

@dataclass
class ResearchCost:
    """
    What answering a question is expected to cost (§12).

    Estimated before running, stored beside the result, so a family
    that consumed forty runs to find nothing is visible as forty runs.
    """
    rows_required: int = 0
    variants: int = 1
    estimated_seconds: float = 0.0
    complexity: int = 1

    @property
    def score(self) -> float:
        """Normalised 0..1, where 1 is expensive."""
        rows = min(self.rows_required / 20000.0, 1.0)
        variants = min(self.variants / 20.0, 1.0)
        seconds = min(self.estimated_seconds / 600.0, 1.0)
        return round(min(0.4 * rows + 0.4 * variants + 0.2 * seconds, 1.0), 4)

    def as_dict(self) -> Dict[str, Any]:
        return {"rows_required": self.rows_required, "variants": self.variants,
                "estimated_seconds": self.estimated_seconds,
                "complexity": self.complexity, "score": self.score}


@dataclass
class PriorityScore:
    """
    Why this question is worth asking before that one (§11).

    Every component is stored, not just the total. A single number
    would be unarguable, and a research priority that cannot be argued
    with is a research priority nobody will trust.

    NOTE ON WHAT IS ABSENT: there is no `predicted_profit` component.
    §11 forbids ranking on predicted profitability alone, and the
    honest way to obey that is not to compute it at all -- a component
    that exists gets weighted eventually.
    """
    evidence_strength: float = 0.0      # how much record stands behind it
    sample_adequacy: float = 0.0        # is there enough to test on
    novelty: float = 0.0                # distance from what was tried
    weakness_relevance: float = 0.0     # does it target a known weakness
    confidence: float = 0.0             # how sure the observation is
    cost_penalty: float = 0.0           # cheaper is better, all else equal
    risk_penalty: float = 0.0           # research risk, not trading risk

    WEIGHTS = {
        "evidence_strength": 0.25,
        "sample_adequacy": 0.20,
        "novelty": 0.15,
        "weakness_relevance": 0.20,
        "confidence": 0.20,
    }

    @property
    def total(self) -> float:
        positive = sum(getattr(self, name) * weight
                       for name, weight in self.WEIGHTS.items())
        return round(max(0.0, min(1.0, positive
                                  - 0.15 * self.cost_penalty
                                  - 0.10 * self.risk_penalty)), 4)

    def explain(self) -> List[str]:
        parts = ["%s %.2f (weight %.2f)" % (name, getattr(self, name), weight)
                 for name, weight in sorted(self.WEIGHTS.items())]
        parts.append("cost penalty %.2f" % self.cost_penalty)
        parts.append("risk penalty %.2f" % self.risk_penalty)
        return parts

    def as_dict(self) -> Dict[str, Any]:
        data = {name: getattr(self, name) for name in self.WEIGHTS}
        data.update({"cost_penalty": self.cost_penalty,
                     "risk_penalty": self.risk_penalty,
                     "total": self.total})
        return data


# ======================================================================
# Budget
# ======================================================================

@dataclass
class ResearchBudget:
    """
    The limits that stop a research explosion (§24, §52, §70).

    Every one of these is a refusal, never a truncation. A budget that
    silently trimmed a sweep would answer a different question than
    the one asked and the reader could not tell -- the same principle
    Phase 22 applied to `max_rows`.
    """
    max_experiments_per_cycle: int = 5
    max_experiments_per_day: int = 25
    max_variants_per_experiment: int = 12
    max_repeats_per_family: int = 8
    max_runtime_seconds: float = 900.0
    max_rows_scanned: int = 200000
    max_concurrent_jobs: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_experiments_per_cycle": self.max_experiments_per_cycle,
            "max_experiments_per_day": self.max_experiments_per_day,
            "max_variants_per_experiment": self.max_variants_per_experiment,
            "max_repeats_per_family": self.max_repeats_per_family,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_rows_scanned": self.max_rows_scanned,
            "max_concurrent_jobs": self.max_concurrent_jobs,
        }


class BudgetExceeded(Exception):
    """A limit was reached. Refused with a reason, never truncated."""


# ======================================================================
# Overfitting warnings
# ======================================================================

#: §19. Each is a shape in the result, not a judgement about intent.
OVERFITTING_WARNINGS = (
    "single_parameter_peak",
    "narrow_time_period",
    "single_instrument",
    "single_regime",
    "many_variants",
    "test_set_reuse",
    "unstable_sign",
    "large_train_test_gap",
)


def overfitting_warnings(*, effect: Optional[float],
                         effect_in_sample: Optional[float],
                         sensitivity_shape: str = "",
                         instrument_count: int = 0,
                         regime_count: int = 0,
                         variants: int = 0,
                         window_reuse_count: int = 0,
                         span_days: float = 0.0,
                         slices_total: int = 0,
                         slices_passing: int = 0) -> List[str]:
    """
    Which of §19's shapes this result has.

    Returns names, not a score. A score would invite a threshold, and
    a threshold would invite tuning it until results stopped being
    flagged. Each warning is a fact about the result that a reader can
    check against the result.
    """
    found: List[str] = []
    if sensitivity_shape == "single_point":
        found.append("single_parameter_peak")
    if span_days and span_days < 60:
        found.append("narrow_time_period")
    if instrument_count == 1:
        found.append("single_instrument")
    if regime_count == 1:
        found.append("single_regime")
    if variants >= 10:
        found.append("many_variants")
    if window_reuse_count >= 3:
        found.append("test_set_reuse")
    if (effect is not None and effect_in_sample is not None
            and effect * effect_in_sample < 0):
        found.append("unstable_sign")
    if (effect is not None and effect_in_sample is not None
            and (effect_in_sample - effect) > 0.05):
        found.append("large_train_test_gap")
    if slices_total and slices_passing * 2 < slices_total:
        if "unstable_sign" not in found:
            found.append("unstable_sign")
    return found


# ======================================================================
# Conclusion
# ======================================================================

@dataclass
class ResearchConclusion:
    """
    What the research found, and how much weight it can bear.

    `reasons` is mandatory in spirit and enforced in `validate()`: a
    conclusion with no reasoning is an assertion, and Phase 22 already
    established that a research result which cannot be argued with is
    not a research result.
    """
    conclusion_id: str
    hypothesis_id: str
    question_id: str
    experiment_id: str = ""
    conclusion: ConclusionType = ConclusionType.INCONCLUSIVE
    confidence: ResearchConfidence = ResearchConfidence.INSUFFICIENT
    effect: Optional[float] = None
    effect_in_sample: Optional[float] = None
    effect_low: Optional[float] = None
    effect_high: Optional[float] = None
    sample_size: int = 0
    reasons: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    family_experiment_count: int = 1
    promising: bool = False
    method_version: str = RESEARCH_METHOD_VERSION
    concluded_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.reasons:
            raise ValueError(
                "a conclusion with no reasons is an assertion. Every check "
                "that produced this verdict must be recorded, including the "
                "ones that passed.")
        if self.promising and self.conclusion is not ConclusionType.SUPPORTED:
            raise ValueError(
                "only a SUPPORTED conclusion may be marked promising (§54)")

    @property
    def is_negative(self) -> bool:
        return self.conclusion in (ConclusionType.REJECTED,
                                   ConclusionType.INCONCLUSIVE,
                                   ConclusionType.INSUFFICIENT_DATA,
                                   ConclusionType.CONFLICTING_EVIDENCE)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "conclusion_id": self.conclusion_id,
            "hypothesis_id": self.hypothesis_id,
            "question_id": self.question_id,
            "experiment_id": self.experiment_id,
            "conclusion": self.conclusion.value,
            "confidence": self.confidence.value,
            "effect": self.effect, "effect_in_sample": self.effect_in_sample,
            "effect_low": self.effect_low, "effect_high": self.effect_high,
            "sample_size": self.sample_size, "reasons": self.reasons,
            "limitations": self.limitations, "warnings": self.warnings,
            "evidence": [e.as_dict() for e in self.evidence],
            "family_experiment_count": self.family_experiment_count,
            "promising": self.promising,
            "method_version": self.method_version,
            "concluded_at": self.concluded_at,
        }


def assess_confidence(*, sample_size: int, effect_low: Optional[float],
                      effect_high: Optional[float],
                      slices_total: int, slices_passing: int,
                      warnings: Sequence[str]) -> ResearchConfidence:
    """
    Evidence-based confidence in a FINDING (§40).

    Not a probability and not calibrated -- an ordinal label, in the
    same spirit as Phase 21's memory confidence and deliberately on a
    separate scale so the two cannot be confused or multiplied.
    """
    if sample_size < MIN_RESEARCH_SAMPLE:
        return ResearchConfidence.INSUFFICIENT
    if effect_low is None or effect_high is None:
        return ResearchConfidence.LOW
    spans_zero = effect_low <= 0 <= effect_high
    robust = slices_total and (slices_passing / slices_total) >= 0.6
    serious = [w for w in warnings
               if w in ("test_set_reuse", "single_parameter_peak",
                        "large_train_test_gap", "unstable_sign")]
    if spans_zero:
        return ResearchConfidence.LOW
    if robust and not serious and sample_size >= 4 * MIN_RESEARCH_SAMPLE:
        return ResearchConfidence.HIGH
    if robust and len(serious) <= 1:
        return ResearchConfidence.MEDIUM
    return ResearchConfidence.LOW


# ======================================================================
# Candidate
# ======================================================================

@dataclass
class ResearchCandidate:
    """
    Something a promising result suggests trying (§55, §56).

    It is a RECORD, not a deployment. There is no code path from here
    into a model, a strategy, a threshold or a capital figure, and the
    absence is tested rather than asserted.
    """
    candidate_id: str
    candidate_type: CandidateType
    name: str
    hypothesis_id: str
    conclusion_id: str
    experiment_id: str = ""
    status: CandidateStatus = CandidateStatus.PROPOSED
    base_version: str = ""
    changes: Dict[str, Any] = field(default_factory=dict)
    dataset_snapshot_id: str = ""
    code_version: str = ""
    requires_review: bool = True
    review_reason: str = ""
    effect: Optional[float] = None
    method_version: str = RESEARCH_METHOD_VERSION
    created_at: str = field(default_factory=utcnow)

    def validate(self) -> None:
        if not self.base_version:
            raise ValueError(
                "a candidate must name the version it changes (§56); a "
                "change with no base is not reproducible")
        if not self.changes:
            raise ValueError("a candidate that changes nothing is not a candidate")
        if self.status is CandidateStatus.PROMOTED:
            raise ValueError(
                "Phase 23 cannot create a promoted candidate. Promotion is a "
                "human decision under the Phase 18 gate (§34, §55).")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type.value, "name": self.name,
            "hypothesis_id": self.hypothesis_id,
            "conclusion_id": self.conclusion_id,
            "experiment_id": self.experiment_id, "status": self.status.value,
            "base_version": self.base_version, "changes": self.changes,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "code_version": self.code_version,
            "requires_review": self.requires_review,
            "review_reason": self.review_reason, "effect": self.effect,
            "method_version": self.method_version, "created_at": self.created_at,
        }


# ======================================================================
# The quality gate
# ======================================================================

def research_quality_gate(*, conclusion: ConclusionType,
                          effect: Optional[float],
                          falsifiability: FalsifiabilityCriteria,
                          sample_size: int,
                          effect_low: Optional[float],
                          effect_high: Optional[float],
                          slices_total: int, slices_passing: int,
                          complexity_ratio: Optional[float],
                          warnings: Sequence[str]) -> Tuple[bool, List[str]]:
    """
    Whether a conclusion may be called PROMISING (§54).

    Returns `(promising, reasons)` and the reasons are returned in both
    cases. A gate that only explains itself when it says no teaches
    readers to skip the explanation.

    The criteria are the ones stated up front in the hypothesis's own
    falsifiability record, plus the structural checks that no
    hypothesis is allowed to waive: sample, interval, robustness,
    complexity, and the absence of the serious overfitting shapes.
    """
    reasons: List[str] = []
    ok = True

    if conclusion is not ConclusionType.SUPPORTED:
        return False, ["not promising: the conclusion is %s, and only a "
                       "SUPPORTED conclusion can be promising"
                       % conclusion.value]

    if sample_size < falsifiability.minimum_sample:
        ok = False
        reasons.append("sample %d is below the %d fixed before the test"
                       % (sample_size, falsifiability.minimum_sample))
    else:
        reasons.append("met: sample %d clears the %d fixed before the test"
                       % (sample_size, falsifiability.minimum_sample))

    if effect is None or effect < falsifiability.minimum_effect:
        ok = False
        reasons.append("out-of-sample effect %s is below the %+0.4f required"
                       % ("unmeasured" if effect is None else "%+0.4f" % effect,
                          falsifiability.minimum_effect))
    else:
        reasons.append("met: out-of-sample effect %+0.4f clears %+0.4f"
                       % (effect, falsifiability.minimum_effect))

    if falsifiability.require_interval_excludes_zero:
        if effect_low is None or effect_high is None:
            ok = False
            reasons.append("no interval could be computed, so the effect "
                           "cannot be distinguished from noise")
        elif effect_low <= 0 <= effect_high:
            ok = False
            reasons.append("the interval [%+0.4f, %+0.4f] includes zero"
                           % (effect_low, effect_high))
        else:
            reasons.append("met: the interval [%+0.4f, %+0.4f] excludes zero"
                           % (effect_low, effect_high))

    if slices_total:
        fraction = slices_passing / slices_total
        if fraction < 0.6:
            ok = False
            reasons.append("the effect held in %d of %d slices (%.0f%%), "
                           "against 60%% required"
                           % (slices_passing, slices_total, 100 * fraction))
        else:
            reasons.append("met: the effect held in %d of %d slices"
                           % (slices_passing, slices_total))
    else:
        ok = False
        reasons.append("no robustness slices were measured")

    if complexity_ratio is not None and complexity_ratio > 5.0:
        ok = False
        reasons.append("the candidate is %.1fx as complex as the baseline"
                       % complexity_ratio)

    serious = [w for w in warnings
               if w in ("test_set_reuse", "single_parameter_peak",
                        "large_train_test_gap", "unstable_sign")]
    if serious:
        ok = False
        reasons.append("overfitting warnings present: " + ", ".join(serious))

    if ok:
        reasons.append(
            "PROMISING means the criteria written before the test were met. "
            "It does not mean profitable, and it does not authorise any "
            "change to production.")
    return ok, reasons


def novelty_against(claim: str, previous: Sequence[str]) -> float:
    """
    How far a claim sits from what has already been tried (§64).

    A crude, honest measure: 0.0 if this exact claim has been tested
    before, 1.0 if nothing has. Novelty is NOT evidence of usefulness —
    §64 says so and the priority weighting treats it as one component
    of five rather than as a reason on its own.
    """
    if not previous:
        return 1.0
    return 0.0 if claim in set(previous) else 1.0
