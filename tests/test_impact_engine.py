"""
test_impact_engine.py
------------------------
Unit tests for Impact Engine v1 (impact_engine.py).

TESTING STRATEGY:
Synthetic article dicts with hand-crafted `companies_mentioned`,
`tickers_mentioned`, `sectors`, `duplicate_group_size`, and `sentiment`
fields, so each test isolates one specific factor in the scoring
formula. The relevance gate — the module's central design decision —
gets dedicated tests reproducing the exact real-world case that
motivated it.
"""

import unittest

from impact_engine import ImpactEngine


def make_article(**overrides):
    base = {
        "article_id": "id-0",
        "title": "Default headline",
        "companies_mentioned": [],
        "tickers_mentioned": [],
        "sectors": [],
        "duplicate_group_size": 1,
        "sentiment": {"score": 0.0, "confidence": 0.0},
    }
    base.update(overrides)
    return base


def make_company(name="Tesla"):
    return {"company": name, "ticker": "TSLA", "category": "stocks"}


def make_sector(name="Automotive"):
    return {"sector": name, "source": "company", "via": ["Tesla"]}


class TestRelevanceGate(unittest.TestCase):
    """
    Tests for the mandatory relevance gate — the module's central
    design decision, motivated by a real production false positive
    (a castle-renovation article scoring "positive" sentiment despite
    zero market relevance).
    """

    def setUp(self):
        self.engine = ImpactEngine()

    def test_no_relevance_signals_forces_score_to_zero(self):
        # Strong sentiment AND strong corroboration, but NO known
        # company/ticker/sector at all -> impact must still be 0.
        article = make_article(
            duplicate_group_size=5,
            sentiment={"score": 1.0, "confidence": 1.0},
        )
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["score"], 0.0)
        self.assertEqual(result["impact"]["level"], "none")

    def test_relevance_gate_reason_is_explanatory(self):
        article = make_article()
        result = self.engine.score_article(article)
        self.assertIn("No known company", result["impact"]["reason"])

    def test_a_single_company_mention_passes_the_gate(self):
        article = make_article(companies_mentioned=[make_company()])
        result = self.engine.score_article(article)
        self.assertGreater(result["impact"]["score"], 0.0)
        self.assertNotEqual(result["impact"]["level"], "none")

    def test_a_single_ticker_mention_passes_the_gate(self):
        article = make_article(tickers_mentioned=[{"ticker": "TSLA", "match_type": "cashtag"}])
        result = self.engine.score_article(article)
        self.assertGreater(result["impact"]["score"], 0.0)

    def test_a_single_sector_mention_passes_the_gate(self):
        article = make_article(sectors=[make_sector()])
        result = self.engine.score_article(article)
        self.assertGreater(result["impact"]["score"], 0.0)


class TestRelevanceScoring(unittest.TestCase):
    """Tests for how relevance signal COUNT affects the score."""

    def setUp(self):
        self.engine = ImpactEngine()

    def test_more_distinct_signals_increase_relevance_score(self):
        one_signal = make_article(companies_mentioned=[make_company()])
        three_signals = make_article(
            companies_mentioned=[make_company()],
            tickers_mentioned=[{"ticker": "TSLA", "match_type": "bare"}],
            sectors=[make_sector()],
        )
        result_one = self.engine.score_article(one_signal)
        result_three = self.engine.score_article(three_signals)
        self.assertLess(
            result_one["impact"]["relevance_score"],
            result_three["impact"]["relevance_score"],
        )

    def test_relevance_score_saturates_at_one(self):
        article = make_article(
            companies_mentioned=[make_company("A"), make_company("B"), make_company("C"), make_company("D")],
        )
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["relevance_score"], 1.0)


