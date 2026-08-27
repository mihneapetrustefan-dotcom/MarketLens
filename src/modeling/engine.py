"""
src/modeling/engine.py
---------------------------
Evaluation, calibration, and the training pipeline
(Phase 9, spec §17-§20, §25-§35, §41-§45, §52-§57).

THE ENFORCEMENT THIS MODULE APPLIES (spec §28, §29): a model is never
evaluated alone. `train_and_evaluate` fits the requested model AND its
baselines on the same split, and returns them together. There is no
code path that produces a metric without its comparison, because a
54%-accuracy result next to a 53% majority-class baseline tells a very
different story from the same number presented by itself.

CALIBRATION IS COMPUTED SEPARATELY FROM ACCURACY (spec §31, §32): they
are different properties. A model can be right often and confidently
wrong about how right it is.

NOTHING HERE PRODUCES A DECISION (spec §7, §51). The output is
predictions and evaluations. No position, no action, no signal —
translating a prediction into a trade needs risk limits, costs and
portfolio context this phase deliberately does not have.
"""

import math
import uuid
import logging
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.model_models import (
    ModelSpecification, ModelFamily, ModelStatus, PredictionTask, TrainedModel,
    Prediction, ModelEvaluation, BaselineComparison, CalibrationBin, CalibrationReport,
    DriftReport, TrainingWindow,
)
from src.modeling import algorithms

