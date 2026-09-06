"""
tests/test_dashboard_experiments.py
--------------------------------------------
Phase 22 §67, §68: the Experiment Lab, and the payload it reads.

THE PROBLEM THIS FILE EXISTS FOR
------------------------------------
The sidebar is built at module scope, before anything renders, from
expressions like `D.attribution.available`. Phases 20 and 21 each added
such an entry but wired their collector into the operations payload
instead of the top-level one. The missing key threw a TypeError while
the sidebar array was being constructed, so the IIFE never finished,
`MLGo` was never defined, and the ENTIRE terminal rendered as a blank
page -- every workspace, not just the new one.

Nothing caught it. The collectors had tests, the SQL had tests, and the
generator produced a 593 KB file without complaining. The failure only
existed in a browser, and the page was never opened in one.

So the first test below parses the sidebar out of the generated
JavaScript and asserts that every payload key it dereferences is
actually present. It is deliberately generic: it will catch the next
phase that adds a nav entry and forgets the payload, which is the
mistake that has now been made twice.

The rest cover the Lab itself: one methodology version, drafts counted
apart from results, and a verdict vocabulary that matches what the
engine actually writes.
"""

import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator
from src.data_access.experiment_schema import initialize_experiment_schema


def a_database():
    conn = sqlite3.connect(":memory:")
    initialize_experiment_schema(conn)
    return conn


