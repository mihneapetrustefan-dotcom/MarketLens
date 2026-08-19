"""
test_recommendation_log.py
------------------------------
Unit tests for Recommendation Log v1.1 (time_horizon storage + safe
schema migration + per-horizon backtest readiness).
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from recommendation_log import RecommendationLog


def make_recommendation(entity="Tesla", recommendation="BUY", confidence_score=0.7, time_horizon="short-term"):
    return {"entity": entity, "recommendation": recommendation, "confidence_score": confidence_score, "time_horizon": time_horizon}


class TestLogRecommendations(unittest.TestCase):
    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_logged_recommendation_is_retrievable(self):
        self.log.log_recommendations([make_recommendation(entity="Tesla")])
        rows = self.log.load_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity"], "Tesla")

    def test_time_horizon_is_persisted(self):
        self.log.log_recommendations([make_recommendation(time_horizon="long-term")])
        rows = self.log.load_all()
        self.assertEqual(rows[0]["time_horizon"], "long-term")

    def test_missing_time_horizon_is_stored_as_null(self):
        rec = make_recommendation()
        del rec["time_horizon"]
        self.log.log_recommendations([rec])
        rows = self.log.load_all()
        self.assertIsNone(rows[0]["time_horizon"])

    def test_ticker_lookup_resolves_ticker(self):
        self.log.log_recommendations([make_recommendation(entity="Tesla")], ticker_lookup={"Tesla": "TSLA"})
        rows = self.log.load_all()
        self.assertEqual(rows[0]["ticker"], "TSLA")

    def test_entity_missing_from_lookup_gets_null_ticker(self):
        self.log.log_recommendations([make_recommendation(entity="Unknown Co")], ticker_lookup={"Tesla": "TSLA"})
        rows = self.log.load_all()
        self.assertIsNone(rows[0]["ticker"])


class TestSchemaMigration(unittest.TestCase):
    """Confirms an EXISTING database (created before time_horizon existed) is safely migrated, not wiped."""

    def test_pre_existing_table_without_time_horizon_gets_migrated(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "old.db")

            # Simulate a database created by the OLD version of this module (no time_horizon column).
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT NOT NULL,
                    ticker TEXT,
                    recommendation TEXT NOT NULL,
                    confidence_score REAL,
                    generated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, generated_at) VALUES (?, ?, ?, ?, ?)",
                ("Nvidia", "NVDA", "BUY", 0.87, "2026-08-01T09:00:00+00:00"),
            )
            conn.commit()
            conn.close()

            # Opening it with the NEW module must not raise, must not lose the old row.
            log = RecommendationLog(db_path)
            rows = log.load_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["entity"], "Nvidia")
            self.assertIsNone(rows[0]["time_horizon"])  # old row has no horizon recorded
            log.close()

    def test_migration_is_idempotent_across_repeated_opens(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test.db")
            log1 = RecommendationLog(db_path)
            log1.log_recommendations([make_recommendation()])
            log1.close()

            # Re-opening (as a fresh daily run would) must not raise
            # "duplicate column" or any other migration error.
            log2 = RecommendationLog(db_path)
            rows = log2.load_all()
            self.assertEqual(len(rows), 1)
            log2.close()


class TestLoadActionableDueForCheck(unittest.TestCase):
    """The core bug-fix behavior: readiness now depends on EACH row's own time_horizon."""

    def setUp(self):
        self.log = RecommendationLog(":memory:")
        self.horizon_days = {"short-term": 5, "mixed": 15, "long-term": 45}

    def tearDown(self):
        self.log.close()

    def _insert_raw(self, entity, recommendation, time_horizon, days_ago):
        generated_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, time_horizon, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entity, "TCK", recommendation, 0.7, time_horizon, generated_at),
        )
        self.log._conn.commit()

    def test_long_term_5_days_old_is_not_yet_due(self):
        self._insert_raw("Nvidia", "BUY", "long-term", days_ago=5)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(due, [])

    def test_long_term_46_days_old_is_due(self):
        self._insert_raw("Nvidia", "BUY", "long-term", days_ago=46)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["entity"], "Nvidia")

    def test_short_term_6_days_old_is_due(self):
        self._insert_raw("Tesla", "BUY", "short-term", days_ago=6)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(len(due), 1)

    def test_mixed_5_days_old_is_not_yet_due(self):
        self._insert_raw("Apple", "BUY", "mixed", days_ago=5)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(due, [])

    def test_null_horizon_uses_default_days(self):
        self._insert_raw("Legacy Co", "BUY", None, days_ago=6)
        due = self.log.load_actionable_due_for_check(self.horizon_days, default_holding_period_days=5)
        self.assertEqual(len(due), 1)

    def test_hold_recommendations_never_included(self):
        self._insert_raw("Microsoft", "HOLD", "short-term", days_ago=100)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(due, [])

    def test_mixed_batch_only_returns_due_entries(self):
        self._insert_raw("Nvidia", "BUY", "long-term", days_ago=5)     # not due
        self._insert_raw("Tesla", "BUY", "short-term", days_ago=6)      # due
        self._insert_raw("Apple", "SELL", "long-term", days_ago=50)     # due
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        due_entities = {r["entity"] for r in due}
        self.assertEqual(due_entities, {"Tesla", "Apple"})

    def test_strong_buy_included_in_due_for_check(self):
        self._insert_raw("Nvidia", "STRONG_BUY", "short-term", days_ago=6)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(len(due), 1)

    def test_strong_sell_included_in_due_for_check(self):
        self._insert_raw("Coinbase", "STRONG_SELL", "short-term", days_ago=6)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(len(due), 1)

    def test_already_checked_row_is_never_returned_again(self):
        self._insert_raw("Nvidia", "BUY", "short-term", days_ago=6)
        due = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(len(due), 1)

        self.log.mark_checked(due[0]["id"], True)

        due_again = self.log.load_actionable_due_for_check(self.horizon_days)
        self.assertEqual(due_again, [])


