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
    load_dataset, chronological_split, main, DEFAULT_LABEL,
)
from src.data_access.research_schema import initialize_research_schema
from src.data_access.model_schema import initialize_model_schema

START = datetime(2026, 6, 1, tzinfo=timezone.utc)


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
        names, _, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertNotIn("event.event_type", names)

    def test_numeric_features_from_both_phases_are_included(self):
        names, _, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertIn("market.return_5d", names)
        self.assertIn("event.count_7d", names)

    def test_rows_are_ordered_oldest_first(self):
        _, _, _, cutoffs, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertEqual(cutoffs, sorted(cutoffs))

    def test_observations_without_labels_are_dropped(self):
        self.conn.execute("DELETE FROM research_labels WHERE observation_id = 'obs-0000'")
        self.conn.commit()
        _, X, _, _, _, _ = load_dataset(self.conn, DEFAULT_LABEL)
        self.assertEqual(len(X), 199)

    def test_invalid_quality_observations_are_excluded_but_counted(self):
        self.conn.execute("UPDATE research_observations SET quality_level='invalid' "
                          "WHERE observation_id IN ('obs-0000','obs-0001')")
        self.conn.commit()
        _, X, _, _, _, quality_counts = load_dataset(self.conn, DEFAULT_LABEL)
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


if __name__ == "__main__":
    unittest.main()
