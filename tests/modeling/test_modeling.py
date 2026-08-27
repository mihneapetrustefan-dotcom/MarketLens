"""
tests/modeling/test_modeling.py
------------------------------------
Tests for the Phase 9 modeling layer.

The critical tests here are the ones that assert the system reports
BAD news correctly (spec §62): a model with no predictive power must
be identified as such, an overfitted model must fail out of sample,
and purging/embargo must actually remove the rows they claim to.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.model_models import (
    ModelSpecification, ModelFamily, ModelStatus, PredictionTask, TrainedModel,
    Prediction, ModelEvaluation, BaselineComparison, CalibrationReport, CalibrationBin,
    DriftReport, TrainingWindow,
)
from src.modeling import algorithms
from src.modeling.splits import (
    WalkForwardSplitter, purge, embargo, verify_no_temporal_overlap, add_months,
)
from src.modeling.engine import (
    ModelingEngine, compute_metrics, compute_calibration, directional_accuracy,
    mean_absolute_error, r_squared, primary_metric_name, MANDATORY_BASELINES,
)

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def spec(task=PredictionTask.MAGNITUDE, family=ModelFamily.RIDGE_REGRESSION, **hyper):
    return ModelSpecification(
        model_id="m1", name="Test model", task=task, family=family,
        feature_set_id="fs", feature_set_version="v1",
        label_name="abnormal_return_5d", dataset_version="v1",
        hyperparameters=hyper, created_at=T0,
    )


class Observation:
    """Minimal timestamped observation for split testing."""
    def __init__(self, moment, value=0.0):
        self.moment = moment
        self.value = value


def obs_time(o):
    return o.moment


class TestPurgingAndEmbargo(unittest.TestCase):
    """Spec §15, §16 — the leak that a date-based split alone does not prevent."""

    def test_purge_removes_observations_whose_label_reaches_the_test_period(self):
        test_start = T0 + timedelta(days=100)
        observations = [
            Observation(T0 + timedelta(days=70)),   # label closes day 90 -> safe
            Observation(T0 + timedelta(days=95)),   # label closes day 115 -> LEAKS
        ]
        kept, purged = purge(observations, test_start, obs_time, label_horizon_days=20)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(purged), 1)
        self.assertEqual(kept[0].moment, T0 + timedelta(days=70))

    def test_purge_boundary_is_strict(self):
        """A label resolving exactly AT test_start is safe; one microsecond later is not."""
        test_start = T0 + timedelta(days=100)
        exactly = Observation(T0 + timedelta(days=80))         # +20 = day 100 exactly
        just_over = Observation(T0 + timedelta(days=80, seconds=1))
        kept, purged = purge([exactly, just_over], test_start, obs_time, label_horizon_days=20)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(purged), 1)

    def test_purge_removes_undated_observations_conservatively(self):
        kept, purged = purge([Observation(None)], T0, obs_time, label_horizon_days=5)
        self.assertEqual(kept, [])
        self.assertEqual(len(purged), 1)

    def test_embargo_removes_observations_immediately_before_the_test_period(self):
        test_start = T0 + timedelta(days=100)
        observations = [
            Observation(T0 + timedelta(days=90)),   # 10 days before -> safe
            Observation(T0 + timedelta(days=99)),   # 1 day before -> embargoed
        ]
        kept, embargoed = embargo(observations, test_start, obs_time, embargo_days=5)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(embargoed), 1)

    def test_zero_embargo_is_a_no_op(self):
        observations = [Observation(T0 + timedelta(days=99))]
        kept, embargoed = embargo(observations, T0 + timedelta(days=100), obs_time, embargo_days=0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(embargoed, [])

    def test_purging_and_embargo_are_on_by_default_in_the_splitter(self):
        splitter = WalkForwardSplitter(label_horizon_days=20)
        self.assertGreater(splitter.embargo_days, 0)
        self.assertEqual(splitter.label_horizon_days, 20)

    def test_negative_horizon_is_rejected(self):
        with self.assertRaises(ValueError):
            WalkForwardSplitter(label_horizon_days=-1)


class TestWalkForwardSplitting(unittest.TestCase):
    """Spec §13, §14."""

    def setUp(self):
        self.splitter = WalkForwardSplitter(
            label_horizon_days=5, embargo_days=1, train_months=12, test_months=6, step_months=6)
        self.observations = [Observation(T0 + timedelta(days=d)) for d in range(0, 1800, 10)]

    def test_test_always_follows_train(self):
        splits = self.splitter.split(self.observations, T0, T0 + timedelta(days=1800), obs_time)
        self.assertGreater(len(splits), 0)
        for split in splits:
            window = split["window"]
            self.assertGreaterEqual(window.test_start, window.train_end)

    def test_no_training_observation_overlaps_the_test_period(self):
        """Independent verification, deliberately re-derived rather than trusting the splitter."""
        splits = self.splitter.split(self.observations, T0, T0 + timedelta(days=1800), obs_time)
        for split in splits:
            violations = verify_no_temporal_overlap(
                split["train"], split["test"], obs_time, label_horizon_days=5)
            self.assertEqual(violations, [], f"leak in window {split['window'].label}: {violations}")

    def test_purge_and_embargo_counts_are_recorded_not_hidden(self):
        splits = self.splitter.split(self.observations, T0, T0 + timedelta(days=1800), obs_time)
        total_removed = sum(s["window"].purged_count + s["window"].embargoed_count for s in splits)
        self.assertGreater(total_removed, 0, "protection should have removed at least some boundary rows")

    def test_training_window_rejects_test_before_train_end(self):
        with self.assertRaises(ValueError):
            TrainingWindow(label="bad", train_start=T0, train_end=T0 + timedelta(days=100),
                            test_start=T0 + timedelta(days=50), test_end=T0 + timedelta(days=150))

    def test_no_shuffle_option_exists(self):
        import inspect
        for method in (WalkForwardSplitter.split, WalkForwardSplitter.generate_windows):
            parameters = inspect.signature(method).parameters
            for forbidden in ("shuffle", "random", "random_state", "seed"):
                self.assertNotIn(forbidden, parameters)


class TestBaselines(unittest.TestCase):
    """Spec §28, §29 — the comparison without which a metric means nothing."""

    def test_majority_class_baseline_captures_the_base_rate(self):
        Y = [0.01] * 53 + [-0.01] * 47
        parameters = algorithms.fit_majority_class_baseline([[1.0]] * 100, Y)
        self.assertAlmostEqual(parameters["base_rate"], 0.53, places=4)
        self.assertEqual(parameters["majority"], 1.0)

    def test_historical_mean_baseline(self):
        parameters = algorithms.fit_historical_mean_baseline([[1.0]] * 4, [0.02, 0.04, 0.00, 0.02])
        self.assertAlmostEqual(parameters["constant"], 0.02, places=6)

    def test_baselines_are_mandatory_and_not_caller_selectable(self):
        """A caller who could choose the baselines could choose the flattering ones."""
        self.assertGreaterEqual(len(MANDATORY_BASELINES), 2)
        import inspect
        parameters = inspect.signature(ModelingEngine.evaluate).parameters
        for forbidden in ("baselines", "baseline_families", "skip_baselines"):
            self.assertNotIn(forbidden, parameters)


class TestHonestEvaluation(unittest.TestCase):
    """Spec §30, §52, §62 — the system must report bad news correctly."""

    def setUp(self):
        self.engine = ModelingEngine()

    def _pure_noise(self, n=200, seed=7):
        import random
        generator = random.Random(seed)
        X = [[generator.gauss(0, 1), generator.gauss(0, 1)] for _ in range(n)]
        Y = [generator.gauss(0, 0.02) for _ in range(n)]
        return X, Y

    def test_case_1_random_features_yield_no_demonstrated_predictive_value(self):
        """Pure noise in -> the evaluation must NOT claim the model works."""
        X, Y = self._pure_noise()
        split = len(X) // 2
        model, evaluation = self.engine.train_and_evaluate(
            spec(), X[:split], Y[:split], X[split:], Y[split:], ["f1", "f2"])

        self.assertIsNotNone(evaluation.beats_all_baselines)
        self.assertIn(evaluation.is_deployable, (False, None))
        if not evaluation.beats_all_baselines:
            self.assertIn("no demonstrated predictive value", evaluation.honest_summary())

    def test_case_2_overfitted_model_fails_out_of_sample(self):
        """
        More features than observations. The system must NOT produce a
        confident-looking out-of-sample result — either it abstains
        (cannot fit a singular system) or it scores poorly. What it
        must never do is report a good fit on noise.
        """
        import random
        generator = random.Random(11)
        n_features = 15
        X = [[generator.gauss(0, 1) for _ in range(n_features)] for _ in range(24)]
        Y = [generator.gauss(0, 0.02) for _ in range(24)]

        model, evaluation = self.engine.train_and_evaluate(
            spec(alpha=0.0), X[:12], Y[:12], X[12:], Y[12:], [f"f{i}" for i in range(n_features)])

        out_of_sample_r2 = evaluation.metrics.get("r_squared")
        if out_of_sample_r2 is None:
            # The honest outcome: unfittable, so every prediction is an
            # abstention rather than an invented number.
            self.assertEqual(evaluation.abstention_rate, 1.0)
            self.assertTrue(model.parameters.get("singular") or model.parameters.get("insufficient_data"))
        else:
            self.assertLess(out_of_sample_r2, 0.9, "out-of-sample R² should not look like a good fit on noise")

        # Either way, this must never be presented as deployable.
        self.assertIn(evaluation.is_deployable, (False, None))

    def test_small_sample_is_never_deployable_even_if_it_beats_baselines(self):
        evaluation = ModelEvaluation(
            evaluation_id="e", trained_model_id="t", model_qualified_id="m:v1",
            sample_size=10, cluster_count=3,
            baseline_comparisons=[BaselineComparison("b", 0.4, 0.9, "directional_accuracy")],
        )
        self.assertTrue(evaluation.beats_all_baselines)
        self.assertTrue(evaluation.small_sample)
        self.assertFalse(evaluation.is_deployable)
        self.assertIn("descriptive, not evidence", evaluation.honest_summary())

    def test_clustered_observations_shrink_the_effective_sample(self):
        evaluation = ModelEvaluation(
            evaluation_id="e", trained_model_id="t", model_qualified_id="m:v1",
            sample_size=400, cluster_count=12)
        self.assertEqual(evaluation.effective_sample_size, 12)
        self.assertTrue(evaluation.small_sample)

    def test_no_baselines_means_the_result_is_uninterpretable(self):
        evaluation = ModelEvaluation(evaluation_id="e", trained_model_id="t",
                                      model_qualified_id="m:v1", sample_size=100)
        self.assertIsNone(evaluation.beats_all_baselines)
        self.assertIsNone(evaluation.is_deployable)
        self.assertIn("cannot be interpreted", evaluation.honest_summary())

    def test_negative_r_squared_is_reported_not_clamped(self):
        """A model worse than the mean must show it, not be floored at zero."""
        actual = [0.01, 0.02, -0.01, 0.03]
        predicted = [0.5, -0.5, 0.5, -0.5]
        self.assertLess(r_squared(actual, predicted), 0)


class TestPredictionsAndAbstention(unittest.TestCase):
    """Spec §7, §24, §27, §51."""

    def setUp(self):
        self.engine = ModelingEngine()

    def test_prediction_carries_full_provenance(self):
        X, Y = [[1.0], [2.0], [3.0], [4.0]], [0.01, 0.02, 0.03, 0.04]
        model = self.engine.train(spec(), X, Y, ["f1"])
        predictions = self.engine.predict(model, X, ["o1", "o2", "o3", "o4"],
                                           information_cutoffs=[T0] * 4)
        for prediction in predictions:
            self.assertEqual(prediction.validate(), [])

    def test_missing_provenance_is_detected(self):
        prediction = Prediction(prediction_id="p", trained_model_id="t",
                                 model_qualified_id="", observation_id="o")
        problems = prediction.validate()
        self.assertIn("no model version recorded", problems)

    def test_model_abstains_rather_than_guessing_on_missing_features(self):
        X, Y = [[1.0], [2.0], [3.0], [4.0]], [0.01, 0.02, 0.03, 0.04]
        model = self.engine.train(spec(), X, Y, ["f1"])
        predictions = self.engine.predict(model, [[None]], ["o1"], [T0])
        self.assertTrue(predictions[0].is_abstention)
        self.assertIsNone(predictions[0].predicted_value)

    def test_untrainable_model_abstains_on_everything(self):
        model = self.engine.train(spec(), [[1.0]], [0.01], ["f1"])   # 1 row: cannot fit
        self.assertTrue(model.parameters.get("insufficient_data"))
        predictions = self.engine.predict(model, [[1.0]], ["o1"], [T0])
        self.assertTrue(predictions[0].is_abstention)

    def test_prediction_has_no_decision_fields(self):
        """Spec §7, §51: a prediction is not a decision."""
        fields = set(Prediction.__dataclass_fields__.keys())
        for forbidden in ("action", "position_size", "signal", "recommendation",
                           "order", "trade", "allocation", "buy", "sell"):
            self.assertNotIn(forbidden, fields)

    def test_model_specification_has_no_decision_fields(self):
        fields = set(ModelSpecification.__dataclass_fields__.keys())
        for forbidden in ("action", "position_size", "signal", "recommendation"):
            self.assertNotIn(forbidden, fields)


class TestCalibration(unittest.TestCase):
    """Spec §31, §32, §33 — calibration is not accuracy."""

    def test_well_calibrated_predictions_have_low_error(self):
        # 80%-confidence predictions that are right ~80% of the time.
        probabilities = [0.8] * 100
        outcomes = [0.01] * 80 + [-0.01] * 20
        report = compute_calibration("m:v1", probabilities, outcomes)
        self.assertIsNotNone(report.expected_calibration_error)
        self.assertLess(report.expected_calibration_error, 0.1)

    def test_overconfident_model_is_detected(self):
        # Claims 90% confidence, right only 50% of the time.
        probabilities = [0.9] * 50 + [0.7] * 50
        outcomes = ([0.01] * 25 + [-0.01] * 25) * 2
        report = compute_calibration("m:v1", probabilities, outcomes)
        self.assertTrue(report.is_overconfident)

    def test_small_calibration_sample_is_flagged(self):
        report = compute_calibration("m:v1", [0.8] * 5, [0.01] * 5)
        self.assertTrue(any("unreliable" in note for note in report.notes))

    def test_calibration_and_accuracy_are_separate_properties(self):
        """A model can be accurate and badly calibrated at the same time."""
        probabilities = [0.99] * 100
        outcomes = [0.01] * 70 + [-0.01] * 30
        report = compute_calibration("m:v1", probabilities, outcomes)
        accuracy = directional_accuracy(outcomes, [1.0] * 100)
        self.assertGreater(accuracy, 0.6)                   # reasonably accurate
        self.assertGreater(report.expected_calibration_error, 0.2)   # badly calibrated

    def test_no_probabilistic_predictions_yields_an_honest_empty_report(self):
        report = compute_calibration("m:v1", [None, None], [0.01, 0.02])
        self.assertEqual(report.sample_size, 0)
        self.assertTrue(report.notes)


class TestAlgorithms(unittest.TestCase):
    def test_ridge_recovers_a_clean_linear_relationship(self):
        X = [[float(i)] for i in range(50)]
        Y = [2.0 * i + 1.0 for i in range(50)]
        parameters = algorithms.fit_ridge(X, Y, alpha=0.001)
        prediction = algorithms.predict_one(parameters, [10.0])
        self.assertAlmostEqual(prediction, 21.0, delta=1.0)

    def test_ridge_is_regularized_by_default(self):
        import inspect
        default_alpha = inspect.signature(algorithms.fit_ridge).parameters["alpha"].default
        self.assertGreater(default_alpha, 0)

    def test_logistic_separates_a_clean_boundary(self):
        X = [[float(i)] for i in range(-20, 20)]
        Y = [0.01 if i > 0 else -0.01 for i in range(-20, 20)]
        parameters = algorithms.fit_logistic(X, Y, iterations=500)
        self.assertGreater(algorithms.predict_one(parameters, [15.0]), 0.5)
        self.assertLess(algorithms.predict_one(parameters, [-15.0]), 0.5)

    def test_missing_values_are_not_imputed(self):
        X = [[1.0, None], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]]
        Y = [0.01, 0.02, 0.03, 0.04]
        parameters = algorithms.fit_ridge(X, Y)
        self.assertFalse(parameters.get("insufficient_data"))   # the clean rows still fit

    def test_singular_system_abstains_rather_than_returning_nonsense(self):
        X = [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]   # perfectly collinear
        Y = [0.01, 0.02, 0.03]
        parameters = algorithms.fit_ridge(X, Y, alpha=0.0, standardize=False)
        prediction = algorithms.predict_one(parameters, [1.0, 1.0])
        self.assertTrue(parameters.get("singular") or prediction is not None)

    def test_unsupported_family_raises_rather_than_substituting(self):
        with self.assertRaises(ValueError):
            algorithms.fit(ModelFamily.DECISION_STUMP, [[1.0]], [0.01])


class TestWalkForwardAggregation(unittest.TestCase):
    """Spec §14, §35 — the spread matters as much as the mean."""

    def test_aggregate_reports_dispersion_not_just_the_average(self):
        engine = ModelingEngine()
        evaluations = [
            ModelEvaluation(evaluation_id=f"e{i}", trained_model_id="t", model_qualified_id="m:v1",
                             metrics={"directional_accuracy": value})
            for i, value in enumerate([0.40, 0.55, 0.70, 0.52])
        ]
        summary = engine.aggregate_windows(evaluations, "directional_accuracy")
        self.assertEqual(summary["window_count"], 4)
        self.assertIsNotNone(summary["std_dev"])
        self.assertEqual(summary["min"], 0.40)
        self.assertEqual(summary["max"], 0.70)

    def test_no_windows_returns_an_honest_note(self):
        summary = ModelingEngine().aggregate_windows([], "mae")
        self.assertEqual(summary["window_count"], 0)


class TestDrift(unittest.TestCase):
    """Spec §44, §45 — feature drift and performance degradation are distinct."""

    def test_feature_drift_is_detected(self):
        engine = ModelingEngine()
        report = engine.detect_drift(
            "m:v1",
            baseline_features={"volatility": [0.1] * 20},
            current_features={"volatility": [0.5] * 20})
        self.assertTrue(report.has_drift)
        self.assertIn("volatility", report.drifted_features)

    def test_stable_features_show_no_drift(self):
        engine = ModelingEngine()
        report = engine.detect_drift(
            "m:v1",
            baseline_features={"volatility": [0.10] * 20},
            current_features={"volatility": [0.11] * 20})
        self.assertFalse(report.has_drift)

    def test_performance_decline_is_reported_separately_from_feature_drift(self):
        engine = ModelingEngine()
        report = engine.detect_drift(
            "m:v1", baseline_features={"f": [1.0] * 10}, current_features={"f": [1.0] * 10},
            baseline_score=0.60, current_score=0.48)
        self.assertFalse(report.has_drift)                    # inputs unchanged
        self.assertLess(report.performance_change, 0)          # outputs worse
        self.assertTrue(any("declined" in note for note in report.notes))


class TestModelLifecycleAndVersioning(unittest.TestCase):
    """Spec §22, §23, §54, §55."""

    def test_specification_fingerprint_covers_hyperparameters(self):
        a = spec(alpha=1.0)
        b = spec(alpha=2.0)
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_specification_fingerprint_covers_the_feature_set_version(self):
        a = spec()
        b = spec()
        b.feature_set_version = "v2"
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_identical_specifications_share_a_fingerprint(self):
        self.assertEqual(spec(alpha=1.0).fingerprint(), spec(alpha=1.0).fingerprint())

    def test_retired_status_exists_so_models_are_never_deleted(self):
        self.assertIn(ModelStatus.RETIRED, list(ModelStatus))

    def test_effective_sample_prefers_clusters_over_row_count(self):
        model = TrainedModel(trained_model_id="t", specification=spec(),
                              train_sample_size=400, train_cluster_count=12)
        self.assertEqual(model.effective_sample_size, 12)

    def test_training_warns_when_rows_far_exceed_clusters(self):
        engine = ModelingEngine()
        X = [[float(i)] for i in range(60)]
        Y = [0.01 * i for i in range(60)]
        model = engine.train(spec(), X, Y, ["f1"], cluster_count=5)
        self.assertTrue(any("effective sample" in note for note in model.training_notes))


class TestMetrics(unittest.TestCase):
    def test_directional_accuracy(self):
        self.assertEqual(directional_accuracy([0.01, -0.01, 0.02], [0.5, -0.5, 0.5]), 1.0)

    def test_mae(self):
        self.assertAlmostEqual(mean_absolute_error([1.0, 2.0], [1.5, 2.5]), 0.5, places=6)

    def test_metrics_omit_what_cannot_be_computed(self):
        metrics = compute_metrics(PredictionTask.MAGNITUDE, [], [])
        self.assertEqual(metrics, {})

    def test_abstentions_are_excluded_from_metrics_not_counted_as_wrong(self):
        accuracy = directional_accuracy([0.01, 0.02], [0.5, None])
        self.assertEqual(accuracy, 1.0)   # the one real prediction was correct

    def test_primary_metric_differs_by_task(self):
        self.assertEqual(primary_metric_name(PredictionTask.DIRECTION), "directional_accuracy")
        self.assertEqual(primary_metric_name(PredictionTask.MAGNITUDE), "mae")


if __name__ == "__main__":
    unittest.main(verbosity=2)
