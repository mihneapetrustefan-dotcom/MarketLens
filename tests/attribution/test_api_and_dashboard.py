"""
tests/attribution/test_api_and_dashboard.py
-------------------------------------------------------
The query surface, the export, and the workspace.

Covers §53, §57 and §49-§52, plus the profile statistics of §28-§36.

THE RECURRING CONCERN
-------------------------
Everything here is about what a reader is allowed to conclude. An
endpoint that can return a conclusion without its evidence invites a
consumer that never checks one. A profile that reports a rate over
eleven observations invites a decision. A page that shows an error
distribution without its coverage invites the reader to believe the
distribution is complete.
"""

import csv
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.attribution import analytics, api
from src.attribution.pipeline import run, save
from src.data_access.attribution_schema import initialize_attribution_schema
from src.data_access.outcome_schema import initialize_outcome_schema
from src.dashboard import DashboardGenerator
from src.domain.attribution_models import MIN_COHORT_SAMPLE

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


class SeededCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        initialize_outcome_schema(self.conn)
        initialize_attribution_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_outcome(self, subject_id, *, direction_result="miss",
                    simple_return=-0.03, status="available",
                    trained_model_id="tm-1", horizon="5d", mfe=0.01):
        self.conn.execute("""
            INSERT OR REPLACE INTO outcome_measurements (
                subject_kind, subject_id, horizon, method_version,
                horizon_value, horizon_unit, status, reference_rule,
                direction_result, expected_direction, expected_return,
                simple_return, mfe, mae, time_to_mfe_seconds, instrument_id,
                trained_model_id, model_status, signal_status, confidence,
                strength, computed_at
            ) VALUES ('signal',?,?,'v1',5.0,'d',?,'first_close_at_or_after_cutoff',
                      ?,'long',0.02,?,?,-0.02,86400.0,'i-1',?,'evaluated',
                      'active',0.3,0.5,'2026-09-05T00:00:00+00:00')
        """, (subject_id, horizon, status, direction_result, simple_return,
              mfe, trained_model_id))
        self.conn.commit()

    def attribute_all(self):
        found, _, _ = run(self.conn)
        save(self.conn, found)
        return found