logger = logging.getLogger("marketlens.modeling.engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


#: Baselines every model is measured against (spec §28). Fixed, not
#: configurable — letting a caller choose which baselines to compare
#: against would let them choose the flattering ones.
MANDATORY_BASELINES = [
    ModelFamily.BASELINE_HISTORICAL_MEAN,
    ModelFamily.BASELINE_MAJORITY_CLASS,
]


# ============================================================
# Metrics (spec §25)
# ============================================================

def mean_absolute_error(actual: Sequence[float], predicted: Sequence[Optional[float]]) -> Optional[float]:
    pairs = [(a, p) for a, p in zip(actual, predicted) if p is not None]
    if not pairs:
        return None
    return round(statistics.fmean(abs(a - p) for a, p in pairs), 6)


def root_mean_squared_error(actual: Sequence[float], predicted: Sequence[Optional[float]]) -> Optional[float]:
    pairs = [(a, p) for a, p in zip(actual, predicted) if p is not None]
    if not pairs:
        return None
    return round(math.sqrt(statistics.fmean((a - p) ** 2 for a, p in pairs)), 6)


def directional_accuracy(actual: Sequence[float], predicted: Sequence[Optional[float]],
                          threshold: float = 0.0) -> Optional[float]:
    """
    Share of predictions whose DIRECTION matched.

    Reported separately from magnitude error because they can diverge
    sharply: a model can get direction right most of the time while
    being badly wrong about size, and one number hides that.
    """
    pairs = [(a, p) for a, p in zip(actual, predicted) if p is not None]
    if not pairs:
        return None
    correct = sum(1 for a, p in pairs if (a > threshold) == (p > threshold))
    return round(correct / len(pairs), 6)


def hit_rate(actual: Sequence[float], predicted: Sequence[Optional[float]],
             threshold: float = 0.0) -> Optional[float]:
    """Among predictions of 'up', how often the outcome was actually up (precision on the positive class)."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if p is not None and p > threshold]
    if not pairs:
        return None
    return round(sum(1 for a, _ in pairs if a > threshold) / len(pairs), 6)


def r_squared(actual: Sequence[float], predicted: Sequence[Optional[float]]) -> Optional[float]:
    """
    Out-of-sample R². CAN BE NEGATIVE, and that is not a bug — a
    negative value means the model does worse than predicting the mean,
    which is exactly the kind of honest bad news this phase exists to
    surface.
    """
    pairs = [(a, p) for a, p in zip(actual, predicted) if p is not None]
    if len(pairs) < 2:
        return None
    actuals = [a for a, _ in pairs]
    mean_actual = statistics.fmean(actuals)
    ss_total = sum((a - mean_actual) ** 2 for a in actuals)
    ss_residual = sum((a - p) ** 2 for a, p in pairs)
    if ss_total == 0:
        return None
    return round(1 - ss_residual / ss_total, 6)


def compute_metrics(task: PredictionTask, actual: Sequence[float],
                     predicted: Sequence[Optional[float]]) -> Dict[str, float]:
    """Metrics appropriate to the task. Values that cannot be computed are OMITTED, never defaulted to zero."""
    metrics: Dict[str, float] = {}
    if task == PredictionTask.DIRECTION:
        for name, value in (("directional_accuracy", directional_accuracy(actual, predicted)),
                             ("hit_rate", hit_rate(actual, predicted))):
            if value is not None:
                metrics[name] = value
    else:
        for name, value in (("mae", mean_absolute_error(actual, predicted)),
                             ("rmse", root_mean_squared_error(actual, predicted)),
                             ("r_squared", r_squared(actual, predicted)),
                             ("directional_accuracy", directional_accuracy(actual, predicted))):
            if value is not None:
                metrics[name] = value
    return metrics


def primary_metric_name(task: PredictionTask) -> str:
    """The metric baselines are compared on — one per task, so comparisons stay like-for-like."""
    return "directional_accuracy" if task == PredictionTask.DIRECTION else "mae"


def higher_is_better(metric_name: str) -> bool:
    return metric_name not in ("mae", "rmse")


# ============================================================
# Calibration (spec §31, §32, §33)
# ============================================================

def compute_calibration(model_qualified_id: str, probabilities: Sequence[Optional[float]],
                         outcomes: Sequence[float], threshold: float = 0.0,
                         bin_count: int = 5) -> CalibrationReport:
    """
    Compare stated confidence against observed frequency.

    Bins predictions by confidence and reports what actually happened
    in each. Expected Calibration Error is the count-weighted mean
    absolute gap — one number for "how far the model's confidence is
    from reality", separate from whether it was right.
    """
    pairs = [(p, 1.0 if o > threshold else 0.0)
             for p, o in zip(probabilities, outcomes) if p is not None]
    report = CalibrationReport(model_qualified_id=model_qualified_id, sample_size=len(pairs))
    if not pairs:
        report.notes.append("no probabilistic predictions available to calibrate")
        return report

    width = 1.0 / bin_count
    total_gap, total_count = 0.0, 0
    for index in range(bin_count):
        lower, upper = index * width, (index + 1) * width
        in_bin = [(p, o) for p, o in pairs if (lower <= p < upper or (index == bin_count - 1 and p == 1.0))]
        calibration_bin = CalibrationBin(lower=round(lower, 4), upper=round(upper, 4), count=len(in_bin))
        if in_bin:
            calibration_bin.mean_predicted = round(statistics.fmean(p for p, _ in in_bin), 6)
            calibration_bin.observed_frequency = round(statistics.fmean(o for _, o in in_bin), 6)
            gap = calibration_bin.gap
            if gap is not None:
                total_gap += abs(gap) * len(in_bin)
                total_count += len(in_bin)
        report.bins.append(calibration_bin)

    if total_count:
        report.expected_calibration_error = round(total_gap / total_count, 6)
    if len(pairs) < 30:
        report.notes.append(f"only {len(pairs)} predictions — calibration estimate is unreliable at this sample size")
    return report


# ============================================================
# Training engine
# ============================================================

class ModelingEngine:
    """Trains models, always alongside their baselines, and evaluates them out of sample."""

    def __init__(self):
        self.trained_models: Dict[str, TrainedModel] = {}
        self.evaluations: List[ModelEvaluation] = []
        self.predictions: List[Prediction] = []

    def train(
        self,
        specification: ModelSpecification,
        X: Sequence[Sequence[Optional[float]]],
        Y: Sequence[Optional[float]],
        feature_names: Sequence[str],
        train_start: Optional[datetime] = None,
        train_end: Optional[datetime] = None,
        cluster_count: Optional[int] = None,
    ) -> TrainedModel:
        """
        Fit one model.

        Records the effective sample size (clusters when known) on the
        trained model, so an evaluation built from it inherits an
        honest denominator rather than the inflated row count.
        """
        parameters = algorithms.fit(specification.family, X, Y, **specification.hyperparameters)
        model = TrainedModel(
            trained_model_id=f"tm-{uuid.uuid4().hex[:16]}",
            specification=specification,
            parameters=parameters,
            feature_names=list(feature_names),
            train_start=train_start, train_end=train_end,
            train_sample_size=len(X), train_cluster_count=cluster_count,
            status=ModelStatus.TRAINED,
            trained_at=datetime.now(timezone.utc),
        )
        if parameters.get("insufficient_data"):
            model.training_notes.append("insufficient data to fit — model will abstain on every prediction")
        if parameters.get("singular"):
            model.training_notes.append("singular system — features are collinear; model will abstain")
        if cluster_count and len(X) >= cluster_count * 3:
            model.training_notes.append(
                f"{len(X)} rows from only {cluster_count} clusters — effective sample is far smaller than it appears")

        self.trained_models[model.trained_model_id] = model
        return model

    def predict(
        self,
        model: TrainedModel,
        X: Sequence[Sequence[Optional[float]]],
        observation_ids: Sequence[str],
        information_cutoffs: Optional[Sequence[Optional[datetime]]] = None,
    ) -> List[Prediction]:
        """
        Produce predictions with full provenance (spec §24).

        A None from the algorithm becomes an explicit ABSTENTION, not a
        zero — the distinction matters when the results are aggregated.
        """
        raw = algorithms.predict_batch(model.parameters, X)
        is_probabilistic = model.specification.family == ModelFamily.LOGISTIC_REGRESSION
        results = []

        for index, value in enumerate(raw):
            cutoff = information_cutoffs[index] if information_cutoffs else None
            prediction = Prediction(
                prediction_id=f"pr-{uuid.uuid4().hex[:16]}",
                trained_model_id=model.trained_model_id,
                model_qualified_id=model.specification.qualified_id,
                observation_id=observation_ids[index],
                feature_set_version=model.specification.feature_set_version,
                information_cutoff=cutoff,
                predicted_at=datetime.now(timezone.utc),
            )
            if value is None:
                prediction.is_abstention = True
                prediction.abstention_reason = "model could not produce a value for this input"
            else:
                prediction.predicted_value = round(value, 8)
                if is_probabilistic:
                    prediction.class_probabilities = {"up": round(value, 6), "down": round(1 - value, 6)}
                    prediction.predicted_class = "up" if value >= 0.5 else "down"
                    prediction.confidence = round(max(value, 1 - value), 6)
                    prediction.uncertainty_basis = "logistic probability output"
                else:
                    prediction.uncertainty_basis = "point estimate; no interval computed by this family"
            results.append(prediction)

        self.predictions.extend(results)
        return results

    def evaluate(
        self,
        model: TrainedModel,
        X_test: Sequence[Sequence[Optional[float]]],
        Y_test: Sequence[Optional[float]],
        X_train: Sequence[Sequence[Optional[float]]],
        Y_train: Sequence[Optional[float]],
        window_label: str = "",
        cluster_count: Optional[int] = None,
    ) -> ModelEvaluation:
        """
        Evaluate out of sample, ALWAYS against baselines.

        The baselines are fitted on the same training data and scored
        on the same test data, so the comparison is like-for-like. This
        is not optional and cannot be skipped by a caller (spec §29).
        """
        clean_pairs = [(row, y) for row, y in zip(X_test, Y_test) if y is not None]
        X_clean = [row for row, _ in clean_pairs]
        Y_clean = [float(y) for _, y in clean_pairs]

        evaluation = ModelEvaluation(
            evaluation_id=f"ev-{uuid.uuid4().hex[:16]}",
            trained_model_id=model.trained_model_id,
            model_qualified_id=model.specification.qualified_id,
            window_label=window_label,
            sample_size=len(Y_clean),
            cluster_count=cluster_count,
            evaluated_at=datetime.now(timezone.utc),
        )
        if not Y_clean:
            evaluation.notes.append("no usable test observations")
            return evaluation

        predicted = algorithms.predict_batch(model.parameters, X_clean)
        abstentions = sum(1 for p in predicted if p is None)
        evaluation.abstention_rate = round(abstentions / len(predicted), 4)
        evaluation.metrics = compute_metrics(model.specification.task, Y_clean, predicted)

        metric_name = primary_metric_name(model.specification.task)
        model_score = evaluation.metrics.get(metric_name)

        for baseline_family in MANDATORY_BASELINES:
            baseline_parameters = algorithms.fit(baseline_family, X_train, Y_train)
            baseline_predicted = algorithms.predict_batch(baseline_parameters, X_clean)
            baseline_metrics = compute_metrics(model.specification.task, Y_clean, baseline_predicted)
            baseline_score = baseline_metrics.get(metric_name)

            comparison = BaselineComparison(
                baseline_name=baseline_family.value, metric_name=metric_name,
                baseline_score=baseline_score, model_score=model_score,
            )
            # For error metrics, LOWER is better — flip the sign so
            # `improvement > 0` always means "the model is better",
            # regardless of metric direction.
            if baseline_score is not None and model_score is not None and not higher_is_better(metric_name):
                comparison.model_score = -model_score
                comparison.baseline_score = -baseline_score
            evaluation.baseline_comparisons.append(comparison)

        if model.specification.family == ModelFamily.LOGISTIC_REGRESSION:
            evaluation.calibration = compute_calibration(
                model.specification.qualified_id, predicted, Y_clean)

        if evaluation.small_sample:
            evaluation.notes.append(
                f"effective sample {evaluation.effective_sample_size} is below "
                f"{ModelEvaluation.MIN_EFFECTIVE_SAMPLE} — descriptive only")

        self.evaluations.append(evaluation)
        logger.info("Evaluated %s on %s: %s", model.specification.qualified_id, window_label, evaluation.metrics)
        return evaluation

    def train_and_evaluate(
        self,
        specification: ModelSpecification,
        X_train, Y_train, X_test, Y_test,
        feature_names: Sequence[str],
        window: Optional[TrainingWindow] = None,
        cluster_count: Optional[int] = None,
    ) -> Tuple[TrainedModel, ModelEvaluation]:
        """Convenience path that makes the correct workflow the easy one: fit, then evaluate against baselines."""
        model = self.train(
            specification, X_train, Y_train, feature_names,
            train_start=window.train_start if window else None,
            train_end=window.train_end if window else None,
            cluster_count=cluster_count,
        )
        evaluation = self.evaluate(
            model, X_test, Y_test, X_train, Y_train,
            window_label=window.label if window else "", cluster_count=cluster_count,
        )
        model.status = ModelStatus.EVALUATED
        return model, evaluation

    def aggregate_windows(self, evaluations: Sequence[ModelEvaluation],
                           metric_name: str) -> Dict[str, Any]:
        """
        Summarize performance across walk-forward windows (spec §14,
        §35).

        Reports the SPREAD, not just the mean: a model averaging 55%
        across windows ranging 40-70% is a different proposition from
        one steady at 54-56%, and only the dispersion reveals that.
        """
        values = [e.metrics.get(metric_name) for e in evaluations if e.metrics.get(metric_name) is not None]
        if not values:
            return {"metric": metric_name, "window_count": 0, "note": "no windows produced this metric"}
        return {
            "metric": metric_name,
            "window_count": len(values),
            "mean": round(statistics.fmean(values), 6),
            "median": round(statistics.median(values), 6),
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "std_dev": round(statistics.stdev(values), 6) if len(values) > 1 else None,
            "windows_beating_all_baselines": sum(1 for e in evaluations if e.beats_all_baselines),
            "note": ("performance varies across windows; the spread matters as much as the mean, "
                      "since a strategy is only as reliable as its worst regime"),
        }

    def detect_drift(self, model_qualified_id: str,
                      baseline_features: Dict[str, Sequence[float]],
                      current_features: Dict[str, Sequence[float]],
                      baseline_score: Optional[float] = None,
                      current_score: Optional[float] = None) -> DriftReport:
        """
        Compare feature distributions and performance between periods
        (spec §44, §45).

        Feature drift and performance degradation are reported
        separately because they have different remedies — retraining
        fixes the first, and may do nothing at all for the second.
        """
        report = DriftReport(
            model_qualified_id=model_qualified_id,
            evaluated_at=datetime.now(timezone.utc),
        )
        for name, baseline_values in baseline_features.items():
            current_values = current_features.get(name)
            if not baseline_values or not current_values:
                continue
            baseline_mean = statistics.fmean(baseline_values)
            current_mean = statistics.fmean(current_values)
            if baseline_mean == 0:
                shift = 0.0 if current_mean == 0 else 1.0
            else:
                shift = (current_mean - baseline_mean) / abs(baseline_mean)
            report.feature_drift[name] = round(shift, 6)

        if baseline_score is not None and current_score is not None:
            report.performance_change = round(current_score - baseline_score, 6)
            if report.performance_change < 0:
                report.notes.append("performance declined versus the baseline period")
        if report.has_drift:
            report.notes.append(f"features with material distribution shift: {', '.join(report.drifted_features)}")
        return report
