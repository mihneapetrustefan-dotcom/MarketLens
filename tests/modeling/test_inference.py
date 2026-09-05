"""
tests/modeling/test_inference.py
------------------------------------------
Applying a trained model to unscored observations.

WHAT THESE DEFEND
---------------------
One property above all: that the design matrix is built to the MODEL'S
stored column order, never to whatever `sorted(features)` happens to
return today.

A model's coefficients are positional. Add one feature to the database
after training, re-derive the ordering, and every coefficient lands on
the wrong column. The model still returns plausible numbers, the
signal layer still accepts them, and nothing anywhere reports a
problem. That is the failure mode worth a test that would otherwise
look paranoid — `test_a_new_feature_does_not_shift_the_columns` builds
exactly that situation and asserts the prediction is unchanged.

The rest assert refusals: no feature contract, too little coverage, a
feature computed after the cutoff it describes.
"""

import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.model_schema import initialize_model_schema
from src.data_access.research_schema import initialize_research_schema
from src.modeling.inference import (
    FeatureContractBroken, NoUsableModel, candidates, load_model,
    save_predictions, score,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


class InferenceCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_research_schema(self.conn)
        initialize_model_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    # ---------------- fixtures ----------------

    def add_model(self, feature_names=("f.a", "f.b"), coefficients=(2.0, 10.0),
                  intercept=0.0, trained_model_id="tm-1",
                  label_name="d5.abnormal_return", trained_at=None,
                  status="active"):
        # ACTIVE by default so the tests in THIS file keep testing what
        # they were written to test — the feature contract, the design
        # matrix, provenance. The quality gate that decides whether a
        # model may score at all is tested in
        # tests/modeling/test_model_quality_gate.py, where a
        # non-active status is the point rather than an obstacle.
        self.conn.execute("""
            INSERT INTO trained_models (
                trained_model_id, model_id, model_qualified_id, name, task,
                family, model_version, label_name, label_version,
                feature_set_id, feature_set_version, dataset_version,
                hyperparameters_json, parameters_json, feature_names_json,
                train_start, train_end, train_sample_size, train_cluster_count,
                status, training_notes_json, trained_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (trained_model_id, "ridge", f"ridge:{trained_model_id}", "Ridge",
              "abnormal_return", "ridge_regression", "v1", label_name, "v1",
              None, "v1", None, "{}",
              json.dumps({"family": "ridge_regression",
                          "coefficients": list(coefficients),
                          "intercept": intercept}),
              json.dumps(list(feature_names)),
              iso(NOW - timedelta(days=30)), iso(NOW - timedelta(days=8)),
              100, 50, status, "[]",
              iso(trained_at or (NOW - timedelta(days=1)))))
        self.conn.commit()

    def add_observation(self, observation_id="obs-1", cutoff=None,
                        quality="high", features=None, feature_as_of=None):
        cutoff = cutoff or (NOW - timedelta(days=1))
        self.conn.execute("""
            INSERT INTO research_observations
            (observation_id, event_id, instrument_id, benchmark_id, event_type,
             event_time, information_time, information_cutoff, quality_level,
             event_cluster_id, observation_created_at, dataset_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (observation_id, "ev-1", "i-aapl", "spy", "earnings",
              iso(cutoff), iso(cutoff), iso(cutoff), quality, "cl-1",
              iso(cutoff), "v1"))
        for name, value in (features or {"f.a": 1.0, "f.b": 0.5}).items():
            self.conn.execute("""
                INSERT INTO research_features
                (observation_id, qualified_name, namespace, value_json, as_of,
                 source, calculation, feature_version, is_contemporaneous)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (observation_id, name, name.split(".")[0], json.dumps(value),
                  iso(feature_as_of or cutoff), "test", "test", "v1", 0))
        self.conn.commit()


class TestTheFeatureContract(InferenceCase):
    """The property that makes inference safe at all."""

    def test_a_new_feature_does_not_shift_the_columns(self):
        """
        THE test in this file.

        Model trained on ["f.a", "f.b"] with coefficients [2, 10]. A
        third feature "f.aa" is then added to the database. Sorted, it
        lands BETWEEN them — so anything re-deriving the ordering would
        feed f.aa into the coefficient meant for f.b.

        The prediction must be identical to the two-feature case.
        """
        self.add_model(feature_names=("f.a", "f.b"), coefficients=(2.0, 10.0))
        self.add_observation("obs-1", features={"f.a": 1.0, "f.b": 0.5})
        predictions, _ = score(self.conn, now=NOW)
        baseline = predictions[0].predicted_value

        self.conn.execute("DELETE FROM predictions")
        self.add_observation("obs-2", features={"f.a": 1.0, "f.aa": 999.0,
                                                "f.b": 0.5})
        predictions, _ = score(self.conn, now=NOW)
        scored = {p.observation_id: p.predicted_value for p in predictions}

        self.assertAlmostEqual(scored["obs-2"], baseline, places=9,
                               msg="a feature the model never saw changed its "
                                   "prediction — the columns shifted")
        self.assertAlmostEqual(baseline, 2.0 * 1.0 + 10.0 * 0.5, places=9)

    def test_a_model_without_stored_feature_names_is_refused(self):
        self.add_model()
        self.conn.execute("UPDATE trained_models SET feature_names_json = '[]'")
        self.conn.commit()
        with self.assertRaises(FeatureContractBroken) as caught:
            load_model(self.conn)[0]
        self.assertIn("positional", str(caught.exception))

    def test_a_feature_the_model_never_saw_is_ignored(self):
        self.add_model(feature_names=("f.a",), coefficients=(3.0,))
        self.add_observation(features={"f.a": 2.0, "f.unknown": 1000.0})
        predictions, _ = score(self.conn, now=NOW)
        self.assertAlmostEqual(predictions[0].predicted_value, 6.0, places=9)

    def test_an_observation_with_too_few_features_is_skipped(self):
        self.add_model(feature_names=("f.a", "f.b", "f.c", "f.d"),
                       coefficients=(1.0, 1.0, 1.0, 1.0))
        self.add_observation(features={"f.a": 1.0})       # 25% coverage
        with self.assertRaises(FeatureContractBroken):
            score(self.conn, now=NOW, min_feature_coverage=0.5)

    def test_coverage_threshold_is_adjustable(self):
        self.add_model(feature_names=("f.a", "f.b", "f.c", "f.d"),
                       coefficients=(1.0, 1.0, 1.0, 1.0))
        self.add_observation(features={"f.a": 1.0})
        predictions, report = score(self.conn, now=NOW,
                                    min_feature_coverage=0.2)
        self.assertEqual(report.scored, 1)


class TestLeakage(InferenceCase):
    """Spec §8: the barrier does not reach rows read straight from a table."""

    def test_a_feature_computed_after_its_cutoff_is_refused(self):
        cutoff = NOW - timedelta(days=2)
        self.add_model()
        self.add_observation(cutoff=cutoff,
                             feature_as_of=cutoff + timedelta(hours=6))
        with self.assertRaises(FeatureContractBroken):
            score(self.conn, now=NOW)

    def test_the_leak_is_counted_not_silently_dropped(self):
        cutoff = NOW - timedelta(days=2)
        self.add_model()
        self.add_observation("obs-leak", cutoff=cutoff,
                             feature_as_of=cutoff + timedelta(hours=6))
        self.add_observation("obs-ok", cutoff=cutoff)
        predictions, report = score(self.conn, now=NOW)
        self.assertEqual(report.skipped_leaky_features, 1)
        self.assertEqual(report.scored, 1)

    def test_a_feature_computed_at_the_cutoff_is_fine(self):
        cutoff = NOW - timedelta(days=2)
        self.add_model()
        self.add_observation(cutoff=cutoff, feature_as_of=cutoff)
        predictions, _ = score(self.conn, now=NOW)
        self.assertEqual(len(predictions), 1)


class TestWhatGetsScored(InferenceCase):

    def test_an_observation_with_no_label_is_scored(self):
        """
        The whole point. `train_models.load_dataset` skips unlabelled
        observations; the absent label is the thing being predicted.
        """
        self.add_model()
        self.add_observation()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM research_labels").fetchone()[0], 0)
        predictions, _ = score(self.conn, now=NOW)
        self.assertEqual(len(predictions), 1)

    def test_an_invalid_observation_is_not_scored(self):
        self.add_model()
        self.add_observation(quality="invalid")
        predictions, report = score(self.conn, now=NOW)
        self.assertEqual(report.candidates, 0)
        self.assertEqual(predictions, [])

    def test_an_already_scored_observation_is_skipped(self):
        self.add_model()
        self.add_observation()
        predictions, _ = score(self.conn, now=NOW)
        save_predictions(self.conn, predictions)
        again, report = score(self.conn, now=NOW)
        self.assertEqual(again, [])
        self.assertEqual(report.candidates, 0)

    def test_rescore_redoes_them(self):
        self.add_model()
        self.add_observation()
        predictions, _ = score(self.conn, now=NOW)
        save_predictions(self.conn, predictions)
        again, _ = score(self.conn, now=NOW, rescore=True)
        self.assertEqual(len(again), 1)

    def test_an_old_observation_is_left_alone_by_default(self):
        """
        Scoring a two-month-old event manufactures a prediction the
        signal layer suppresses as stale on arrival.
        """
        self.add_model()
        self.add_observation(cutoff=NOW - timedelta(days=60))
        predictions, report = score(self.conn, now=NOW, max_age_days=30.0)
        self.assertEqual(report.candidates, 0)

    def test_the_age_bound_can_be_lifted(self):
        self.add_model()
        self.add_observation(cutoff=NOW - timedelta(days=60))
        predictions, report = score(self.conn, now=NOW, max_age_days=None)
        self.assertEqual(report.scored, 1)


class TestModelSelection(InferenceCase):

    def test_no_model_at_all_raises(self):
        self.add_observation()
        with self.assertRaises(NoUsableModel):
            score(self.conn, now=NOW)

    def test_the_newest_ACTIVE_model_is_used_by_default(self):
        """
        Newest *among the promoted ones*. Phase 18 changed the tie-break
        pool, not the tie-break: "newest" was never the problem, "any
        model at all" was.
        """
        self.add_model(trained_model_id="tm-old",
                       trained_at=NOW - timedelta(days=10))
        self.add_model(trained_model_id="tm-new",
                       trained_at=NOW - timedelta(days=1))
        self.assertEqual(load_model(self.conn)[0].trained_model_id, "tm-new")

    def test_a_newer_UNPROMOTED_model_does_not_displace_the_active_one(self):
        """
        The exact regression NEW-01 describes: training something newer
        must not change what production scores with.
        """
        self.add_model(trained_model_id="tm-active",
                       trained_at=NOW - timedelta(days=10), status="active")
        self.add_model(trained_model_id="tm-newer-but-unpromoted",
                       trained_at=NOW - timedelta(days=1), status="evaluated")
        self.assertEqual(load_model(self.conn)[0].trained_model_id, "tm-active")

    def test_a_model_can_be_pinned(self):
        self.add_model(trained_model_id="tm-old",
                       trained_at=NOW - timedelta(days=10))
        self.add_model(trained_model_id="tm-new",
                       trained_at=NOW - timedelta(days=1))
        self.assertEqual(
            load_model(self.conn, trained_model_id="tm-old")[0].trained_model_id,
            "tm-old")

    def test_scoring_will_not_pick_a_model_for_a_different_label(self):
        """
        A model fitted against 5-day abnormal return must not be used
        to score for a 20-day one just because it trained later.
        """
        self.add_model(trained_model_id="tm-d5", label_name="d5.abnormal_return",
                       trained_at=NOW - timedelta(days=5))
        self.add_model(trained_model_id="tm-d20", label_name="d20.abnormal_return",
                       trained_at=NOW - timedelta(days=1))
        picked, _ = load_model(self.conn, label_name="d5.abnormal_return")
        self.assertEqual(picked.trained_model_id, "tm-d5")


class TestProvenance(InferenceCase):

    def test_the_prediction_carries_the_observation_cutoff(self):
        """
        Not wall-clock time. The signal layer measures staleness from
        the information cutoff, and getting this wrong would make every
        prediction look permanently fresh.
        """
        cutoff = NOW - timedelta(days=3)
        self.add_model()
        self.add_observation(cutoff=cutoff)
        predictions, _ = score(self.conn, now=NOW)
        self.assertEqual(predictions[0].information_cutoff, cutoff)

    def test_the_prediction_is_traceable_to_its_model(self):
        self.add_model(trained_model_id="tm-x")
        self.add_observation()
        predictions, _ = score(self.conn, now=NOW)
        self.assertEqual(predictions[0].trained_model_id, "tm-x")
        self.assertTrue(predictions[0].observation_id)
        self.assertTrue(predictions[0].prediction_id)

    def test_predictions_round_trip_through_the_database(self):
        self.add_model()
        self.add_observation()
        predictions, _ = score(self.conn, now=NOW)
        self.assertEqual(save_predictions(self.conn, predictions), 1)
        row = self.conn.execute(
            "SELECT observation_id, predicted_value, information_cutoff, "
            "is_abstention FROM predictions").fetchone()
        self.assertEqual(row[0], "obs-1")
        self.assertAlmostEqual(row[1], predictions[0].predicted_value)
        self.assertEqual(row[3], 0)


if __name__ == "__main__":
    unittest.main()
