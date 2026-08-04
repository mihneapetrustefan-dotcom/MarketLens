"""
test_portfolio_history.py
-----------------------------
Unit tests for Portfolio History v1 (portfolio_history.py).

TESTING STRATEGY: every test uses an in-memory SQLite database
(db_path=":memory:") — fast, isolated, no files touch disk. Same
pattern used throughout the project's other SQLite-backed modules.
"""

import unittest

from portfolio_history import PortfolioHistory


def make_portfolio_result(total_invested=1000.0, total_final_value=1100.0,
                           total_return_pct=10.0, trades_simulated=1):
    return {
        "total_invested": total_invested, "total_final_value": total_final_value,
        "total_return_pct": total_return_pct, "trades_simulated": trades_simulated,
    }


class TestLogSnapshot(unittest.TestCase):
    def setUp(self):
        self.history = PortfolioHistory(":memory:")

    def tearDown(self):
        self.history.close()

    def test_logged_snapshot_is_retrievable(self):
        self.history.log_snapshot(make_portfolio_result(), recorded_at="2026-08-01T09:00:00+00:00")
        snapshots = self.history.load_all()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["total_return_pct"], 10.0)

    def test_zero_trades_snapshot_is_still_logged(self):
        empty_result = {"total_invested": 0.0, "total_final_value": 0.0,
                         "total_return_pct": None, "trades_simulated": 0}
        self.history.log_snapshot(empty_result, recorded_at="2026-08-01T09:00:00+00:00")
        snapshots = self.history.load_all()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["trades_simulated"], 0)

    def test_default_recorded_at_is_used_when_not_given(self):
        self.history.log_snapshot(make_portfolio_result())
        snapshots = self.history.load_all()
        self.assertIsNotNone(snapshots[0]["recorded_at"])


class TestLoadAll(unittest.TestCase):
    def setUp(self):
        self.history = PortfolioHistory(":memory:")

    def tearDown(self):
        self.history.close()

    def test_returns_snapshots_ordered_oldest_first(self):
        self.history.log_snapshot(make_portfolio_result(total_return_pct=5.0), recorded_at="2026-08-02T09:00:00+00:00")
        self.history.log_snapshot(make_portfolio_result(total_return_pct=1.0), recorded_at="2026-08-01T09:00:00+00:00")
        snapshots = self.history.load_all()
        self.assertEqual([s["total_return_pct"] for s in snapshots], [1.0, 5.0])

    def test_empty_history_returns_empty_list(self):
        self.assertEqual(self.history.load_all(), [])

    def test_multiple_snapshots_all_persisted(self):
        for i in range(5):
            self.history.log_snapshot(make_portfolio_result(total_return_pct=float(i)), recorded_at=f"2026-08-0{i+1}T09:00:00+00:00")
        self.assertEqual(len(self.history.load_all()), 5)


class TestPersistenceAcrossConnections(unittest.TestCase):
    def test_snapshots_persist_in_a_real_file(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test.db")

            history1 = PortfolioHistory(db_path)
            history1.log_snapshot(make_portfolio_result(total_return_pct=7.5), recorded_at="2026-08-01T09:00:00+00:00")
            history1.close()

            history2 = PortfolioHistory(db_path)
            snapshots = history2.load_all()
            history2.close()

            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["total_return_pct"], 7.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
