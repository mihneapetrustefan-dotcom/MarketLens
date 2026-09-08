"""
tests/test_dashboard_challengers.py
---------------------------------------------
Phase 24 §62-§65 — the Challenger Lab workspace.

Two properties matter more than the layout:

    THE PAGE CANNOT SHOW ONLY THE WINNERS.
    `rejected` and `not_superior` are collected beside `superior`, and
    the listing carries every status. On a comparison page that failure
    mode is worse than elsewhere, because the whole claim is fairness.

    THE PAGE CANNOT SHOW AN OVERALL SCORE.
    §37 forbids collapsing the comparison into one number, and the
    reason is mechanical: a sortable column gets sorted, and the top of
    a list of a hundred challengers is where the noise collects.
"""

import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator
from src.data_access.challenger_schema import initialize_challenger_schema


def a_database():
    conn = sqlite3.connect(":memory:")
    initialize_challenger_schema(conn)
    return conn


def a_challenger(conn, challenger_id, *, status="proposed", version=1,
                 method_version="v1", decision=None, effect=None):
    conn.execute("""
        INSERT INTO challengers (
            challenger_id, version, method_version, variant_type, name,
            status, candidate_id, hypothesis_id, experiment_id,
            conclusion_id, family_id, baseline_kind, baseline_name,
            baseline_version, change_kind, change_summary, fingerprint,
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (challenger_id, version, method_version, "signal",
          "challenger " + challenger_id, status, "cand-1", "h-1", "exp-1",
          "con-1", "fam-1", "current_signal_rule", "all signals",
          "signal-rule:all_signals@abc", "signal_filter_added",
          "restrict to a cohort", "fp-" + challenger_id, "2026-09-07"))
    if decision is not None:
        conn.execute("""
            INSERT INTO challenger_results (
                run_id, challenger_id, challenger_version, method_version,
                metric, effect, decision, reasons_json, scorecard_json,
                computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, ("run-" + challenger_id, challenger_id, version, method_version,
              "directional_accuracy", effect, decision, '["because"]',
              json.dumps({"performance": {"name": "performance",
                                          "value": effect,
                                          "verdict": "better",
                                          "detail": "d"}}),
              "2026-09-07"))
    conn.commit()


class TestTheLabPayload(unittest.TestCase):

    def test_every_key_the_sidebar_reads_exists_in_the_payload(self):
        """
        The guard that catches a blank terminal. Phase 24 adds a
        sidebar entry reading `D.challengers`, and the mistake of
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

    def test_the_challenger_key_is_present(self):
        html = DashboardGenerator().generate_report(conn=a_database())
        data = json.loads(re.search(r"var D = (\{.*?\});\n", html, re.S).group(1))
        self.assertIn("challengers", data)


class TestTheLabCollector(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        self.generator = DashboardGenerator()

    def tearDown(self):
        self.conn.close()

    def test_an_absent_table_is_reported_as_unavailable(self):
        empty = sqlite3.connect(":memory:")
        self.assertFalse(
            self.generator._collect_challengers(empty)["available"])
        empty.close()

    def test_an_empty_table_is_reported_as_unavailable(self):
        self.assertFalse(
            self.generator._collect_challengers(self.conn)["available"])

    def test_only_one_methodology_version_is_counted(self):
        a_challenger(self.conn, "chl-old", method_version="v0")
        a_challenger(self.conn, "chl-new", method_version="v1")
        collected = self.generator._collect_challengers(self.conn)
        self.assertEqual(collected["method_version"], "v1")
        self.assertEqual(collected["total"], 1)

    def test_only_the_latest_version_of_each_challenger_is_listed(self):
        """§9: versions accumulate; the list must not double-count them."""
        a_challenger(self.conn, "chl-1", version=1)
        a_challenger(self.conn, "chl-1", version=2)
        collected = self.generator._collect_challengers(self.conn)
        self.assertEqual(collected["total"], 1)
        self.assertEqual(collected["listing"][0][1], 2)

    def test_rejected_challengers_are_counted_and_listed(self):
        """§42: a record filtered to its winners is not a record."""
        a_challenger(self.conn, "chl-a", status="rejected",
                     decision="inferior", effect=-0.03)
        a_challenger(self.conn, "chl-b", status="promising",
                     decision="superior", effect=0.06)
        collected = self.generator._collect_challengers(self.conn)
        self.assertEqual(collected["total"], 2)
        self.assertEqual(collected["rejected"], 1)
        self.assertEqual(collected["superior"], 1)
        self.assertEqual(collected["not_superior"], 1)
        self.assertEqual(len(collected["listing"]), 2)

    def test_paper_candidates_are_counted_separately(self):
        a_challenger(self.conn, "chl-p", status="paper_candidate",
                     decision="superior", effect=0.06)
        self.assertEqual(
            self.generator._collect_challengers(self.conn)["paper_candidates"],
            1)

    def test_the_listing_shape_matches_what_the_view_indexes(self):
        """
        The view reads this tuple positionally. A column inserted in
        the middle silently shifts every field after it — the failure
        that made Phase 19 print "unknown" for every direction cohort.
        """
        a_challenger(self.conn, "chl-1", decision="superior", effect=0.06)
        row = self.generator._collect_challengers(self.conn)["listing"][0]
        self.assertEqual(len(row), 36)
        self.assertEqual(row[0], "chl-1")
        self.assertEqual(row[15], "superior")


class TestTheLabIsHonest(unittest.TestCase):

    def setUp(self):
        self.conn = a_database()
        a_challenger(self.conn, "chl-1", decision="requires_review",
                     effect=0.06)
        self.html = DashboardGenerator().generate_report(conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_page_carries_no_overall_score(self):
        """§37: a sortable score gets sorted."""
        collected = DashboardGenerator()._collect_challengers(self.conn)
        for key in ("overall", "total_score", "score", "rank"):
            self.assertNotIn(key, collected)

    def test_the_page_states_that_research_is_not_approval(self):
        self.assertIn("nu o aprobare de productie", self.html)

    def test_the_page_makes_no_network_calls(self):
        for word in ("fetch(", "XMLHttpRequest", "WebSocket"):
            self.assertNotIn(word, self.html)

    def test_the_page_offers_a_command_rather_than_a_run_button(self):
        self.assertIn("scripts/run_challenger.py", self.html)


if __name__ == "__main__":
    unittest.main()
