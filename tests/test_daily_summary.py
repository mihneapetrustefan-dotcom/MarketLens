"""
test_daily_summary.py
-------------------------
Unit tests for Daily Summary Generator v1.
"""

import unittest

from daily_summary import DailySummaryGenerator


def make_recommendation(entity, recommendation):
    return {"entity": entity, "recommendation": recommendation}


def make_sector_score(sector, article_count, dominant_sentiment):
    return {"sector": sector, "article_count": article_count, "dominant_sentiment": dominant_sentiment}


def make_change(entity, change):
    return {"entity": entity, "current": "BUY", "previous": "HOLD", "change": change}


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.generator = DailySummaryGenerator()

    def test_includes_buy_and_sell_counts(self):
        recs = [make_recommendation("Tesla", "BUY"), make_recommendation("Bitcoin", "SELL"),
                make_recommendation("Apple", "HOLD")]
        summary = self.generator.generate(recs, [], [])
        self.assertIn("1 BUY", summary)
        self.assertIn("1 SELL", summary)
        self.assertIn("3 tracked entities", summary)

    def test_includes_upgrades_when_present(self):
        changes = [make_change("Tesla", "upgrade")]
        summary = self.generator.generate([], [], changes)
        self.assertIn("Upgraded since last check", summary)
        self.assertIn("Tesla", summary)

    def test_includes_downgrades_when_present(self):
        changes = [make_change("Nvidia", "downgrade")]
        summary = self.generator.generate([], [], changes)
        self.assertIn("Downgraded since last check", summary)
        self.assertIn("Nvidia", summary)

    def test_truncates_long_upgrade_list_with_more_count(self):
        changes = [make_change(f"Company{i}", "upgrade") for i in range(5)]
        summary = self.generator.generate([], [], changes)
        self.assertIn("+2 more", summary)

    def test_includes_top_sector_when_present(self):
        sectors = [make_sector_score("Technology", 42, "positive")]
        summary = self.generator.generate([], sectors, [])
        self.assertIn("Technology", summary)
        self.assertIn("42 articles", summary)

    def test_no_changes_and_all_hold_shows_fallback_sentence(self):
        recs = [make_recommendation("Tesla", "HOLD")]
        summary = self.generator.generate(recs, [], [])
        self.assertIn("No actionable changes today", summary)

    def test_empty_inputs_do_not_crash(self):
        summary = self.generator.generate([], [], [])
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
