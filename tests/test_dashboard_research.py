"""
tests/test_dashboard_research.py
------------------------------------------
Phase 23 §78, §79, §80 — the Autonomous Research Lab workspace.

The sidebar-payload test in `test_dashboard_experiments.py` already
guards the failure that took the whole terminal down twice. This file
guards the Lab's own numbers, and one property that matters more than
the layout:

    THE PAGE MUST NOT BE ABLE TO SHOW ONLY THE SUCCESSES.

`_collect_research_lab` reports `refused_total` beside `questions_total`
and `not_supported` beside `supported`, and the tests below pin both
pairs. A research dashboard that leads with its promising candidates
and buries its refusals is a marketing page.
"""

import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator
from src.data_access.autoresearch_schema import initialize_autoresearch_schema


def a_database():
    conn = sqlite3.connect(":memory:")
    initialize_autoresearch_schema(conn)
    return conn


def a_question(conn, question_id, *, triage="testable", version="v1",
               priority=0.5):
    conn.execute("""
        INSERT INTO autoresearch_questions (
            question_id, method_version, title, question, source_type,
            triage, triage_reason, priority, sample_size, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (question_id, version, "t", "Is this a question?", "memory_pattern",
          triage, "a stated reason", priority, 200, "2026-09-07"))
    conn.commit()


def a_conclusion(conn, conclusion_id, *, conclusion="rejected", version="v1",
                 effect=-0.02, in_sample=0.05, promising=0):
    conn.execute("""
        INSERT INTO autoresearch_hypotheses (
            hypothesis_id, method_version, statement, mechanism,
            claim_fingerprint, created_at
        ) VALUES (?,?,?,?,?,?)
    """, ("h-" + conclusion_id, version, "a statement", "a mechanism",
          "fp-" + conclusion_id, "2026-09-07"))
    conn.execute("""
        INSERT INTO autoresearch_conclusions (
            conclusion_id, method_version, hypothesis_id, conclusion,
            confidence, effect, effect_in_sample, sample_size, reasons_json,
            promising, concluded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (conclusion_id, version, "h-" + conclusion_id, conclusion, "low",
          effect, in_sample, 200, '["because"]', promising, "2026-09-07"))
    conn.commit()


class TestTheLabPayload(unittest.TestCase):

    def test_every_key_the_sidebar_reads_exists_in_the_payload(self):
        """
        The same guard that catches a blank terminal. Phase 23 adds a
        sidebar entry reading `D.researchlab`, and the mistake of
        adding one without the payload has now been made twice.
        """
        html = DashboardGenerator().generate_report(conn=a_database())
        data = json.loads(re.search(r"var D = (\{.*?\});\n", html, re.S).group(1))
        navigation = re.search(r"var NAV = \[(.*?)\n  \];", html, re.S)
        self.assertIsNotNone(navigation)
        referenced = set(re.findall(r"D\.([A-Za-z_][A-Za-z0-9_]*)",
                                    navigation.group(1)))
        missing = sorted(name for name in referenced if name not in data)
        self.assertEqual(missing, [], "sidebar dereferences %s" % missing)

    def test_the_lab_key_is_present(self):
        html = DashboardGenerator().generate_report(conn=a_database())
        data = json.loads(re.search(r"var D = (\{.*?\});\n", html, re.S).group(1))
        self.assertIn("researchlab", data)


class TestTheLabCollector(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.generator = DashboardGenerator()

    def tearDown(self):
        self.conn.close()

    def test_an_absent_table_is_reported_as_unavailable(self):
        empty = sqlite3.connect(":memory:")
        self.assertFalse(
            self.generator._collect_research_lab(empty)["available"])
        empty.close()

    def test_an_empty_table_is_reported_as_unavailable(self):
        self.assertFalse(
            self.generator._collect_research_lab(self.conn)["available"])

    def test_only_one_methodology_version_is_counted(self):
        """§84: versions coexist; a page that blends them doubles itself."""
        a_question(self.conn, "q-old", version="v0")
        a_question(self.conn, "q-new", version="v1")
        collected = self.generator._collect_research_lab(self.conn)
        self.assertEqual(collected["method_version"], "v1")
        self.assertEqual(collected["questions_total"], 1)

    def test_refusals_are_counted_beside_the_testable_questions(self):
        """
        §6: seven of eight triage states are a "no", and the page must
        not be able to show only the eighth.
        """
        a_question(self.conn, "q-1", triage="testable")
        a_question(self.conn, "q-2", triage="untestable")
        a_question(self.conn, "q-3", triage="insufficient_data")
        a_question(self.conn, "q-4", triage="duplicate")
        collected = self.generator._collect_research_lab(self.conn)
        self.assertEqual(collected["questions_total"], 4)
        self.assertEqual(collected["testable"], 1)
        self.assertEqual(collected["refused_total"], 3)

    def test_every_refusal_carries_its_reason_to_the_page(self):
        a_question(self.conn, "q-2", triage="untestable")
        collected = self.generator._collect_research_lab(self.conn)
        self.assertTrue(collected["refused"])
        self.assertTrue(collected["refused"][0][2].strip())

    def test_unsupported_conclusions_are_counted_beside_supported(self):
        """§42: a record filtered to its successes is not a record."""
        a_conclusion(self.conn, "c-1", conclusion="supported", effect=0.05,
                     promising=1)
        a_conclusion(self.conn, "c-2", conclusion="rejected")
        a_conclusion(self.conn, "c-3", conclusion="inconclusive")
        a_conclusion(self.conn, "c-4", conclusion="insufficient_data")
        collected = self.generator._collect_research_lab(self.conn)
        self.assertEqual(collected["conclusions_total"], 4)
        self.assertEqual(collected["supported"], 1)
        self.assertEqual(collected["not_supported"], 3)

    def test_repeated_claims_are_visible(self):
        """§13, §21: evidence is thinner than a raw count suggests."""
        a_conclusion(self.conn, "c-1")
        self.conn.execute(
            "UPDATE autoresearch_hypotheses SET claim_fingerprint = 'same'")
        self.conn.execute("""
            INSERT INTO autoresearch_hypotheses (
                hypothesis_id, method_version, statement, mechanism,
                claim_fingerprint, created_at
            ) VALUES ('h-twin','v1','s','m','same','2026-09-07')
        """)
        a_question(self.conn, "q-1")
        self.conn.commit()
        collected = self.generator._collect_research_lab(self.conn)
        self.assertEqual(collected["hypotheses"], 2)
        self.assertEqual(collected["distinct_claims"], 1)
        self.assertEqual(collected["repeated_claims"], 1)

    def test_the_conclusion_row_shape_matches_what_the_view_indexes(self):
        """
        The view reads this tuple positionally. A column inserted in
        the middle silently shifts every field after it -- the failure
        that made Phase 19 print "unknown" for every direction cohort.
        """
        a_question(self.conn, "q-1")
        a_conclusion(self.conn, "c-1")
        row = self.generator._collect_research_lab(self.conn)["conclusions"][0]
        self.assertEqual(len(row), 24)
        self.assertEqual(row[0], "c-1")
        self.assertEqual(row[2], "rejected")


class TestTheLabIsReadOnly(unittest.TestCase):
    """§33, §34: the dashboard is a static file with no server."""

    def setUp(self):
        self.conn = a_database()
        a_question(self.conn, "q-1")
        self.html = DashboardGenerator().generate_report(conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_lab_offers_a_command_rather_than_a_run_button(self):
        self.assertIn("scripts/run_research.py", self.html)

    def test_the_page_makes_no_network_calls(self):
        for word in ("fetch(", "XMLHttpRequest", "WebSocket"):
            self.assertNotIn(word, self.html)

    def test_the_page_states_that_production_is_untouched(self):
        self.assertIn("Nu modifica nimic din productie", self.html)


if __name__ == "__main__":
    unittest.main()
