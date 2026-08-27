"""
src/domain/model_models.py
-------------------------------
Modeling domain models (Phase 9).

THE STRUCTURAL COMMITMENT (spec §5, §6, §7, §51): a model produces a
PREDICTION. A prediction is not a decision, not a recommendation, not
a signal. Nothing in this file carries a position size, an action, or
a trade instruction — the absence is deliberate, so a later phase must
introduce its own types rather than quietly widening these.

THE SECOND COMMITMENT (spec §23, §24, §57): every prediction records
the model version, feature set version, and information cutoff that
produced it. A prediction whose provenance cannot be reconstructed is
worthless for validation, and the fields are mandatory rather than
optional so one cannot be omitted by accident.

CALIBRATION IS NOT ACCURACY (spec §31, §32, §33): a model can be
accurate and badly calibrated, or poorly discriminating and perfectly
calibrated. They are separate properties, stored separately, and
CalibrationReport exists precisely so a "0.8 confidence" claim can be
checked against what actually happened 0.8-confidence predictions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _require_utc(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{name} must be in UTC (got offset {value.utcoffset()})")
    return value


# ============================================================
# Problem framing (spec §8, §9, §10, §11)
# ============================================================

class PredictionTask(str, Enum):
    """
    What the model is asked to predict.

    DIRECTION and MAGNITUDE are deliberately distinct tasks (spec §9):
    "will it go up" and "how far will it move" are different questions
    with different error structures, and a single model answering both
    tends to answer neither well.
    """
    DIRECTION = "direction"                  # classification: up / down / flat
    MAGNITUDE = "magnitude"                  # regression: how large a move
    VOLATILITY = "volatility"                # regression: dispersion, not direction
    ABNORMAL_RETURN = "abnormal_return"      # regression: benchmark-adjusted
    RANKING = "ranking"                      # relative ordering, not absolute value


class ModelFamily(str, Enum):
    """
    Deliberately restricted (spec §36, §37, §38): simple, interpretable
    families only. There is no deep-learning member, because this
    phase's own instruction is to establish honest evaluation first —
    a complex model whose failures cannot be diagnosed is worse than a
    simple one whose limits are visible.
    """
    BASELINE_CONSTANT = "baseline_constant"
    BASELINE_HISTORICAL_MEAN = "baseline_historical_mean"
    BASELINE_MAJORITY_CLASS = "baseline_majority_class"
    BASELINE_RANDOM = "baseline_random"
    LINEAR_REGRESSION = "linear_regression"
    RIDGE_REGRESSION = "ridge_regression"
    LOGISTIC_REGRESSION = "logistic_regression"
    DECISION_STUMP = "decision_stump"


class ModelStatus(str, Enum):
    """
    Lifecycle (spec §54, §55).

    RETIRED models are KEPT, never deleted: predictions they made are
    still on record, and a prediction whose model has vanished cannot
    be audited.
    """
    DRAFT = "draft"
    TRAINED = "trained"
    EVALUATED = "evaluated"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RETIRED = "retired"


# ============================================================
# Specification (spec §22, §23)
# ============================================================

@dataclass
class ModelSpecification:
    """
    Everything that defines a model BEFORE it is trained.

    Frozen in identity by (model_id, version): changing a
    hyperparameter or the feature set means a new version, never an
    edit — otherwise a stored evaluation would silently refer to a
    model that no longer exists.
    """
    model_id: str
    name: str
    task: PredictionTask
    family: ModelFamily
    version: str = "v1"
    description: str = ""

    feature_set_id: Optional[str] = None
    feature_set_version: Optional[str] = None
    label_name: str = ""
    label_version: str = "v1"
    dataset_version: Optional[str] = None
    cohort_id: Optional[str] = None

    hyperparameters: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        self.created_at = _require_utc(self.created_at, "created_at")

    @property
    def qualified_id(self) -> str:
        return f"{self.model_id}:{self.version}"

    def fingerprint(self) -> str:
        """Everything that determines what training will produce. Two identical fingerprints must yield identical models."""
        parameters = ";".join(f"{k}={v}" for k, v in sorted(self.hyperparameters.items()))
        return "|".join([
            self.qualified_id, self.task.value, self.family.value,
            self.feature_set_id or "", self.feature_set_version or "",
            self.label_name, self.label_version,
            self.dataset_version or "", self.cohort_id or "", parameters,
        ])


# ============================================================
# Training result (spec §17, §18, §23)
# ============================================================

@dataclass
class TrainingWindow:
    """One train/test boundary pair. Test always strictly follows train (spec §13)."""
    label: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_size: int = 0
    test_size: int = 0
    purged_count: int = 0
    embargoed_count: int = 0

    def __post_init__(self):
        for name in ("train_start", "train_end", "test_start", "test_end"):
            _require_utc(getattr(self, name), name)
        if self.test_start < self.train_end:
            raise ValueError(f"window '{self.label}': test period must not start before training ends")


@dataclass
class TrainedModel:
    """
    A model that has been fitted, with the exact context it was fitted
    in.

    `parameters` holds whatever the algorithm learned (coefficients,
    a threshold, a constant). Kept as plain data so a model can be
    inspected, serialized, and re-applied without the training code.
    """
    trained_model_id: str
    specification: ModelSpecification
    parameters: Dict[str, Any] = field(default_factory=dict)
    feature_names: List[str] = field(default_factory=list)

    train_start: Optional[datetime] = None
    train_end: Optional[datetime] = None
    train_sample_size: int = 0
    train_cluster_count: Optional[int] = None

    status: ModelStatus = ModelStatus.TRAINED
    trained_at: Optional[datetime] = None
    training_notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        for name in ("train_start", "train_end", "trained_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    @property
    def qualified_id(self) -> str:
        return f"{self.specification.qualified_id}@{self.trained_model_id}"

    @property
    def effective_sample_size(self) -> int:
        """
        Cluster count when known, otherwise raw sample size (spec §19).

        Observations from one catalyst are not independent — 400 rows
        from 12 event clusters is closer to a 12-sample problem, and
        reporting 400 would overstate the evidence badly.
        """
        return self.train_cluster_count or self.train_sample_size


# ============================================================
# Prediction (spec §5, §6, §7, §24, §51)
# ============================================================

@dataclass
class Prediction:
    """
    One model output, with full provenance.

    NOTE THE ABSENCE (spec §7, §51): no `action`, no `position_size`,
    no `signal`, no `recommendation`. A prediction is an estimate with
    an uncertainty attached. Turning it into a decision requires risk
    limits, costs, and portfolio context that this phase deliberately
    does not have.
    """
    prediction_id: str
    trained_model_id: str
    model_qualified_id: str
    observation_id: str

    predicted_value: Optional[float] = None
    predicted_class: Optional[str] = None
    class_probabilities: Dict[str, float] = field(default_factory=dict)

    # Uncertainty is MANDATORY in spirit (spec §26): a point estimate
    # with no stated uncertainty invites false confidence.
    confidence: Optional[float] = None
    prediction_interval_low: Optional[float] = None
    prediction_interval_high: Optional[float] = None
    uncertainty_basis: str = ""

    information_cutoff: Optional[datetime] = None
    feature_set_version: Optional[str] = None
    predicted_at: Optional[datetime] = None

    # Spec §27: a model must be able to say "I don't know".
    is_abstention: bool = False
    abstention_reason: str = ""

    def __post_init__(self):
        for name in ("information_cutoff", "predicted_at"):
            setattr(self, name, _require_utc(getattr(self, name), name))

    def validate(self) -> List[str]:
        """Provenance completeness check (spec §24). Empty list means the prediction is auditable."""
        problems = []
        if not self.model_qualified_id:
            problems.append("no model version recorded")
        if not self.feature_set_version:
            problems.append("no feature set version recorded")
        if self.information_cutoff is None:
            problems.append("no information cutoff recorded")
        if not self.is_abstention and self.predicted_value is None and self.predicted_class is None:
            problems.append("prediction has no value and is not an abstention")
        return problems


# ============================================================
# Evaluation (spec §25, §28, §29, §30)
# ============================================================

@dataclass
class BaselineComparison:
    """
    A model's performance NEXT TO the naive alternatives (spec §28,
    §29).

    Reported together, always. A model with 54% accuracy sounds
    reasonable until the majority-class baseline is shown at 53% — and
    a metric presented without that comparison is close to
    meaningless.
    """
    baseline_name: str
    baseline_score: Optional[float] = None
    model_score: Optional[float] = None
    metric_name: str = ""

    @property
    def improvement(self) -> Optional[float]:
        if self.model_score is None or self.baseline_score is None:
            return None
        return round(self.model_score - self.baseline_score, 6)

    @property
    def beats_baseline(self) -> Optional[bool]:
        improvement = self.improvement
        return None if improvement is None else improvement > 0


@dataclass
class CalibrationBin:
    """One confidence bucket, and what actually happened inside it."""
    lower: float
    upper: float
    count: int = 0
    mean_predicted: Optional[float] = None
    observed_frequency: Optional[float] = None

    @property
    def gap(self) -> Optional[float]:
        """Predicted minus observed. Positive means the model was overconfident in this bucket."""
        if self.mean_predicted is None or self.observed_frequency is None:
            return None
        return round(self.mean_predicted - self.observed_frequency, 6)


@dataclass
class CalibrationReport:
    """
    Whether stated confidence matches observed outcomes (spec §31,
    §32, §33).

    Separate from accuracy on purpose: these are different properties,
    and a system that reports only accuracy can be confidently wrong
    in a way nobody notices.
    """
    model_qualified_id: str
    bins: List[CalibrationBin] = field(default_factory=list)
    sample_size: int = 0
    expected_calibration_error: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def is_overconfident(self) -> Optional[bool]:
        """
        Whether the model's stated confidence systematically exceeds
        its observed hit rate. None when there is too little evidence
        to say — absence of data is not evidence of good calibration.
        """
        gaps = [b.gap for b in self.bins if b.gap is not None and b.count > 0]
        if len(gaps) < 2:
            return None
        return sum(gaps) / len(gaps) > 0.05


@dataclass
class ModelEvaluation:
    """
    Out-of-sample results for one trained model (spec §25, §30, §34).

    `is_deployable` is deliberately conservative and gate-like: a
    model that does not beat its baseline, or rests on too small an
    effective sample, is not "slightly worse" — it is not evidence of
    anything, and the flag says so.
    """
    evaluation_id: str
    trained_model_id: str
    model_qualified_id: str

    window_label: str = ""
    sample_size: int = 0
    cluster_count: Optional[int] = None

    metrics: Dict[str, float] = field(default_factory=dict)
    baseline_comparisons: List[BaselineComparison] = field(default_factory=list)
    calibration: Optional[CalibrationReport] = None

    abstention_rate: Optional[float] = None
    evaluated_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    #: Below this many independent clusters, results are descriptive
    #: colour rather than evidence.
    MIN_EFFECTIVE_SAMPLE = 30

    def __post_init__(self):
        self.evaluated_at = _require_utc(self.evaluated_at, "evaluated_at")

    @property
    def effective_sample_size(self) -> int:
        return self.cluster_count or self.sample_size

    @property
    def small_sample(self) -> bool:
        return self.effective_sample_size < self.MIN_EFFECTIVE_SAMPLE

    @property
    def beats_all_baselines(self) -> Optional[bool]:
        results = [c.beats_baseline for c in self.baseline_comparisons if c.beats_baseline is not None]
        return all(results) if results else None

    @property
    def is_deployable(self) -> Optional[bool]:
        """
        Whether this model has earned any further consideration.

        Requires: beats every baseline AND has a large enough effective
        sample. Returns None when it cannot be judged — which is
        different from False, and both are different from True.
        """
        if self.beats_all_baselines is None:
            return None
        if self.small_sample:
            return False
        return self.beats_all_baselines

    def honest_summary(self) -> str:
        """A plain-language statement that never overstates what was found (spec §30, §52)."""
        if self.beats_all_baselines is None:
            return "No baseline comparison available — this result cannot be interpreted."
        if self.small_sample:
            return (f"Effective sample of {self.effective_sample_size} is below the "
                     f"{self.MIN_EFFECTIVE_SAMPLE} threshold; treat these numbers as descriptive, not evidence.")
        if not self.beats_all_baselines:
            return "Does not beat naive baselines — this model has no demonstrated predictive value."
        return (f"Beats all {len(self.baseline_comparisons)} baseline(s) on an effective sample of "
                 f"{self.effective_sample_size}. Out-of-sample only; no live validation has occurred.")


# ============================================================
# Drift & monitoring (spec §44, §45)
# ============================================================

@dataclass
class DriftReport:
    """
    Whether the world has moved away from what the model was trained
    on (spec §44, §45).

    Distinguishes FEATURE drift (inputs changed) from PERFORMANCE
    degradation (outputs got worse) — they have different causes and
    different remedies, and conflating them leads to retraining a model
    that was never the problem.
    """
    model_qualified_id: str
    feature_drift: Dict[str, float] = field(default_factory=dict)
    performance_change: Optional[float] = None
    baseline_period: str = ""
    current_period: str = ""
    evaluated_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    #: Relative shift in a feature's mean beyond which drift is flagged.
    DRIFT_THRESHOLD = 0.5

    def __post_init__(self):
        self.evaluated_at = _require_utc(self.evaluated_at, "evaluated_at")

    @property
    def drifted_features(self) -> List[str]:
        return [name for name, shift in self.feature_drift.items() if abs(shift) > self.DRIFT_THRESHOLD]

    @property
    def has_drift(self) -> bool:
        return bool(self.drifted_features)