class TestMarkCheckedAndLatestOutcome(unittest.TestCase):
    """
    The core fix for the real-world 'inconsistent verified badge'
    problem: each row is checked exactly once, and the Dashboard's
    badge reflects the entity's MOST RECENT checked row specifically —
    not whichever historical row happened to come due that day.
    """

    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def _insert_raw(self, entity, recommendation, generated_at_iso, time_horizon="short-term"):
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, time_horizon, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entity, "TCK", recommendation, 0.7, time_horizon, generated_at_iso),
        )
        self.log._conn.commit()
        return self.log._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_mark_checked_persists_correct_outcome(self):
        row_id = self._insert_raw("Nvidia", "BUY", "2026-08-01T09:00:00+00:00")
        self.log.mark_checked(row_id, True)
        row = dict(self.log._conn.execute("SELECT * FROM recommendations WHERE id = ?", (row_id,)).fetchone())
        self.assertEqual(row["was_correct"], 1)
        self.assertIsNotNone(row["checked_at"])

    def test_mark_checked_with_none_leaves_was_correct_null_but_sets_checked_at(self):
        row_id = self._insert_raw("Nvidia", "BUY", "2026-08-01T09:00:00+00:00")
        self.log.mark_checked(row_id, None)
        row = dict(self.log._conn.execute("SELECT * FROM recommendations WHERE id = ?", (row_id,)).fetchone())
        self.assertIsNone(row["was_correct"])
        self.assertIsNotNone(row["checked_at"])  # still marked, so it's not retried forever

    def test_latest_outcome_reflects_most_recently_generated_checked_row(self):
        old_id = self._insert_raw("Nvidia", "BUY", "2026-07-01T09:00:00+00:00")
        new_id = self._insert_raw("Nvidia", "BUY", "2026-08-01T09:00:00+00:00")
        self.log.mark_checked(old_id, False)   # older call: was wrong
        self.log.mark_checked(new_id, True)    # newer call: was right

        latest = self.log.load_latest_verified_outcome_per_entity()
        self.assertTrue(latest["Nvidia"])  # reflects the NEWER row, not the older one

    def test_unchecked_entity_absent_from_latest_outcome_dict(self):
        self._insert_raw("Tesla", "BUY", "2026-08-01T09:00:00+00:00")  # never marked checked
        latest = self.log.load_latest_verified_outcome_per_entity()
        self.assertNotIn("Tesla", latest)

    def test_skipped_row_never_counted_as_an_outcome(self):
        row_id = self._insert_raw("Tesla", "BUY", "2026-08-01T09:00:00+00:00")
        self.log.mark_checked(row_id, None)  # skipped, not a real outcome
        latest = self.log.load_latest_verified_outcome_per_entity()
        self.assertNotIn("Tesla", latest)

    def test_multiple_entities_tracked_independently(self):
        nvda_id = self._insert_raw("Nvidia", "BUY", "2026-08-01T09:00:00+00:00")
        tsla_id = self._insert_raw("Tesla", "SELL", "2026-08-01T09:00:00+00:00")
        self.log.mark_checked(nvda_id, True)
        self.log.mark_checked(tsla_id, False)

        latest = self.log.load_latest_verified_outcome_per_entity()
        self.assertTrue(latest["Nvidia"])
        self.assertFalse(latest["Tesla"])