class TestTheQueryApi(SeededCase):

    def test_a_listed_attribution_always_arrives_with_its_evidence(self):
        """
        §22. An endpoint that could omit evidence makes it easy to build
        a consumer that never looks at any.
        """
        self.add_outcome("sig-1")
        self.attribute_all()
        rows = api.list_attributions(self.conn)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["evidence"], row["error_type"])

    def test_hypotheticals_are_excluded_unless_asked_for(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        self.conn.execute(
            "UPDATE error_attributions SET observability='hypothetical'")
        self.conn.commit()
        self.assertEqual(api.list_attributions(self.conn), [])
        self.assertTrue(api.list_attributions(self.conn,
                                              observability="hypothetical"))

    def test_a_subject_query_returns_the_primary_first(self):
        self.add_outcome("sig-1", mfe=0.08)
        self.attribute_all()
        rows = api.errors_for_signal(self.conn, "sig-1")
        self.assertEqual(rows[0]["role"], "primary")

    def test_the_summary_reports_coverage_before_any_rate(self):
        self.add_outcome("sig-1")
        self.add_outcome("sig-2", status="pending")
        self.attribute_all()
        summary = api.summary(self.conn)
        self.assertIn("coverage", summary)
        self.assertAlmostEqual(summary["coverage"], 0.5, places=9)

    def test_by_type_separates_primary_from_contributing(self):
        """
        "was usually the main cause" and "was often involved" are
        different claims, and a single count conflates them.
        """
        self.add_outcome("sig-1", mfe=0.08)
        self.attribute_all()
        rows = {row["error_type"]: row for row in api.by_type(self.conn)}
        self.assertIn("prediction_error", rows)
        self.assertIn("primary", rows["prediction_error"])
        self.assertIn("contributing", rows["prediction_error"])

    def test_by_type_marks_which_members_are_not_errors(self):
        self.add_outcome("sig-1", direction_result="hit", simple_return=0.021)
        self.attribute_all()
        rows = {row["error_type"]: row for row in api.by_type(self.conn)}
        if "no_error" in rows:
            self.assertFalse(rows["no_error"]["is_error"])

    def test_listing_is_paginated_with_a_hard_ceiling(self):
        for index in range(5):
            self.add_outcome(f"sig-{index}")
        self.attribute_all()
        self.assertEqual(len(api.list_attributions(self.conn, limit=2)), 2)
        self.assertLessEqual(
            len(api.list_attributions(self.conn, limit=10_000)),
            api.MAX_LIMIT)

    def test_the_review_queue_is_queryable(self):
        self.assertEqual(api.review_queue(self.conn), [])


class TestCounterfactualSafety(SeededCase):
    """§23, §24 — architecture, never an answer presented as history."""

    def test_every_question_is_labelled_hypothetical(self):
        self.add_outcome("sig-1")
        for question in api.COUNTERFACTUAL_QUESTIONS:
            result = api.counterfactual(self.conn, "signal", "sig-1", "5d",
                                        question)
            self.assertEqual(result["observability"], "hypothetical")

    def test_no_alternative_outcome_is_ever_computed(self):
        self.add_outcome("sig-1")
        for question in api.COUNTERFACTUAL_QUESTIONS:
            result = api.counterfactual(self.conn, "signal", "sig-1", "5d",
                                        question)
            self.assertIsNone(result["result"])

    def test_it_states_what_would_be_needed_to_answer(self):
        self.add_outcome("sig-1")
        result = api.counterfactual(self.conn, "signal", "sig-1", "5d",
                                    "better_fill")
        self.assertIn("a fill", result["requires"])
        self.assertFalse(result["answerable_now"])

    def test_it_carries_observed_facts_without_pretending_they_answer(self):
        self.add_outcome("sig-1", mfe=0.08)
        result = api.counterfactual(self.conn, "signal", "sig-1", "5d",
                                    "earlier_entry")
        self.assertAlmostEqual(result["observed"]["mfe"], 0.08)
        self.assertIsNone(result["result"])

    def test_an_unknown_question_is_refused(self):
        with self.assertRaises(ValueError):
            api.counterfactual(self.conn, "signal", "sig-1", "5d", "invent")


class TestExport(SeededCase):
    """§57 — research export."""

    def test_it_writes_a_csv_with_a_header(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "attribution.csv")
            count = api.export_csv(self.conn, path)
            self.assertGreater(count, 0)
            with open(path, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), count)
            self.assertIn("error_type", rows[0])
            self.assertIn("observability", rows[0])

    def test_the_export_joins_the_outcome_it_diagnosed(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        rows = api.export_rows(self.conn)
        self.assertIn("simple_return", rows[0])
        self.assertIn("mfe", rows[0])

    def test_it_reports_how_much_evidence_backs_each_row(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        for row in api.export_rows(self.conn):
            self.assertGreater(row["evidence_count"], 0)

    def test_hypotheticals_are_excluded_from_the_export_by_default(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        self.conn.execute(
            "UPDATE error_attributions SET observability='hypothetical'")
        self.conn.commit()
        self.assertEqual(api.export_rows(self.conn), [])

    def test_the_evidence_table_exports_separately(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "evidence.csv")
            self.assertGreater(api.export_evidence_csv(self.conn, path), 0)


class TestProfileStatistics(SeededCase):
    """§28-§36, §62 — clustering with caution built in."""

    def test_a_small_cohort_declines_to_report_a_rate(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        profiles = analytics.build_profiles(
            analytics.load_attributions(self.conn))
        overall = next(p for p in profiles if p.cohort_kind == "overall")
        self.assertTrue(overall.small_sample)
        self.assertIn("too few", overall.describe())

    def test_a_large_cohort_uses_hedged_language(self):
        """§62: 'evidence suggests', never 'proved'."""
        for index in range(MIN_COHORT_SAMPLE + 5):
            self.add_outcome(f"sig-{index}")
        self.attribute_all()
        profiles = analytics.build_profiles(
            analytics.load_attributions(self.conn))
        overall = next(p for p in profiles if p.cohort_kind == "overall")
        self.assertFalse(overall.small_sample)
        self.assertIn("evidence suggests", overall.describe())
        self.assertNotIn("prove", overall.describe())

    def test_unassessable_outcomes_stay_out_of_the_denominator(self):
        """
        Folding silence into a denominator would report an execution
        error rate of 0%, which reads as flawless and means never
        attempted.
        """
        self.add_outcome("sig-1")
        self.add_outcome("sig-2", status="pending")
        self.attribute_all()
        profiles = analytics.build_profiles(
            analytics.load_attributions(self.conn))
        overall = next(p for p in profiles if p.cohort_kind == "overall")
        self.assertEqual(overall.assessed, 1)
        self.assertEqual(overall.not_assessable, 1)

    def test_a_cohort_with_nothing_assessed_reports_none_not_zero(self):
        self.add_outcome("sig-1", status="pending")
        self.attribute_all()
        profiles = analytics.build_profiles(
            analytics.load_attributions(self.conn))
        overall = next(p for p in profiles if p.cohort_kind == "overall")
        self.assertIsNone(overall.error_rate)
        self.assertIn("nothing can be said", overall.describe())

    def test_the_integrity_check_passes_on_a_clean_run(self):
        """§67, as a query rather than a promise."""
        self.add_outcome("sig-1")
        self.attribute_all()
        for name, count in analytics.integrity_check(self.conn).items():
            self.assertEqual(count, 0, name)

    def test_the_integrity_check_notices_evidence_without_a_conclusion(self):
        self.add_outcome("sig-1")
        self.attribute_all()
        self.conn.execute("DELETE FROM error_attributions")
        self.conn.commit()
        self.assertGreater(
            analytics.integrity_check(self.conn)["orphan_evidence"], 0)


class TestTheWorkspace(SeededCase):
    """§49-§52."""

    def collect(self):
        generator = DashboardGenerator.__new__(DashboardGenerator)
        return generator._collect_attribution(self.conn)

    def test_it_renders_before_phase_20_has_ever_run(self):
        conn = sqlite3.connect(":memory:")
        generator = DashboardGenerator.__new__(DashboardGenerator)
        self.assertEqual(generator._collect_attribution(conn),
                         {"available": False})
        conn.close()

    def test_an_empty_attribution_table_is_reported_not_crashed(self):
        self.assertEqual(self.collect(), {"available": False})

    def test_it_reports_coverage(self):
        self.add_outcome("sig-1")
        self.add_outcome("sig-2", status="pending")
        self.attribute_all()
        data = self.collect()
        self.assertAlmostEqual(data["coverage"], 0.5, places=9)

    def test_it_pins_a_single_methodology_version(self):
        """
        Versions coexist by design, so a page that did not filter would
        add two methodologies together and double the findings over the
        same subjects.
        """
        self.add_outcome("sig-1")
        self.attribute_all()
        found, _, _ = run(self.conn, method_version="v2")
        save(self.conn, found)
        data = self.collect()
        total_rows = self.conn.execute(
            "SELECT COUNT(*) FROM error_attributions WHERE role='primary'"
        ).fetchone()[0]
        self.assertLess(data["total"], total_rows)
        self.assertEqual(data["method_version"], "v2")


class TestThePageSaysTheRightThings(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "src", "dashboard.py"),
                  encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_the_workspace_exists_and_is_routed(self):
        self.assertIn("function viewAttribution()", self.source)
        self.assertIn('v === "attribution"', self.source)

    def test_it_states_that_a_loss_is_not_a_mistake(self):
        self.assertIn("O pierdere nu este o greseala", self.source)

    def test_it_names_the_layers_that_cannot_be_assessed(self):
        self.assertIn("ce tabel lipseste", self.source)

    def test_it_states_that_confidence_is_not_a_probability(self):
        self.assertIn("nu o probabilitate", self.source)

    def test_it_separates_severity_from_confidence_in_words(self):
        self.assertIn("cat de mult a contat, nu cat de siguri suntem",
                      self.source)

    def test_it_states_that_nothing_is_modified(self):
        self.assertIn("Nicio concluzie de aici nu modifica", self.source)

    def test_the_review_queue_says_it_does_not_close_itself(self):
        self.assertIn("nu se inchid automat", self.source)

    def test_there_is_one_definition_of_an_error_label(self):
        self.assertEqual(self.source.count("var ERROR_LABEL"), 1)


if __name__ == "__main__":
    unittest.main()
