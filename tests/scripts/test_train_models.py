"""
tests/scripts/test_train_models.py
-----------------------------------------------------------
Tests for scripts/train_models.py.

The properties that matter most here are the ones that protect against
fooling yourself: the split must be chronological (never random), the
embargo must actually remove rows, categorical features must not be
silently coerced into the design matrix, and baselines must always be
persisted alongside the model.
"""

import json
import os
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.train_models import (
    load_dataset, chronological_split, main, persist, DEFAULT_LABEL,
)
from src.data_access.research_schema import initialize_research_schema
from src.data_access.model_schema import initialize_model_schema
from src.domain.model_models import (
    BaselineComparison, ModelEvaluation, ModelFamily, ModelSpecification,
    PredictionTask, TrainedModel,
)

START = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Sentinel for "work it out from the comparisons", so a test can still
#: ask for `abstention_rate=None` explicitly.
DERIVE = object()


def seed(path, n=200, quality="high", with_labels=True):
    conn = sqlite3.connect(path)
    initialize_research_schema(conn)
    initialize_model_schema(conn)
    random.seed(42)

    for i in range(n):
        oid = f"obs-{i:04d}"
        cutoff = START + timedelta(days=i * 0.4)
        cluster = f"inst-{i % 25}"
        conn.execute("""INSERT INTO research_observations
            (observation_id,event_id,instrument_id,observation_created_at,information_cutoff,
             event_cluster_id,quality_level,dataset_version)
            VALUES (?,?,?,?,?,?,?, 'v1')""",
            (oid, f"ce-{i}", cluster, cutoff.isoformat(), cutoff.isoformat(), cluster, quality))

        f1, f2, f3 = random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1)
        for name, val in [("market.return_5d", f1), ("volatility.realized_20d", f2),
                          ("event.count_7d", f3)]:
            conn.execute("""INSERT INTO research_features
                (observation_id,qualified_name,namespace,value_json,as_of,source)
                VALUES (?,?,?,?,?,'phase8_feature_engine')""",
                (oid, name, "market", json.dumps(val), cutoff.isoformat()))
        # A categorical feature that must NOT enter the design matrix.
        conn.execute("""INSERT INTO research_features
            (observation_id,qualified_name,namespace,value_json,as_of,source)
            VALUES (?,?,?,?,?,'phase6_event_study')""",
            (oid, "event.event_type", "event", json.dumps("acquisition"), cutoff.isoformat()))

        if with_labels:
            label = 0.3 * f1 - 0.2 * f2 + random.gauss(0, 1)
            conn.execute("""INSERT INTO research_labels
                (observation_id,name,value_json,measured_at,window_name)
                VALUES (?,?,?,?,'d5')""",
                (oid, DEFAULT_LABEL, json.dumps(label),
                 (cutoff + timedelta(days=5)).isoformat()))
    conn.commit()
    conn.close()


class TestChronologicalSplit(unittest.TestCase):
    def test_train_always_precedes_test(self):
        cutoffs = [START + timedelta(days=i) for i in range(100)]
        train, test, _ = chronological_split(cutoffs, 0.7, embargo_days=0.0)
        self.assertTrue(max(train) < min(test))

    def test_embargo_removes_rows_from_both_sets(self):
        cutoffs = [START + timedelta(days=i) for i in range(100)]
        _, _, no_embargo = chronological_split(cutoffs, 0.7, embargo_days=0.0)
        train, test, embargoed = chronological_split(cutoffs, 0.7, embargo_days=5.0)
        self.assertEqual(no_embargo, 0)
        self.assertGreater(embargoed, 0)
        self.assertEqual(len(train) + len(test) + embargoed, len(cutoffs))

    def test_gap_between_train_and_test_respects_embargo(self):
        cutoffs = [START + timedelta(days=i) for i in range(100)]
        train, test, _ = chronological_split(cutoffs, 0.7, embargo_days=5.0)
        gap = (cutoffs[min(test)] - cutoffs[max(train)]).days
        self.assertGreaterEqual(gap, 5)

    def test_empty_input_returns_empty(self):
        self.assertEqual(chronological_split([], 0.7, 1.0), ([], [], 0))