def store(conn, experiment_id, *, status="draft", version="v1",
          decision=None, effect=None, effect_in_sample=None,
          low=None, high=None, source="researcher"):
    conn.execute("""
        INSERT INTO experiments (
            experiment_id, method_version, name, experiment_type, status,
            statement, mechanism, hypothesis_source, baseline_name,
            baseline_evaluator, candidate_name, candidate_evaluator,
            fingerprint, metric, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (experiment_id, version, "experiment " + experiment_id, "signal",
          status, "a statement", "a mechanism", source, "all signals",
          "signal_all", "a candidate", "signal_strength_threshold",
          "fp-" + experiment_id, "directional_accuracy",
          "2026-09-0%d" % (len(experiment_id) % 9 + 1)))
    if decision is not None:
        conn.execute("""
            INSERT INTO experiment_results (
                run_id, experiment_id, metric, effect, effect_in_sample,
                effect_low, effect_high, decision, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, ("run-" + experiment_id, experiment_id, "directional_accuracy",
              effect, effect_in_sample, low, high, decision, "2026-09-05"))
    conn.commit()


class TestTheSidebarPayloadIsComplete(unittest.TestCase):
    """
    The regression test for a blank dashboard.

    A missing payload key is not a missing section: the sidebar is
    built before the router runs, so it takes the whole page down.
    """

    def test_every_key_the_sidebar_reads_exists_in_the_payload(self):
        html = DashboardGenerator().generate_report(conn=a_database())

        payload = re.search(r"var D = (\{.*?\});\n", html, re.S)
        self.assertIsNotNone(payload, "the payload is no longer assigned to D")
        data = json.loads(payload.group(1))

        navigation = re.search(r"var NAV = \[(.*?)\n  \];", html, re.S)
        self.assertIsNotNone(navigation, "the sidebar array is no longer named NAV")

        referenced = set(re.findall(r"D\.([A-Za-z_][A-Za-z0-9_]*)",
                                    navigation.group(1)))
        self.assertTrue(referenced, "the sidebar reads nothing - parser is broken")
        missing = sorted(name for name in referenced if name not in data)
        self.assertEqual(missing, [],
                         "the sidebar dereferences %s, which the payload does "
                         "not contain; the page will throw before it renders "
                         "anything at all" % missing)

    def test_the_page_still_renders_on_a_database_with_no_phase_tables(self):
        html = DashboardGenerator().generate_report(conn=sqlite3.connect(":memory:"))
        self.assertIn("MarketLens", html)


class TestExperimentCollector(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.generator = DashboardGenerator()

    def tearDown(self):
        self.conn.close()

    def test_an_absent_table_is_reported_as_unavailable(self):
        empty = sqlite3.connect(":memory:")
        self.assertFalse(
            self.generator._collect_experiments(empty)["available"])
        empty.close()

    def test_an_empty_table_is_reported_as_unavailable(self):
        self.assertFalse(
            self.generator._collect_experiments(self.conn)["available"])

    def test_only_one_methodology_version_is_counted(self):
        """
        §44: versions coexist by design, so a page that did not pin one
        would add two methodologies over the same experiments and
        report twice the research that was done.
        """
        store(self.conn, "exp-old", version="v0")
        store(self.conn, "exp-new", version="v1")
        collected = self.generator._collect_experiments(self.conn)
        self.assertEqual(collected["method_version"], "v1")
        self.assertEqual(collected["total"], 1)

    def test_drafts_are_counted_apart_from_results(self):
        """
        A proposal is not evidence. Counting drafts as completed work
        would report a body of research that was never carried out.
        """
        store(self.conn, "exp-a")
        store(self.conn, "exp-b")
        store(self.conn, "exp-c", status="rejected", decision="fail",
              effect=-0.02, effect_in_sample=0.05)
        collected = self.generator._collect_experiments(self.conn)
        self.assertEqual(collected["total"], 3)
        self.assertEqual(collected["drafted"], 2)
        self.assertEqual(collected["decided"], 1)

    def test_a_verdict_status_counts_as_completed(self):
        """
        The engine writes the verdict AS the status: a FAIL lands in
        'rejected', not 'completed'. Counting only 'completed' reported
        zero finished experiments on a database that had run one.
        """
        store(self.conn, "exp-p", status="passed", decision="pass",
              effect=0.05, effect_in_sample=0.06)
        store(self.conn, "exp-r", status="rejected", decision="fail",
              effect=-0.02, effect_in_sample=0.05)
        store(self.conn, "exp-i", status="inconclusive",
              decision="inconclusive")
        collected = self.generator._collect_experiments(self.conn)
        self.assertEqual(collected["completed"], 3)
        self.assertEqual(collected["passed"], 1)
        self.assertEqual(collected["failed"], 1)
        self.assertEqual(collected["inconclusive"], 1)

    def test_intervals_that_include_zero_are_counted(self):
        """
        §48: an interval spanning zero is the absence of a measurable
        effect, not a near miss, and the count keeps that visible.
        """
        store(self.conn, "exp-z", status="rejected", decision="fail",
              effect=0.01, effect_in_sample=0.02, low=-0.05, high=0.07)
        store(self.conn, "exp-c", status="passed", decision="pass",
              effect=0.06, effect_in_sample=0.06, low=0.02, high=0.10)
        self.assertEqual(
            self.generator._collect_experiments(self.conn)["spans_zero"], 1)

    def test_drafts_appear_in_the_listing_rather_than_vanishing(self):
        """
        The listing LEFT JOINs its results. An inner join would hide
        every proposal until it was run, which is how a backlog becomes
        invisible.
        """
        store(self.conn, "exp-draft")
        store(self.conn, "exp-done", status="rejected", decision="fail",
              effect=-0.01, effect_in_sample=0.04)
        listed = self.generator._collect_experiments(self.conn)["listing"]
        self.assertEqual({row[0] for row in listed}, {"exp-draft", "exp-done"})

    def test_the_overfitting_gap_is_measured_not_narrated(self):
        store(self.conn, "exp-fit", status="rejected", decision="fail",
              effect=-0.02, effect_in_sample=0.11)
        overfit = self.generator._collect_experiments(self.conn)["overfit"]
        self.assertEqual(len(overfit), 1)
        self.assertAlmostEqual(overfit[0][3], 0.13, places=6)

    def test_experiments_repeating_a_comparison_are_counted(self):
        """
        §41-§43: three experiments over one comparison are one piece
        of evidence with three names. The per-family correction misses
        them when they sit in different families, which is exactly how
        the first proposal sweep produced three identical results.
        """
        for name in ("exp-1", "exp-2", "exp-3"):
            self.conn.execute("""
                INSERT INTO experiments (
                    experiment_id, method_version, name, experiment_type,
                    status, statement, mechanism, hypothesis_source,
                    baseline_name, baseline_evaluator, baseline_params_json,
                    candidate_name, candidate_evaluator, candidate_params_json,
                    dataset_json, fingerprint, metric, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (name, "v1", "named differently: " + name, "signal", "draft",
                  "a statement", "a mechanism", "error_attribution",
                  "all signals", "signal_all", "{}",
                  "candidate " + name, "signal_strength_threshold",
                  '{"threshold": 0.5}', "{}", "fp-" + name,
                  "directional_accuracy", "2026-09-05"))
        self.conn.commit()
        collected = self.generator._collect_experiments(self.conn)
        self.assertEqual(collected["total"], 3)
        self.assertEqual(collected["duplicate_comparisons"], 2)

    def test_genuinely_different_comparisons_are_not_counted_as_repeats(self):
        store(self.conn, "exp-a")
        store(self.conn, "exp-b")
        self.conn.execute(
            "UPDATE experiments SET candidate_params_json = ? "
            "WHERE experiment_id = 'exp-b'", ('{"threshold": 0.9}',))
        self.conn.commit()
        self.assertEqual(
            self.generator._collect_experiments(self.conn)["duplicate_comparisons"], 0)

    def test_the_listing_columns_match_what_the_view_indexes(self):
        """
        The view reads this tuple positionally. A column added in the
        middle would silently shift every field after it, which is the
        failure mode that made Phase 19 print "unknown" for every
        direction cohort.
        """
        store(self.conn, "exp-x", status="rejected", decision="fail",
              effect=-0.01, effect_in_sample=0.02)
        row = self.generator._collect_experiments(self.conn)["listing"][0]
        self.assertEqual(len(row), 32)
        self.assertEqual(row[0], "exp-x")
        self.assertEqual(row[3], "rejected")
        self.assertEqual(row[10], "fail")


class TestTheLabIsReadOnly(unittest.TestCase):
    """§79, §80: the dashboard is a static file. It cannot run anything."""

    def setUp(self):
        self.conn = a_database()
        store(self.conn, "exp-a")
        self.html = DashboardGenerator().generate_report(conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_lab_offers_a_command_rather_than_a_run_button(self):
        self.assertIn("scripts/run_experiment.py", self.html)

    def test_the_page_makes_no_network_calls(self):
        for word in ("fetch(", "XMLHttpRequest", "WebSocket", "navigator.send"):
            self.assertNotIn(word, self.html, "the dashboard calls out to %s" % word)

    def test_pass_is_never_described_as_a_recommendation_to_deploy(self):
        """§4, §47: PASS means the predefined criteria were met."""
        self.assertIn("Nu inseamna profitabil", self.html)


if __name__ == "__main__":
    unittest.main()
