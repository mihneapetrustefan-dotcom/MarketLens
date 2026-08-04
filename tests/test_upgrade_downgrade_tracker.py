"""
test_upgrade_downgrade_tracker.py
------------------------------------
Unit tests for Upgrade/Downgrade Tracker v1.
"""

import unittest

from upgrade_downgrade_tracker import UpgradeDowngradeTracker


def make_current(entity, recommendation):
    return {"entity": entity, "recommendation": recommendation}


def make_logged(entity, recommendation, generated_at):
    return {"entity": entity, "recommendation": recommendation, "generated_at": generated_at}


class TestCompareEntity(unittest.TestCase):
    def setUp(self):
        self.tracker = UpgradeDowngradeTracker()

    def test_no_prior_entry_is_new(self):
        result = self.tracker.compare_entity(make_current("Tesla", "BUY"), [])
        self.assertEqual(result["change"], "new")
        self.assertIsNone(result["previous"])

    def test_hold_to_buy_is_upgrade(self):
        logged = [make_logged("Tesla", "HOLD", "2026-08-01T00:00:00+00:00")]
        result = self.tracker.compare_entity(make_current("Tesla", "BUY"), logged)
        self.assertEqual(result["change"], "upgrade")

    def test_buy_to_sell_is_downgrade(self):
        logged = [make_logged("Tesla", "BUY", "2026-08-01T00:00:00+00:00")]
        result = self.tracker.compare_entity(make_current("Tesla", "SELL"), logged)
        self.assertEqual(result["change"], "downgrade")

    def test_sell_to_hold_is_upgrade(self):
        logged = [make_logged("Tesla", "SELL", "2026-08-01T00:00:00+00:00")]
        result = self.tracker.compare_entity(make_current("Tesla", "HOLD"), logged)
        self.assertEqual(result["change"], "upgrade")

    def test_same_recommendation_is_unchanged(self):
        logged = [make_logged("Tesla", "BUY", "2026-08-01T00:00:00+00:00")]
        result = self.tracker.compare_entity(make_current("Tesla", "BUY"), logged)
        self.assertEqual(result["change"], "unchanged")

    def test_uses_most_recent_prior_entry_when_multiple_exist(self):
        logged = [
            make_logged("Tesla", "SELL", "2026-07-01T00:00:00+00:00"),
            make_logged("Tesla", "HOLD", "2026-08-01T00:00:00+00:00"),
        ]
        result = self.tracker.compare_entity(make_current("Tesla", "BUY"), logged)
        self.assertEqual(result["previous"], "HOLD")
        self.assertEqual(result["change"], "upgrade")

    def test_ignores_entries_for_other_entities(self):
        logged = [make_logged("Apple", "BUY", "2026-08-01T00:00:00+00:00")]
        result = self.tracker.compare_entity(make_current("Tesla", "BUY"), logged)
        self.assertEqual(result["change"], "new")


class TestCompareBatch(unittest.TestCase):
    def setUp(self):
        self.tracker = UpgradeDowngradeTracker()

    def test_compares_every_entity_in_batch(self):
        current = [make_current("Tesla", "BUY"), make_current("Apple", "SELL")]
        logged = [make_logged("Tesla", "HOLD", "2026-08-01T00:00:00+00:00")]
        results = self.tracker.compare_batch(current, logged)
        by_entity = {r["entity"]: r for r in results}
        self.assertEqual(by_entity["Tesla"]["change"], "upgrade")
        self.assertEqual(by_entity["Apple"]["change"], "new")

    def test_empty_current_returns_empty_list(self):
        self.assertEqual(self.tracker.compare_batch([], []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
