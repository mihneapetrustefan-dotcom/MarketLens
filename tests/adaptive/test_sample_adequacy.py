"""
tests/adaptive/test_sample_adequacy.py
--------------------------------------------------
Phase 25.9I-F.6: rows are not evidence, and the gate does not move.

The property everything else rests on is the first one tested:
duplicating a cluster cannot raise the effective sample. If that ever
became false, every qualification decision downstream would be built on
a number that can be inflated by copying rows.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.sample_adequacy import (
    MIN_EFFECTIVE_SAMPLE, MIN_INTRADAY_SESSIONS, MIN_VALID_FOLDS,
    ResearchReadiness, SampleAdequacy, assess_horizon, effective_sample_of,
)
from src.domain.model_models import ModelEvaluation

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")
UTC = timezone.utc
HOLDOUT = datetime(2026, 8, 15, 1, 23, 31, tzinfo=UTC)


def adequacy(**kw) -> SampleAdequacy:
    base = dict(horizon_id="d5", raw_rows=100, matured_labels=100,
                unique_instruments=50, unique_clusters=50,
                unique_sessions=20, unique_dates=20,
                research_region_rows=80, research_region_clusters=40,
                holdout_rows=20, holdout_clusters=10)
    base.update(kw)
    return SampleAdequacy(**base)


class TestEffectiveSampleIsClusters(unittest.TestCase):
    """Tests 1-4 -- the property everything else depends on."""

    def test_duplicating_a_cluster_does_not_increase_the_sample(self):
        one = ["inst-a", "inst-b", "inst-c"]
        many = ["inst-a"] * 50 + ["inst-b"] * 50 + ["inst-c"] * 50
        self.assertEqual(effective_sample_of(one), 3)
        self.assertEqual(effective_sample_of(many), 3)

    def test_a_genuinely_new_cluster_does_increase_it(self):
        self.assertEqual(effective_sample_of(["a", "b"]), 2)
        self.assertEqual(effective_sample_of(["a", "b", "c"]), 3)

    def test_blank_and_none_clusters_are_not_counted(self):
        self.assertEqual(effective_sample_of(["a", "", None, "a"]), 1)

    def test_raw_rows_cannot_override_a_low_effective_sample(self):
        thin = adequacy(raw_rows=5000, unique_clusters=4,
                        research_region_clusters=4)
        self.assertEqual(thin.effective_sample, 4)
        self.assertFalse(thin.meets_effective_floor)
        self.assertTrue(thin.rows_sufficient_but_sample_is_not)

    def test_the_row_inflation_ratio_exposes_a_narrow_corpus(self):
        thin = adequacy(raw_rows=5000, unique_clusters=4)
        self.assertEqual(thin.row_inflation_ratio, 1250.0)

    def test_more_clusters_than_rows_is_refused(self):
        with self.assertRaises(ValueError):
            adequacy(raw_rows=10, unique_clusters=20)

    def test_the_shortfall_is_reported_in_clusters(self):
        thin = adequacy(unique_clusters=4)
        self.assertEqual(thin.clusters_short_of_floor,
                         MIN_EFFECTIVE_SAMPLE - 4)


class TestGatesNotWeakened(unittest.TestCase):
    """Tests 6-7 -- the floor is the canonical one and it does not move."""

    def test_the_floor_is_the_canonical_constant(self):
        self.assertIs(MIN_EFFECTIVE_SAMPLE,
                      ModelEvaluation.MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(MIN_EFFECTIVE_SAMPLE, 30)

    def test_no_second_floor_is_defined_in_this_module(self):
        path = os.path.join(ROOT, "src", "adaptive", "sample_adequacy.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("ModelEvaluation.MIN_EFFECTIVE_SAMPLE", source)
        self.assertNotIn("MIN_EFFECTIVE_SAMPLE = 30", source)

    def test_the_fold_minimum_is_three(self):
        self.assertEqual(MIN_VALID_FOLDS, 3)

    def test_insufficient_effective_sample_blocks_research(self):
        thin = assess_readiness(adequacy(unique_clusters=4,
                                         research_region_clusters=4))
        self.assertIs(thin, ResearchReadiness.INSUFFICIENT_EFFECTIVE_SAMPLE)

    def test_a_full_holdout_cannot_rescue_a_thin_research_region(self):
        """The holdout is not borrowable to reach the floor."""
        lopsided = adequacy(unique_clusters=100, research_region_clusters=5,
                            holdout_clusters=95)
        self.assertTrue(lopsided.meets_effective_floor)
        self.assertFalse(lopsided.research_region_meets_floor)
        self.assertIs(assess_readiness(lopsided),
                      ResearchReadiness.INSUFFICIENT_EFFECTIVE_SAMPLE)


def assess_readiness(item: SampleAdequacy) -> ResearchReadiness:
    from src.adaptive.sample_adequacy import _judge
    return _judge(item)[0]


class TestIntradaySessionIndependence(unittest.TestCase):
    """Section 15 -- many bars from few days are not many days."""

    def test_few_sessions_defer_an_intraday_horizon(self):
        busy = adequacy(horizon_id="intraday_5m", is_intraday=True,
                        raw_rows=500000, unique_clusters=200,
                        research_region_clusters=150,
                        unique_sessions=12, unique_dates=12)
        self.assertIs(assess_readiness(busy),
                      ResearchReadiness.DATA_ACCUMULATION_REQUIRED)

    def test_enough_sessions_make_it_researchable(self):
        spread = adequacy(horizon_id="intraday_5m", is_intraday=True,
                          unique_sessions=MIN_INTRADAY_SESSIONS + 5,
                          unique_dates=MIN_INTRADAY_SESSIONS + 5)
        self.assertIs(assess_readiness(spread), ResearchReadiness.RESEARCH_READY)

    def test_a_daily_horizon_is_not_session_gated(self):
        daily = adequacy(horizon_id="d5", is_intraday=False, unique_sessions=5)
        self.assertIs(assess_readiness(daily), ResearchReadiness.RESEARCH_READY)

    def test_research_ready_promises_a_fair_test_not_an_edge(self):
        ready = adequacy()
        self.assertTrue(assess_readiness(ready).is_researchable)
        _, reasons = _judge_reasons(ready)
        self.assertTrue(any("says nothing about whether an edge exists" in r
                            for r in reasons))


def _judge_reasons(item):
    from src.adaptive.sample_adequacy import _judge
    return _judge(item)


@unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
class TestRealCorpus(unittest.TestCase):
    """The measured state of the repository, asserted."""

    @classmethod
    def setUpClass(cls):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            cls.by_horizon = {
                horizon: assess_horizon(conn, horizon, holdout_start=HOLDOUT)
                for horizon in ("d1", "d3", "d5", "d10", "intraday_5m", "d20")}
        finally:
            conn.close()

    def test_a_cluster_is_an_instrument(self):
        """`event_cluster_id` equals `instrument_id` for every row."""
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            mismatches = conn.execute(
                "SELECT COUNT(*) FROM research_observations "
                "WHERE event_cluster_id IS NOT NULL "
                "AND event_cluster_id != instrument_id").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mismatches, 0)

    def test_the_daily_horizons_clear_the_sample_floor(self):
        """Refutes the F.5 reading that the sample was the constraint."""
        for horizon in ("d1", "d3", "d5", "d10"):
            item = self.by_horizon[horizon]
            self.assertGreater(item.effective_sample, MIN_EFFECTIVE_SAMPLE,
                               f"{horizon} clusters")
            self.assertTrue(item.research_region_meets_floor, horizon)

    def test_the_daily_horizons_are_research_ready(self):
        for horizon in ("d1", "d3", "d5", "d10"):
            self.assertIs(self.by_horizon[horizon].readiness,
                          ResearchReadiness.RESEARCH_READY, horizon)

    def test_intraday_is_deferred_on_sessions_not_on_rows(self):
        item = self.by_horizon["intraday_5m"]
        self.assertIs(item.readiness,
                      ResearchReadiness.DATA_ACCUMULATION_REQUIRED)
        self.assertGreater(item.raw_rows, MIN_EFFECTIVE_SAMPLE)
        self.assertLess(item.unique_sessions, MIN_INTRADAY_SESSIONS)

    def test_the_protected_horizon_is_never_assessed_for_research(self):
        item = self.by_horizon["d20"]
        self.assertIs(item.readiness, ResearchReadiness.PROTECTED)
        self.assertEqual(item.raw_rows, 0)
        self.assertEqual(item.unique_clusters, 0)

    def test_adequacy_never_reads_a_label_value(self):
        path = os.path.join(ROOT, "src", "adaptive", "sample_adequacy.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("value_json", source)

    def test_assessment_is_deterministic(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            again = assess_horizon(conn, "d5", holdout_start=HOLDOUT)
        finally:
            conn.close()
        self.assertEqual(again.describe(), self.by_horizon["d5"].describe())

    def test_an_earlier_as_of_cannot_see_later_rows(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            early = assess_horizon(conn, "d5", holdout_start=HOLDOUT,
                                   as_of=datetime(2026, 8, 5, tzinfo=UTC))
            late = assess_horizon(conn, "d5", holdout_start=HOLDOUT)
        finally:
            conn.close()
        self.assertLess(early.raw_rows, late.raw_rows)
        self.assertLessEqual(early.effective_sample, late.effective_sample)


if __name__ == "__main__":
    unittest.main()
