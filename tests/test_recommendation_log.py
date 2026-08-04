"""
test_recommendation_log.py
-----------------------------
Unit tests for Recommendation Log v1 (recommendation_log.py).

TESTING STRATEGY: in-memory SQLite (":memory:"), same approach as
test_news_database.py — fast, isolated, no real file touched.
"""

import unittest

from recommendation_log import RecommendationLog


def make_recommendation(**overrides):
    base = {
        "entity": "Tesla",
        "recommendation": "BUY",
        "confidence_score": 0.75,
    }
    base.update(overrides)
    return base


class TestSchemaInitialization(unittest.TestCase):
    def test_table_exists_after_construction(self):
        log = RecommendationLog(":memory:")
        self.assertEqual(log.load_all(), [])
        log.close()


class TestLogRecommendations(unittest.TestCase):
    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_logs_recommendation_with_ticker_from_lookup(self):
        self.log.log_recommendations([make_recommendation(entity="Tesla")], ticker_lookup={"Tesla": "TSLA"})
        stored = self.log.load_all()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["ticker"], "TSLA")

    def test_logs_recommendation_with_null_ticker_when_unknown(self):
        self.log.log_recommendations([make_recommendation(entity="Unknown Corp")], ticker_lookup={"Tesla": "TSLA"})
        stored = self.log.load_all()
        self.assertIsNone(stored[0]["ticker"])

    def test_logs_multiple_recommendations(self):
        recs = [make_recommendation(entity="Tesla"), make_recommendation(entity="Bitcoin", recommendation="SELL")]
        count = self.log.log_recommendations(recs, ticker_lookup={"Tesla": "TSLA", "Bitcoin": "BTC"})
        self.assertEqual(count, 2)
        self.assertEqual(len(self.log.load_all()), 2)

    def test_every_logged_row_has_a_generated_at_timestamp(self):
        self.log.log_recommendations([make_recommendation()])
        stored = self.log.load_all()
        self.assertIsNotNone(stored[0]["generated_at"])

    def test_empty_list_logs_nothing(self):
        count = self.log.log_recommendations([])
        self.assertEqual(count, 0)
        self.assertEqual(self.log.load_all(), [])


class TestLoadActionableBefore(unittest.TestCase):
    def setUp(self):
        self.log = RecommendationLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_excludes_hold_recommendations(self):
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, generated_at) VALUES (?,?,?,?,?)",
            ("Apple", "AAPL", "HOLD", 0.4, "2026-08-01T00:00:00+00:00"),
        )
        self.log._conn.commit()
        result = self.log.load_actionable_before("2026-08-02T00:00:00+00:00")
        self.assertEqual(result, [])

    def test_includes_buy_and_sell_before_cutoff(self):
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, generated_at) VALUES (?,?,?,?,?)",
            ("Tesla", "TSLA", "BUY", 0.7, "2026-08-01T00:00:00+00:00"),
        )
        self.log._conn.commit()
        result = self.log.load_actionable_before("2026-08-02T00:00:00+00:00")
        self.assertEqual(len(result), 1)

    def test_excludes_recommendations_after_cutoff(self):
        self.log._conn.execute(
            "INSERT INTO recommendations (entity, ticker, recommendation, confidence_score, generated_at) VALUES (?,?,?,?,?)",
            ("Tesla", "TSLA", "BUY", 0.7, "2026-08-10T00:00:00+00:00"),
        )
        self.log._conn.commit()
        result = self.log.load_actionable_before("2026-08-02T00:00:00+00:00")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