class TestCorroborationScoring(unittest.TestCase):
    """Tests for how duplicate_group_size affects the score."""

    def setUp(self):
        self.engine = ImpactEngine()

    def test_higher_group_size_increases_corroboration_score(self):
        single_source = make_article(companies_mentioned=[make_company()], duplicate_group_size=1)
        multi_source = make_article(companies_mentioned=[make_company()], duplicate_group_size=5)
        result_single = self.engine.score_article(single_source)
        result_multi = self.engine.score_article(multi_source)
        self.assertEqual(result_single["impact"]["corroboration_score"], 0.0)
        self.assertGreater(result_multi["impact"]["corroboration_score"], 0.0)
        self.assertGreater(
            result_multi["impact"]["score"], result_single["impact"]["score"],
        )

    def test_missing_duplicate_group_size_defaults_to_single_source(self):
        article = make_article(companies_mentioned=[make_company()])
        del article["duplicate_group_size"]
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["corroboration_score"], 0.0)


class TestSentimentStrengthScoring(unittest.TestCase):
    """Tests for how sentiment score + confidence affect the impact score."""

    def setUp(self):
        self.engine = ImpactEngine()

    def test_strong_confident_sentiment_increases_score(self):
        weak = make_article(
            companies_mentioned=[make_company()],
            sentiment={"score": 0.1, "confidence": 0.2},
        )
        strong = make_article(
            companies_mentioned=[make_company()],
            sentiment={"score": 1.0, "confidence": 1.0},
        )
        result_weak = self.engine.score_article(weak)
        result_strong = self.engine.score_article(strong)
        self.assertGreater(
            result_strong["impact"]["sentiment_strength"],
            result_weak["impact"]["sentiment_strength"],
        )

    def test_strong_score_with_low_confidence_is_dampened(self):
        # score=1.0 but confidence=0.1 (only a very thin signal) should
        # produce a LOW sentiment_strength, not a high one.
        article = make_article(
            companies_mentioned=[make_company()],
            sentiment={"score": 1.0, "confidence": 0.1},
        )
        result = self.engine.score_article(article)
        self.assertLess(result["impact"]["sentiment_strength"], 0.2)

    def test_missing_sentiment_key_defaults_to_zero_strength(self):
        article = make_article(companies_mentioned=[make_company()])
        del article["sentiment"]
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["sentiment_strength"], 0.0)


class TestScoreCombinationAndLevels(unittest.TestCase):
    """Tests for the final weighted combination and level thresholds."""

    def setUp(self):
        self.engine = ImpactEngine()

    def test_maximal_inputs_produce_score_of_one(self):
        article = make_article(
            companies_mentioned=[make_company("A"), make_company("B"), make_company("C")],
            duplicate_group_size=5,
            sentiment={"score": 1.0, "confidence": 1.0},
        )
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["score"], 1.0)
        self.assertEqual(result["impact"]["level"], "high")

    def test_minimal_relevance_only_produces_low_level(self):
        article = make_article(companies_mentioned=[make_company()])  # relevance=1/3, rest 0
        result = self.engine.score_article(article)
        self.assertEqual(result["impact"]["level"], "low")

    def test_does_not_mutate_original_article(self):
        article = make_article(companies_mentioned=[make_company()])
        self.engine.score_article(article)
        self.assertNotIn("impact", article)


class TestScoreBatch(unittest.TestCase):
    """Tests for score_batch(): the full-list orchestration method."""

    def setUp(self):
        self.engine = ImpactEngine()

    def test_tags_every_article_in_batch(self):
        articles = [
            make_article(article_id="1", companies_mentioned=[make_company()]),
            make_article(article_id="2"),  # no relevance -> "none"
        ]
        result = self.engine.score_batch(articles)
        self.assertTrue(all("impact" in a for a in result))
        relevant = next(a for a in result if a["article_id"] == "1")
        irrelevant = next(a for a in result if a["article_id"] == "2")
        self.assertNotEqual(relevant["impact"]["level"], "none")
        self.assertEqual(irrelevant["impact"]["level"], "none")

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.engine.score_batch([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