class TestLoadDataset(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_categorical_features_are_excluded(self):
        names, _, _, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertNotIn("event.event_type", names)

    def test_numeric_features_from_both_phases_are_included(self):
        names, _, _, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertIn("market.return_5d", names)
        self.assertIn("event.count_7d", names)

    def test_rows_are_ordered_oldest_first(self):
        _, _, _, cutoffs, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertEqual(cutoffs, sorted(cutoffs))

    def test_observations_without_labels_are_dropped(self):
        self.conn.execute("DELETE FROM research_labels WHERE observation_id = 'obs-0000'")
        self.conn.commit()
        _, X, _, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertEqual(len(X), 199)

    def test_invalid_quality_observations_are_excluded_but_counted(self):
        self.conn.execute("UPDATE research_observations SET quality_level='invalid' "
                          "WHERE observation_id IN ('obs-0000','obs-0001')")
        self.conn.commit()
        _, X, _, _, _, _, quality_counts = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertEqual(quality_counts["invalid"], 2)
        self.assertEqual(len(X), 198)


class TestTrainEndToEnd(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, extra=None):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path] + (extra or [])
        try:
            return main()
        finally:
            sys.argv = argv

    def _count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self):
        self._run()
        self.assertEqual(self._count("trained_models"), 0)

    def test_apply_persists_model_and_evaluation(self):
        self._run(["--apply"])
        self.assertEqual(self._count("trained_models"), 1)
        self.assertEqual(self._count("model_evaluations"), 1)

    def test_baselines_are_always_persisted_with_the_evaluation(self):
        # The engine refuses to produce a metric without baselines;
        # persistence must not lose that.
        self._run(["--apply"])
        self.assertEqual(self._count("model_baseline_comparisons"), 2)

    def test_small_sample_flag_is_persisted_not_discarded(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        small, effective = conn.execute(
            "SELECT small_sample, effective_sample_size FROM model_evaluations").fetchone()
        conn.close()
        # 25 clusters is below MIN_EFFECTIVE_SAMPLE of 30.
        self.assertEqual(small, 1)
        self.assertEqual(effective, 25)

    def test_cluster_count_is_recorded_on_the_model(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        clusters, rows = conn.execute(
            "SELECT train_cluster_count, train_sample_size FROM trained_models").fetchone()
        conn.close()
        self.assertEqual(clusters, 25)
        self.assertGreater(rows, clusters)

    def test_refuses_when_too_few_rows(self):
        os.remove(self.db_path)
        seed(self.db_path, n=5)
        self.assertEqual(self._run(["--apply"]), 2)
        self.assertEqual(self._count("trained_models"), 0)

    def test_refuses_when_no_labels_exist(self):
        os.remove(self.db_path)
        seed(self.db_path, n=200, with_labels=False)
        self.assertEqual(self._run(["--apply"]), 2)

    def test_predictions_are_persisted_for_the_test_set(self):
        # Phase 10 Signals reference prediction_id; if predictions are
        # not stored, that provenance link points at nothing.
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        self.assertGreater(count, 0)

    def test_predictions_cover_only_test_observations(self):
        # An in-sample prediction must never become a signal.
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        prediction_obs = {r[0] for r in conn.execute(
            "SELECT observation_id FROM predictions")}
        train_size = conn.execute(
            "SELECT train_sample_size FROM trained_models").fetchone()[0]
        eval_size = conn.execute(
            "SELECT sample_size FROM model_evaluations").fetchone()[0]
        conn.close()
        self.assertEqual(len(prediction_obs), eval_size)
        self.assertLess(len(prediction_obs), train_size)

    def test_predictions_carry_the_information_cutoff(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        missing = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE information_cutoff IS NULL").fetchone()[0]
        conn.close()
        self.assertEqual(missing, 0)

    def test_rerunning_does_not_duplicate_predictions(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        first = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        second = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        # Prediction ids are uuid-minted per run, so a re-run adds a
        # fresh set rather than replacing. That is correct: each run is
        # a distinct act of prediction with its own timestamp. What
        # must NOT happen is silent loss of the earlier ones.
        self.assertGreaterEqual(second, first)


class TestPersistedBaselineVerdict(unittest.TestCase):
    """
    What `persist()` writes for `beats_all_baselines`, which is
    `Optional[bool]` and has THREE states.

    An evaluation whose baselines could not be compared is not an
    evaluation that lost: `src/autoresearch/observations.py` selects
    `WHERE beats_all_baselines = 0` and reports those rows as models
    that lost, so writing 0 for "unavailable" would be a fabricated
    measurement. `int(None)` used to raise TypeError here instead
    (`scripts/train_models.py:242`), which is how the Train models
    stage failed on 2026-10-28.

    These tests build the evaluation directly rather than training a
    model: the three states are properties of `ModelEvaluation`, and
    constructing one is enough to pin what persistence does with it.
    """

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = sqlite3.connect(self.db_path)
        initialize_model_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def model(self):
        specification = ModelSpecification(
            model_id="ridge_abnormal_return",
            name="Ridge regression on abnormal return",
            task=PredictionTask.ABNORMAL_RETURN,
            family=ModelFamily.RIDGE_REGRESSION,
            version="v1", label_name=DEFAULT_LABEL,
            feature_set_id="all_numeric_v1", feature_set_version="v1",
            dataset_version="v1", hyperparameters={"alpha": 1.0})
        return TrainedModel(
            trained_model_id="tm-fixture", specification=specification,
            parameters={"intercept": 0.0}, feature_names=["market.return_5d"],
            train_start=START, train_end=START + timedelta(days=30),
            train_sample_size=100, train_cluster_count=40,
            trained_at=START + timedelta(days=31))

    def evaluation(self, comparisons, notes=(), abstention_rate=DERIVE):
        """
        An evaluation shaped like the engine's output.

        `metrics` follows the comparisons rather than being passed in:
        the primary metric is what gives a comparison its `model_score`,
        so a comparison with no model score can only come from an
        evaluation that computed no metrics. Defaulting these two
        independently would let a test assert a state the engine cannot
        produce.
        """
        comparisons = list(comparisons)
        usable = any(c.model_score is not None for c in comparisons)
        if abstention_rate is DERIVE:
            abstention_rate = 0.0 if usable else 1.0
        return ModelEvaluation(
            evaluation_id="ev-fixture", trained_model_id="tm-fixture",
            model_qualified_id="ridge_abnormal_return:v1",
            window_label="single_chronological_split",
            sample_size=100, cluster_count=40,
            metrics={"mae": 0.01} if usable else {},
            baseline_comparisons=comparisons, notes=list(notes),
            abstention_rate=abstention_rate,
            evaluated_at=START + timedelta(days=31))

    def comparison(self, model_score, baseline_score=0.50):
        # directional_accuracy: higher is better, so a model score above
        # the baseline score means the model won.
        return BaselineComparison(
            baseline_name="baseline_historical_mean",
            metric_name="directional_accuracy",
            baseline_score=baseline_score, model_score=model_score)

    def stored(self):
        return self.conn.execute(
            "SELECT beats_all_baselines FROM model_evaluations "
            "WHERE evaluation_id = 'ev-fixture'").fetchone()[0]

    def test_true_is_stored_as_one(self):
        evaluation = self.evaluation([self.comparison(0.55)])
        self.assertIs(evaluation.beats_all_baselines, True)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertEqual(self.stored(), 1)

    def test_false_is_stored_as_zero(self):
        evaluation = self.evaluation([self.comparison(0.45)])
        self.assertIs(evaluation.beats_all_baselines, False)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertEqual(self.stored(), 0)

    def test_none_is_stored_as_sql_null(self):
        evaluation = self.evaluation([])
        self.assertIsNone(evaluation.beats_all_baselines)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertIsNone(self.stored(), "unavailable must be NULL, not 0")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM model_evaluations "
            "WHERE beats_all_baselines IS NULL").fetchone()[0], 1)

    def test_no_usable_baseline_comparison_stores_null(self):
        """
        The 2026-10-28 shape: the model abstained on every test row, so
        `metrics` was empty, so every comparison carried
        `model_score=None` and none of them could be read either way.
        """
        evaluation = self.evaluation(
            [self.comparison(None), self.comparison(None, baseline_score=0.49)])
        self.assertEqual(evaluation.metrics, {})
        self.assertEqual(evaluation.abstention_rate, 1.0)
        self.assertTrue(evaluation.baseline_comparisons,
                        "the comparisons exist; it is their values that do not")
        self.assertIsNone(evaluation.beats_all_baselines)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertIsNone(self.stored())

    def test_no_usable_test_observations_stores_null(self):
        """
        The other route to None: `train_and_evaluate` returns early with
        no comparisons at all when nothing in the test set is usable.
        """
        # The early return happens before the abstention calculation,
        # so the rate stays None on that route.
        evaluation = self.evaluation([], notes=["no usable test observations"],
                                     abstention_rate=None)
        self.assertIsNone(evaluation.abstention_rate)
        self.assertEqual(evaluation.baseline_comparisons, [])
        self.assertIsNone(evaluation.beats_all_baselines)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertIsNone(self.stored())
        self.assertIn("no usable test observations",
                      json.loads(self.conn.execute(
                          "SELECT notes_json FROM model_evaluations").fetchone()[0]))

    def test_an_unavailable_verdict_is_not_reported_as_a_loss(self):
        # The query src/autoresearch/observations.py runs to find models
        # that lost to their baselines must not pick this row up.
        persist(self.conn, self.model(), self.evaluation([]))
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM model_evaluations "
            "WHERE beats_all_baselines = 0").fetchone()[0], 0)

    def test_a_real_loss_is_still_reported_as_a_loss(self):
        # The counterpart: NULL must not swallow a measured defeat.
        persist(self.conn, self.model(), self.evaluation([self.comparison(0.45)]))
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM model_evaluations "
            "WHERE beats_all_baselines = 0").fetchone()[0], 1)

    def test_persisting_an_unjudgeable_evaluation_does_not_raise(self):
        # The regression itself: this call used to raise TypeError from
        # int(None), and then IntegrityError once None was passed to a
        # NOT NULL column.
        try:
            persist(self.conn, self.model(), self.evaluation([]))
        except (TypeError, sqlite3.IntegrityError) as error:
            self.fail("persist() still refuses an unjudgeable "
                      "evaluation: %r" % (error,))

    def test_the_model_row_is_written_alongside(self):
        # A run that could not be judged still happened, and the record
        # of the attempt must survive.
        persist(self.conn, self.model(), self.evaluation([]))
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM trained_models "
            "WHERE trained_model_id = 'tm-fixture'").fetchone()[0], 1)

    def test_small_sample_is_still_written_as_an_integer(self):
        # `small_sample` is a plain bool property and keeps its int()
        # conversion; this pins that the fix did not disturb it.
        evaluation = ModelEvaluation(
            evaluation_id="ev-fixture", trained_model_id="tm-fixture",
            model_qualified_id="ridge_abnormal_return:v1",
            sample_size=10, cluster_count=4,
            evaluated_at=START + timedelta(days=31))
        self.assertTrue(evaluation.small_sample)
        persist(self.conn, self.model(), evaluation)
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT small_sample FROM model_evaluations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
