"""
tests/outcomes/test_dashboard_outcomes.py
-------------------------------------------------
The Outcome Intelligence workspace (§42-§45).

WHAT THESE DEFEND
---------------------
1. The page renders on a database where Phase 19 has never run. The
   outcome tables are created by a script, and a dashboard that
   required them would break every database that predates this phase.
2. Coverage is presented, not just the hit rate. "51% directional
   accuracy" means something very different at 66% coverage than at
   95%.
3. Training metrics and realized outcomes stay in separate columns
   (§44, §45). A model that scored well on a held-out split and badly
   forward is the case worth seeing, and pooling them hides it.
4. The multiple-testing caveat is on the page with the tables (§41),
   not in a document nobody opens while reading them.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.outcome_schema import initialize_outcome_schema
from src.dashboard import DashboardGenerator

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class WorkspaceCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def collect(self):
        generator = DashboardGenerator.__new__(DashboardGenerator)
        return generator._collect_outcomes(self.conn)

    def seed(self, count=4):
        initialize_outcome_schema(self.conn)
        for index in range(count):
            self.conn.execute("""
                INSERT INTO outcome_measurements (
                    subject_kind, subject_id, horizon, method_version,
                    horizon_value, horizon_unit, status, reference_rule,
                    direction_result, expected_direction, simple_return,
                    mfe, mae, instrument_id, trained_model_id, model_status,
                    market_regime, confidence, strength, signal_status,
                    computed_at, data_as_of, bars_observed
                ) VALUES ('signal',?,?,'v1',?,?,'available',
                          'first_close_at_or_after_cutoff',?,?,?,?,?,
                          'i-1','tm-1','evaluated','bull',0.3,0.5,'active',
                          '2026-09-05T00:00:00+00:00','2026-09-03T00:00:00+00:00',6)
            """, (f"sig-{index}", "5d", 5.0, "d",
                  "hit" if index % 2 == 0 else "miss",
                  "long", 0.01 * (index + 1), 0.03, -0.02))
        self.conn.commit()


class TestItRendersWithoutTheOutcomeTables(WorkspaceCase):

    def test_an_absent_outcome_layer_is_reported_not_crashed(self):
        self.assertEqual(self.collect(), {"available": False})

    def test_an_empty_outcome_table_does_not_divide_by_zero(self):
        initialize_outcome_schema(self.conn)
        data = self.collect()
        self.assertTrue(data["available"])
        self.assertEqual(data["total"], 0)
        self.assertIsNone(data["coverage"])
        self.assertIsNone(data["accuracy"])

    def test_a_missing_trained_models_table_does_not_empty_the_page(self):
        """
        The Phase 17.5 lesson: an optional table joined into a query
        becomes a hard dependency, and `_rows` swallows the failure into
        an empty list rather than an error.
        """
        self.seed()
        data = self.collect()
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["quality"], [])


class TestCoverageIsPresentedBesideTheRate(WorkspaceCase):

    def test_coverage_is_computed_from_the_status_mix(self):
        self.seed(count=3)
        self.conn.execute("""
            INSERT INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, computed_at
            ) VALUES ('signal','sig-x','5d','v1',5.0,'d','insufficient_data',
                      'first_close_at_or_after_cutoff','insufficient_data',
                      '2026-09-05T00:00:00+00:00')
        """)
        self.conn.commit()
        data = self.collect()
        self.assertEqual(data["total"], 4)
        self.assertAlmostEqual(data["coverage"], 0.75, places=9)

    def test_accuracy_excludes_neutrals_from_the_denominator(self):
        self.seed(count=2)   # one hit, one miss
        self.conn.execute("""
            INSERT INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, computed_at
            ) VALUES ('signal','sig-n','5d','v1',5.0,'d','available',
                      'first_close_at_or_after_cutoff','neutral',
                      '2026-09-05T00:00:00+00:00')
        """)
        self.conn.commit()
        data = self.collect()
        self.assertEqual(data["neutrals"], 1)
        self.assertAlmostEqual(data["accuracy"], 0.5, places=9,
                               msg="a neutral was counted as a decision")

    def test_no_decided_measurement_yields_none_not_zero(self):
        """
        "No signal was right" and "nothing was measured" are different
        facts, and a rate of 0.0 asserts the first.
        """
        initialize_outcome_schema(self.conn)
        self.conn.execute("""
            INSERT INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, computed_at
            ) VALUES ('signal','sig-p','5d','v1',5.0,'d','pending',
                      'first_close_at_or_after_cutoff','insufficient_data',
                      '2026-09-05T00:00:00+00:00')
        """)
        self.conn.commit()
        self.assertIsNone(self.collect()["accuracy"])

    def test_the_decay_curve_is_ordered_by_time(self):
        initialize_outcome_schema(self.conn)
        for horizon, value, unit in (("10d", 10.0, "d"), ("1h", 1.0, "h"),
                                     ("5d", 5.0, "d")):
            self.conn.execute("""
                INSERT INTO outcome_measurements (
                    subject_kind, subject_id, horizon, method_version,
                    horizon_value, horizon_unit, status, reference_rule,
                    direction_result, computed_at
                ) VALUES ('signal',?,?,'v1',?,?,'available',
                          'first_close_at_or_after_cutoff','hit',
                          '2026-09-05T00:00:00+00:00')
            """, (f"sig-{horizon}", horizon, value, unit))
            self.conn.execute("""
                INSERT INTO outcome_aggregates (
                    aggregate_id, method_version, subject_kind, cohort_kind,
                    cohort_value, horizon, sample_size, computed_at
                ) VALUES (?, 'v1','signal','overall','all',?,10,
                          '2026-09-05T00:00:00+00:00')
            """, (f"oa-{horizon}", horizon))
        self.conn.commit()
        keys = [row[0] for row in self.collect()["decay"]]
        self.assertEqual(keys, ["1h", "5d", "10d"],
                         "'10d' sorted before '5d' as a string")


class TestThePageSaysTheRightThings(unittest.TestCase):
    """
    Rendered text, pinned. These are honesty requirements and they are
    easy to delete by accident while restyling.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "src", "dashboard.py"),
                  encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_the_workspace_exists_and_is_routed(self):
        self.assertIn("function viewOutcomes()", self.source)
        self.assertIn('v === "outcomes"', self.source)
        self.assertIn('id: "outcomes"', self.source)

    def test_it_states_that_insufficient_data_is_not_a_zero_or_a_miss(self):
        self.assertIn("nu inseamna niciodata randament zero", self.source)

    def test_it_states_that_profit_is_not_measured(self):
        """
        §2. Without this, a reader sees percentages and reasonably
        assumes they are returns on capital.
        """
        self.assertIn("Nu masoara profit", self.source)

    def test_the_multiple_testing_caveat_is_on_the_page(self):
        self.assertIn("Niciun test de semnificatie nu a fost rulat", self.source)
        self.assertIn("una din douazeci", self.source)

    def test_training_and_realized_metrics_are_separate_columns(self):
        self.assertIn("Evaluare la antrenare vs. rezultat realizat", self.source)

    def test_small_cohorts_are_flagged_in_every_table(self):
        self.assertIn("esantion mic", self.source)

    def test_the_methodology_version_is_shown(self):
        self.assertIn("O.method_version", self.source)

    def test_confidence_and_strength_are_shown_as_separate_cohorts(self):
        self.assertIn("Dupa scor de incredere", self.source)
        self.assertIn("Dupa forta semnalului", self.source)
        self.assertIn("forta NU e acelasi lucru cu increderea", self.source)

    def test_experimental_and_validated_outcomes_stay_distinguishable(self):
        """§59: the Phase 18 distinction must survive into this page."""
        self.assertIn("Dupa stare model", self.source)
        self.assertIn("experimental vs. validat", self.source)


if __name__ == "__main__":
    unittest.main()
