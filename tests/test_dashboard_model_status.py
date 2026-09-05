"""
tests/test_dashboard_model_status.py
--------------------------------------------
Phase 18 §11/§12: an experimental signal must not look validated.

THE PROBLEM
---------------
The model page already said "nu bate baseline" in red. The signals
page said nothing at all, so a reader saw

    Apple  SHORT  -0.56%

with no way to learn that the model behind it had a negative
r-squared and a directional accuracy of 0.413.

WHAT THESE DEFEND
---------------------
1. Each signal carries the status of the model that produced it, at
   index 10, APPENDED — the page indexes this tuple positionally.
2. The status is DERIVED from `trained_models.status` at read time,
   not stored on the signal. A demoted model must retroactively make
   its old signals experimental, because the question a reader asks is
   "is this approved now".
3. A signal whose model is unknown is treated as NOT validated. Absence
   of evidence is not evidence of approval.
4. The lookup cannot delete the signals it annotates — the same
   regression the label lookup caused in Phase 17.5.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.dashboard import DashboardGenerator

IDX_SIGNAL_ID = 0
IDX_INSTRUMENT_ID = 1
IDX_NAME = 8
IDX_TICKER = 9
IDX_MODEL_STATUS = 10


class ModelStatusCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE signals (signal_id TEXT PRIMARY KEY, instrument_id TEXT,
                direction TEXT, status TEXT, strength REAL, confidence REAL,
                expected_return REAL, source_information_cutoff TEXT);
            CREATE TABLE signal_contributions (signal_id TEXT, prediction_id TEXT,
                trained_model_id TEXT, model_qualified_id TEXT, predicted_value REAL,
                probability_up REAL, confidence REAL, weight REAL, reliability REAL,
                is_abstention INTEGER, note TEXT);
            CREATE TABLE trained_models (trained_model_id TEXT PRIMARY KEY,
                model_qualified_id TEXT, label_name TEXT, status TEXT,
                trained_at TEXT, train_sample_size INTEGER, train_cluster_count INTEGER,
                dataset_version TEXT, feature_set_version TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def add_signal(self, signal_id="sig-1", instrument_id="us_and_intl-aapl",
                   cutoff="2026-09-03T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO signals VALUES (?,?, 'short','active',0.5,0.3,-0.01,?)",
            (signal_id, instrument_id, cutoff))
        self.conn.commit()

    def add_model(self, trained_model_id="tm-1", status="evaluated"):
        self.conn.execute(
            "INSERT INTO trained_models VALUES (?,?,'d5.abnormal_return',?,"
            "'2026-09-04T00:00:00+00:00',600,116,'ds-1','v1')",
            (trained_model_id, f"ridge:{trained_model_id}", status))
        self.conn.commit()

    def link(self, signal_id="sig-1", trained_model_id="tm-1"):
        self.conn.execute(
            "INSERT INTO signal_contributions VALUES (?,'p-1',?,'q',0.1,NULL,"
            "NULL,1.0,NULL,0,'')", (signal_id, trained_model_id))
        self.conn.commit()

    def recent(self):
        generator = DashboardGenerator.__new__(DashboardGenerator)
        return generator._collect_signals(self.conn)["recent"]


class TestTheStatusTravelsWithTheSignal(ModelStatusCase):

    def test_a_signal_from_an_unpromoted_model_says_evaluated(self):
        self.add_signal()
        self.add_model(status="evaluated")
        self.link()
        self.assertEqual(self.recent()[0][IDX_MODEL_STATUS], "evaluated")

    def test_a_signal_from_a_promoted_model_says_active(self):
        self.add_signal()
        self.add_model(status="active")
        self.link()
        self.assertEqual(self.recent()[0][IDX_MODEL_STATUS], "active")

    def test_a_signal_with_no_known_model_is_not_called_validated(self):
        """
        Absence of evidence is not evidence of approval. The page treats
        anything that is not exactly 'active' as experimental, so None
        lands on the safe side.
        """
        self.add_signal()
        self.assertIsNone(self.recent()[0][IDX_MODEL_STATUS])

    def test_demoting_a_model_retroactively_marks_its_signals(self):
        """
        The reason this is derived rather than stored. A signal must not
        keep claiming validated provenance after its model was pulled.
        """
        self.add_signal()
        self.add_model(status="active")
        self.link()
        self.assertEqual(self.recent()[0][IDX_MODEL_STATUS], "active")
        self.conn.execute("UPDATE trained_models SET status='degraded'")
        self.conn.commit()
        self.assertEqual(self.recent()[0][IDX_MODEL_STATUS], "degraded")

    def test_one_unpromoted_contributor_makes_the_whole_signal_experimental(self):
        """A signal is only as validated as its weakest input."""
        self.add_signal()
        self.add_model("tm-active", status="active")
        self.add_model("tm-experimental", status="evaluated")
        self.link("sig-1", "tm-active")
        self.link("sig-1", "tm-experimental")
        self.assertNotEqual(self.recent()[0][IDX_MODEL_STATUS], "active")


class TestTheStatusCannotDeleteTheSignal(ModelStatusCase):
    """
    Phase 17.5 recorded a regression where a label lookup joined into
    the signals query made the registry a hard dependency, and `_rows`
    swallowed the missing table into [] — so every signal vanished.
    This annotation must not repeat it.
    """

    def test_signals_survive_a_missing_model_registry(self):
        self.conn.execute("DROP TABLE trained_models")
        self.add_signal()
        rows = self.recent()
        self.assertEqual(len(rows), 1, "the signal vanished with the model table")
        self.assertIsNone(rows[0][IDX_MODEL_STATUS])

    def test_signals_survive_a_missing_contributions_table(self):
        self.conn.execute("DROP TABLE signal_contributions")
        self.add_signal()
        self.assertEqual(len(self.recent()), 1)

    def test_the_tuple_keeps_its_width_without_either_table(self):
        self.conn.execute("DROP TABLE signal_contributions")
        self.conn.execute("DROP TABLE trained_models")
        self.add_signal()
        self.assertEqual(len(self.recent()[0]), 11)


class TestThePageSaysSo(unittest.TestCase):
    """
    The rendered page, checked as text. These are the honesty
    requirements (§12) and they are worth pinning because they are easy
    to delete by accident while restyling.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "src", "dashboard.py")
        with open(path, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_there_is_one_definition_of_a_model_status_label(self):
        self.assertEqual(self.source.count("var MODEL_STATUS_LABEL"), 1)

    def test_experimental_is_a_named_state_not_a_guess(self):
        self.assertIn('evaluated: ["EXPERIMENTAL"', self.source)
        self.assertIn('active:    ["VALIDAT"', self.source)

    def test_anything_not_active_counts_as_experimental(self):
        """
        The predicate is written as != 'active', not == 'evaluated', so
        a status nobody anticipated fails safe.
        """
        self.assertIn('!== "active"', self.source)

    def test_the_signal_detail_panel_carries_an_explicit_warning(self):
        self.assertIn("nu a fost promovat", self.source)
        self.assertIn("rezultat de cercetare", self.source)

    def test_the_models_page_states_whether_anything_is_validated(self):
        self.assertIn("Niciun model validat", self.source)

    def test_r_squared_and_directional_accuracy_reach_the_card(self):
        self.assertIn("metrics.r_squared", self.source)
        self.assertIn("metrics.directional_accuracy", self.source)

    def test_a_missing_baseline_is_shown_as_unjudgeable_not_as_a_pass(self):
        self.assertIn("nejudecabil", self.source)


if __name__ == "__main__":
    unittest.main()
