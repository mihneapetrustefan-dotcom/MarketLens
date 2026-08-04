"""
test_time_horizon_classifier.py
-----------------------------------
Unit tests for Time Horizon Classifier v1 (time_horizon_classifier.py).

TESTING STRATEGY: synthetic article dicts with hand-crafted
`collected_at` timestamps, isolating exactly the date-distribution
patterns the classifier is meant to distinguish.
"""

import unittest

from time_horizon_classifier import TimeHorizonClassifier


def make_article(collected_at):
    return {"collected_at": collected_at}


class TestClassifyEntity(unittest.TestCase):
    def setUp(self):
        self.classifier = TimeHorizonClassifier()

    def test_tightly_clustered_recent_articles_are_short_term(self):
        articles = [
            make_article("2026-08-01T09:00:00+00:00"),
            make_article("2026-08-02T10:00:00+00:00"),
            make_article("2026-08-02T14:00:00+00:00"),
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "short-term")

    def test_single_article_is_short_term(self):
        result = self.classifier.classify_entity("Tesla", [make_article("2026-08-02T09:00:00+00:00")])
        self.assertEqual(result["time_horizon"], "short-term")
        self.assertEqual(result["span_days"], 1)

    def test_coverage_spread_across_many_distinct_days_is_long_term(self):
        articles = [
            make_article("2026-07-01T09:00:00+00:00"),
            make_article("2026-07-08T09:00:00+00:00"),
            make_article("2026-07-15T09:00:00+00:00"),
            make_article("2026-07-22T09:00:00+00:00"),
            make_article("2026-08-01T09:00:00+00:00"),
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "long-term")
        self.assertEqual(result["distinct_days"], 5)

    def test_long_span_but_few_distinct_days_is_mixed(self):
        articles = [
            make_article("2026-07-01T09:00:00+00:00"),
            make_article("2026-07-31T09:00:00+00:00"),
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "mixed")

    def test_no_valid_dates_is_unknown(self):
        articles = [{"collected_at": None}, {"collected_at": "not-a-date"}]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "unknown")

    def test_empty_article_list_is_unknown(self):
        result = self.classifier.classify_entity("Tesla", [])
        self.assertEqual(result["time_horizon"], "unknown")

    def test_result_includes_reason_string(self):
        result = self.classifier.classify_entity("Tesla", [make_article("2026-08-02T09:00:00+00:00")])
        self.assertIsInstance(result["reason"], str)
        self.assertGreater(len(result["reason"]), 0)

    def test_multiple_articles_same_day_count_as_one_distinct_day(self):
        articles = [
            make_article("2026-08-02T08:00:00+00:00"),
            make_article("2026-08-02T14:00:00+00:00"),
            make_article("2026-08-02T20:00:00+00:00"),
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["distinct_days"], 1)
        self.assertEqual(result["span_days"], 1)

    def test_published_at_takes_priority_over_collected_at(self):
        # Reproduces a real bug found in production: articles pulled
        # via Google News Historical Backfill all share the SAME
        # collected_at (the moment of this run), but have REAL,
        # spread-out published_at dates from when they actually came
        # out. Using collected_at would make every entity look like a
        # tight, same-day event; published_at must be preferred.
        same_collection_moment = "2026-08-02T12:00:00+00:00"
        articles = [
            {"published_at": "2026-07-01T09:00:00+00:00", "collected_at": same_collection_moment},
            {"published_at": "2026-07-08T09:00:00+00:00", "collected_at": same_collection_moment},
            {"published_at": "2026-07-15T09:00:00+00:00", "collected_at": same_collection_moment},
            {"published_at": "2026-07-22T09:00:00+00:00", "collected_at": same_collection_moment},
            {"published_at": "2026-08-01T09:00:00+00:00", "collected_at": same_collection_moment},
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "long-term")
        self.assertEqual(result["distinct_days"], 5)

    def test_falls_back_to_collected_at_when_published_at_missing(self):
        articles = [
            {"published_at": None, "collected_at": "2026-08-02T09:00:00+00:00"},
        ]
        result = self.classifier.classify_entity("Tesla", articles)
        self.assertEqual(result["time_horizon"], "short-term")
        self.assertEqual(result["span_days"], 1)

    def test_sparse_incidental_mentions_over_wide_backfill_window_are_mixed_not_long_term(self):
        # Reproduces a real miscalibration found in production: once
        # Historical Backfill covers a ~60-day window for every
        # company, a handful of scattered, incidental mentions (4 stray
        # days out of 60) used to clear the old distinct-days-only bar
        # and get labeled "long-term" despite being sparse, not a real
        # sustained trend.
        articles = [
            {"published_at": "2026-06-04T09:00:00+00:00"},
            {"published_at": "2026-06-20T09:00:00+00:00"},
            {"published_at": "2026-07-10T09:00:00+00:00"},
            {"published_at": "2026-08-02T09:00:00+00:00"},
        ]
        result = self.classifier.classify_entity("SomeCompany", articles)
        self.assertEqual(result["time_horizon"], "mixed")
        self.assertLess(result["coverage_density"], 0.15)


class TestClassifyBatch(unittest.TestCase):
    def setUp(self):
        self.classifier = TimeHorizonClassifier()

    def test_classifies_every_entity_in_the_map(self):
        entity_map = {
            "Tesla": [make_article("2026-08-02T09:00:00+00:00")],
            "Amazon": [
                make_article("2026-07-01T09:00:00+00:00"),
                make_article("2026-07-08T09:00:00+00:00"),
                make_article("2026-07-15T09:00:00+00:00"),
                make_article("2026-07-22T09:00:00+00:00"),
                make_article("2026-08-01T09:00:00+00:00"),
            ],
        }
        results = self.classifier.classify_batch(entity_map)
        self.assertEqual(results["Tesla"]["time_horizon"], "short-term")
        self.assertEqual(results["Amazon"]["time_horizon"], "long-term")

    def test_empty_map_returns_empty_dict(self):
        self.assertEqual(self.classifier.classify_batch({}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
