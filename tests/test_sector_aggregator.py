"""
test_sector_aggregator.py
-----------------------------
Unit tests for Sector Aggregator v1.
"""

import unittest

from sector_aggregator import SectorAggregator


def make_article(sectors, sentiment_label="neutral", impact_score=0.0, source="TestSource"):
    return {
        "source": source,
        "sectors": [{"sector": s, "source": "company", "via": []} for s in sectors],
        "sentiment": {"label": sentiment_label},
        "impact": {"score": impact_score},
    }


class TestAggregateBySector(unittest.TestCase):
    def setUp(self):
        self.aggregator = SectorAggregator()

    def test_groups_articles_by_sector(self):
        articles = [
            make_article(["Technology"]),
            make_article(["Technology"]),
            make_article(["Energy"]),
        ]
        sector_map = self.aggregator.aggregate_by_sector(articles)
        self.assertEqual(len(sector_map["Technology"]), 2)
        self.assertEqual(len(sector_map["Energy"]), 1)

    def test_article_with_multiple_sectors_appears_under_each(self):
        articles = [make_article(["Technology", "Financial Services"])]
        sector_map = self.aggregator.aggregate_by_sector(articles)
        self.assertIn("Technology", sector_map)
        self.assertIn("Financial Services", sector_map)

    def test_empty_articles_returns_empty_map(self):
        self.assertEqual(self.aggregator.aggregate_by_sector([]), {})


class TestScoreSector(unittest.TestCase):
    def setUp(self):
        self.aggregator = SectorAggregator()

    def test_dominant_sentiment_positive(self):
        articles = [
            make_article(["Energy"], sentiment_label="positive"),
            make_article(["Energy"], sentiment_label="positive"),
            make_article(["Energy"], sentiment_label="negative"),
        ]
        result = self.aggregator.score_sector("Energy", articles)
        self.assertEqual(result["dominant_sentiment"], "positive")

    def test_all_neutral_sentiment(self):
        articles = [make_article(["Energy"], sentiment_label="neutral")]
        result = self.aggregator.score_sector("Energy", articles)
        self.assertEqual(result["dominant_sentiment"], "neutral")
        self.assertEqual(result["sentiment_consistency"], 0.0)

    def test_average_impact_computed_correctly(self):
        articles = [
            make_article(["Energy"], impact_score=0.4),
            make_article(["Energy"], impact_score=0.6),
        ]
        result = self.aggregator.score_sector("Energy", articles)
        self.assertEqual(result["average_impact"], 0.5)

    def test_distinct_source_count(self):
        articles = [
            make_article(["Energy"], source="Reuters"),
            make_article(["Energy"], source="CNBC"),
            make_article(["Energy"], source="Reuters"),
        ]
        result = self.aggregator.score_sector("Energy", articles)
        self.assertEqual(result["distinct_source_count"], 2)
        self.assertEqual(result["article_count"], 3)


class TestScoreAllSectors(unittest.TestCase):
    def setUp(self):
        self.aggregator = SectorAggregator()

    def test_sorted_by_article_count_descending(self):
        articles = (
            [make_article(["Technology"]) for _ in range(5)]
            + [make_article(["Energy"]) for _ in range(2)]
        )
        results = self.aggregator.score_all_sectors(articles)
        self.assertEqual(results[0]["sector"], "Technology")
        self.assertEqual(results[1]["sector"], "Energy")

    def test_empty_articles_returns_empty_list(self):
        self.assertEqual(self.aggregator.score_all_sectors([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
