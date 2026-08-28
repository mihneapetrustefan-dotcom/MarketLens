"""
tests/scripts/test_build_research_observations.py
-----------------------------------------------------------
Tests for scripts/build_research_observations.py.

The one thing this script must never get wrong is the feature/label
time split — a feature timestamped after the cutoff, or a label at or
before it, is exactly the leakage the whole phase exists to prevent.
That gets its own dedicated tests, not just an end-to-end smoke test.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.build_research_observations import (
    observation_id_for, build_observation, persist, main,
)
from src.data_access.schema import initialize_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.impact_schema import initialize_impact_schema
from src.data_access.research_schema import initialize_research_schema
from src.impact.engine import EventStudyEngine
from scripts.build_event_studies import main as build_studies_main

ANCHOR = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def seed_full_db(path, days_before=40, days_after=10):
    conn = sqlite3.connect(path)
    initialize_schema(conn)
    initialize_fusion_schema(conn)
    initialize_price_cache_schema(conn)
    initialize_impact_schema(conn)
    initialize_research_schema(conn)

    conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id,name,country) VALUES ('US_AND_INTL','X','US')")
    conn.execute("INSERT OR IGNORE INTO companies (company_id, canonical_name) VALUES ('nvidia','NVIDIA')")
    conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, instrument_type) "
                 "VALUES ('nvidia-common','nvidia','common_stock')")
    conn.execute("INSERT OR IGNORE INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) "
                 "VALUES ('inst-nvda','nvidia-common','US_AND_INTL','NVDA','stock')")
    conn.execute("""INSERT INTO canonical_events
        (canonical_event_id, event_type, category, lifecycle_state, corroboration_state,
         first_reported_at, independent_source_count, quality_confidence)
        VALUES ('ce-1','acquisition','corporate_action','reported','single_source',?,2,0.85)""",
        (ANCHOR.isoformat(),))
    conn.execute("INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) "
                 "VALUES ('ce-1','nvidia','primary')")

    def seed_daily(instrument_id, base_price):
        for i in range(-days_before, days_after + 1):
            ts = (ANCHOR + timedelta(days=i)).replace(hour=20, minute=0)
            price = base_price + i * 0.1
            conn.execute("""INSERT INTO price_candle_cache
                (instrument_id, interval, timestamp, open, high, low, close, adjusted_close, volume, source, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument_id, "1d", ts.isoformat(), price, price + 1, price - 1, price, price,
                 1_000_000, "test", "now"))

    seed_daily("inst-nvda", 100.0)
    seed_daily("benchmark-spy", 500.0)
    conn.commit()
    conn.close()


class TestObservationIdFor(unittest.TestCase):
    def test_stable_across_calls(self):
        self.assertEqual(observation_id_for("ce-1", "inst-nvda"), observation_id_for("ce-1", "inst-nvda"))

    def test_different_instrument_differs(self):
        self.assertNotEqual(observation_id_for("ce-1", "inst-nvda"), observation_id_for("ce-1", "inst-msft"))


class TestFeatureLabelSplitEndToEnd(unittest.TestCase):
    """The core correctness property: every feature predates the
    cutoff, every label postdates it."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed_full_db(self.db_path)
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path, "--apply"]
        try:
            build_studies_main()
        finally:
            sys.argv = argv

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, extra):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path] + extra
        try:
            return main()
        finally:
            sys.argv = argv

    def test_dry_run_writes_nothing(self):
        self._run([])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM research_observations").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_apply_produces_high_quality_observation(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        level = conn.execute("SELECT quality_level FROM research_observations").fetchone()[0]
        conn.close()
        self.assertEqual(level, "high")

    def test_all_features_predate_or_equal_cutoff(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        cutoff = conn.execute("SELECT information_cutoff FROM research_observations").fetchone()[0]
        cutoff_dt = datetime.fromisoformat(cutoff)
        rows = conn.execute("SELECT as_of FROM research_features WHERE as_of IS NOT NULL").fetchall()
        conn.close()
        self.assertTrue(len(rows) > 0)
        for (as_of,) in rows:
            self.assertLessEqual(datetime.fromisoformat(as_of), cutoff_dt)

    def test_all_labels_strictly_postdate_cutoff(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        cutoff = conn.execute("SELECT information_cutoff FROM research_observations").fetchone()[0]
        cutoff_dt = datetime.fromisoformat(cutoff)
        rows = conn.execute("SELECT measured_at FROM research_labels").fetchall()
        conn.close()
        self.assertTrue(len(rows) > 0)
        for (measured_at,) in rows:
            self.assertGreater(datetime.fromisoformat(measured_at), cutoff_dt)

    def test_built_observation_passes_its_own_validate(self):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT event_id, instrument_id, benchmark_id, event_time, publication_time, "
            "market_visibility_latest, quality_level, quality_issues_json FROM event_studies"
        ).fetchone()
        obs = build_observation(conn, EventStudyEngine(), row, {})
        conn.close()
        self.assertEqual(obs.validate(), [])

    def test_rerunning_does_not_duplicate(self):
        self._run(["--apply"])
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        obs_count = conn.execute("SELECT COUNT(*) FROM research_observations").fetchone()[0]
        feat_count = conn.execute("SELECT COUNT(*) FROM research_features").fetchone()[0]
        conn.close()
        self.assertEqual(obs_count, 1)
        self.assertGreater(feat_count, 0)

    def test_event_cluster_id_is_the_instrument(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        cluster_id = conn.execute("SELECT event_cluster_id FROM research_observations").fetchone()[0]
        conn.close()
        self.assertEqual(cluster_id, "inst-nvda")

    def test_contemporaneous_event_attributes_are_flagged(self):
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT is_contemporaneous FROM research_features WHERE qualified_name = 'event.event_type'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)


class TestUnusableStudyStillPersists(unittest.TestCase):
    """Spec §20: bad observations are marked and kept, never dropped."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed_full_db(self.db_path, days_before=5)  # below min_baseline -> UNUSABLE
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path, "--apply"]
        try:
            build_studies_main()
        finally:
            sys.argv = argv

    def tearDown(self):
        os.remove(self.db_path)

    def test_unusable_study_yields_invalid_observation_not_a_missing_one(self):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path, "--apply"]
        try:
            main()
        finally:
            sys.argv = argv
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT quality_level, exclusions_json FROM research_observations").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "invalid")
        self.assertIn("insufficient_price_data", row[1])


if __name__ == "__main__":
    unittest.main()