class TestAccuracyHistory(unittest.TestCase):
    """Tests for the v1.6 cumulative hit-rate-over-time tracking."""

    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def _insert_and_check(self, entity, was_correct, checked_at):
        row_id = self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, time_horizon, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entity, "TCK", "BUY", 0.7, "short-term", "2026-07-01T09:00:00+00:00"),
        )
        self.log._conn.commit()
        row_id = self.log._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.log.mark_checked(row_id, was_correct, checked_at=checked_at)

    def test_empty_history_returns_empty_list(self):
        self.assertEqual(self.log.load_accuracy_history(), [])

    def test_cumulative_rate_after_one_correct_check(self):
        self._insert_and_check("Nvidia", True, "2026-08-01T09:00:00+00:00")
        history = self.log.load_accuracy_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["cumulative_hit_rate"], 1.0)
        self.assertEqual(history[0]["cumulative_checked"], 1)

    def test_cumulative_rate_updates_correctly_across_checks(self):
        self._insert_and_check("Nvidia", True, "2026-08-01T09:00:00+00:00")
        self._insert_and_check("Tesla", False, "2026-08-02T09:00:00+00:00")
        self._insert_and_check("Apple", True, "2026-08-03T09:00:00+00:00")

        history = self.log.load_accuracy_history()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["cumulative_hit_rate"], 1.0)       # 1/1
        self.assertEqual(history[1]["cumulative_hit_rate"], 0.5)       # 1/2
        self.assertAlmostEqual(history[2]["cumulative_hit_rate"], 0.667, places=2)  # 2/3

    def test_history_is_chronologically_ordered(self):
        self._insert_and_check("C", True, "2026-08-03T09:00:00+00:00")
        self._insert_and_check("A", True, "2026-08-01T09:00:00+00:00")
        self._insert_and_check("B", True, "2026-08-02T09:00:00+00:00")

        history = self.log.load_accuracy_history()
        dates = [h["checked_at"] for h in history]
        self.assertEqual(dates, sorted(dates))

    def test_skipped_rows_excluded_from_history(self):
        row_id = self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, time_horizon, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("Legacy Co", "TCK", "BUY", 0.7, "short-term", "2026-07-01T09:00:00+00:00"),
        )
        self.log._conn.commit()
        row_id = self.log._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.log.mark_checked(row_id, None)  # skipped, not a real outcome

        self.assertEqual(self.log.load_accuracy_history(), [])


class TestAccuracyHistoryDaily(unittest.TestCase):
    """Tests for the day-bucketed variant used for charting."""

    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def _insert_and_check(self, entity, was_correct, checked_at):
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, time_horizon, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (entity, "TCK", "BUY", 0.7, "short-term", "2026-07-01T09:00:00+00:00"),
        )
        self.log._conn.commit()
        row_id = self.log._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.log.mark_checked(row_id, was_correct, checked_at=checked_at)

    def test_multiple_checks_same_day_collapse_to_one_point(self):
        self._insert_and_check("Nvidia", True, "2026-08-01T09:00:00+00:00")
        self._insert_and_check("Tesla", False, "2026-08-01T15:00:00+00:00")
        self._insert_and_check("Apple", True, "2026-08-01T20:00:00+00:00")

        daily = self.log.load_accuracy_history_daily()
        self.assertEqual(len(daily), 1)  # all 3 same calendar day -> 1 point
        self.assertEqual(daily[0]["cumulative_checked"], 3)  # reflects the LAST check that day

    def test_checks_across_different_days_produce_separate_points(self):
        self._insert_and_check("Nvidia", True, "2026-08-01T09:00:00+00:00")
        self._insert_and_check("Tesla", False, "2026-08-02T09:00:00+00:00")

        daily = self.log.load_accuracy_history_daily()
        self.assertEqual(len(daily), 2)

    def test_empty_history_returns_empty_list(self):
        self.assertEqual(self.log.load_accuracy_history_daily(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
