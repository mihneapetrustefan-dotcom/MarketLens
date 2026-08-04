"""
test_recommendation_engine.py
--------------------------------
Unit tests for Recommendation Engine v1 (recommendation_engine.py).

TESTING STRATEGY:
Synthetic entity-confidence dicts (matching ConfidenceEngine's exact
output shape) with hand-crafted values for each of the three gates.
Each gate gets a dedicated test proving it independently forces HOLD,
plus tests confirming BUY/SELL are reachable only when ALL gates pass.
"""

import unittest

from recommendation_engine import RecommendationEngine


def make_entity_confidence(**overrides):
    base = {
        "entity": "Tesla",
        "article_count": 3,
        "distinct_source_count": 3,
        "sentiment_breakdown": {"positive": 3, "negative": 0, "neutral": 0},
        "dominant_sentiment": "positive",
        "sentiment_consistency": 1.0,
        "average_impact": 0.6,
        "confidence_score": 0.8,
        "sufficient_data": True,
        "reason": "Based on 3 articles from 3 distinct source(s)",
    }
    base.update(overrides)
    return base


class TestSufficientDataGate(unittest.TestCase):
    """Tests for Gate 1 — the 'never a single article' rule."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_insufficient_data_forces_hold_regardless_of_strong_positive_signal(self):
        ec = make_entity_confidence(
            sufficient_data=False,
            confidence_score=0.3,
            dominant_sentiment="positive",
            average_impact=1.0,
            article_count=1,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIn("insufficient data", result["explanation"].lower())

    def test_insufficient_data_forces_hold_regardless_of_strong_negative_signal(self):
        ec = make_entity_confidence(
            sufficient_data=False,
            confidence_score=0.3,
            dominant_sentiment="negative",
            average_impact=1.0,
            article_count=1,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")


class TestMinimumConfidenceGate(unittest.TestCase):
    """Tests for Gate 2 — minimum confidence required for a directional call."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_low_confidence_forces_hold_despite_positive_sentiment(self):
        ec = make_entity_confidence(confidence_score=0.2, dominant_sentiment="positive", average_impact=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIn("confidence score is below", result["explanation"].lower())

    def test_confidence_exactly_at_threshold_passes_the_gate(self):
        ec = make_entity_confidence(confidence_score=0.5, dominant_sentiment="positive", average_impact=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")


class TestMinimumImpactGate(unittest.TestCase):
    """Tests for Gate 3 — minimum market impact required for a directional call."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_low_impact_forces_hold_despite_high_confidence_and_positive_sentiment(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="positive", average_impact=0.1)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIn("average impact is below", result["explanation"].lower())

    def test_impact_exactly_at_threshold_passes_the_gate(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="negative", average_impact=0.3)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "SELL")


class TestDirectionalCall(unittest.TestCase):
    """Tests for the final directional decision once all gates pass."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_positive_sentiment_with_all_gates_passed_gives_buy(self):
        ec = make_entity_confidence(dominant_sentiment="positive", confidence_score=0.8, average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")

    def test_negative_sentiment_with_all_gates_passed_gives_sell(self):
        ec = make_entity_confidence(dominant_sentiment="negative", confidence_score=0.8, average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "SELL")

    def test_mixed_sentiment_with_all_gates_passed_still_gives_hold(self):
        ec = make_entity_confidence(dominant_sentiment="mixed", confidence_score=0.8, average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIn("no clear positive or negative consensus", result["explanation"].lower())

    def test_neutral_sentiment_with_all_gates_passed_still_gives_hold(self):
        ec = make_entity_confidence(dominant_sentiment="neutral", confidence_score=0.8, average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")


class TestExplanationContent(unittest.TestCase):
    """Tests confirming explanations cite the real figures, not just a label."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_explanation_cites_article_and_source_counts(self):
        ec = make_entity_confidence(article_count=5, distinct_source_count=4)
        result = self.engine.recommend_entity(ec)
        self.assertIn("5 article", result["explanation"])
        self.assertIn("4 distinct source", result["explanation"])

    def test_explanation_cites_confidence_score(self):
        ec = make_entity_confidence(confidence_score=0.73)
        result = self.engine.recommend_entity(ec)
        self.assertIn("0.73", result["explanation"])

    def test_result_includes_underlying_figures_not_just_label(self):
        ec = make_entity_confidence()
        result = self.engine.recommend_entity(ec)
        for key in ["confidence_score", "sufficient_data", "article_count",
                    "distinct_source_count", "dominant_sentiment", "average_impact"]:
            self.assertIn(key, result)


class TestConfigurableThresholds(unittest.TestCase):
    """Tests confirming the gate thresholds are now real, working constructor parameters (v1.3)."""

    def test_lower_confidence_threshold_lets_weaker_signal_through(self):
        engine = RecommendationEngine(min_confidence_for_action=0.2)
        ec = make_entity_confidence(confidence_score=0.3, dominant_sentiment="positive", average_impact=0.9)
        result = engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")

    def test_lower_impact_threshold_lets_routine_news_through(self):
        engine = RecommendationEngine(min_impact_for_action=0.05)
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="negative", average_impact=0.1)
        result = engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "SELL")

    def test_default_values_unchanged_from_original_constants(self):
        engine = RecommendationEngine()
        self.assertEqual(engine.min_confidence_for_action, 0.5)
        self.assertEqual(engine.min_impact_for_action, 0.3)


class TestRecommendAll(unittest.TestCase):
    """Tests for recommend_all(): the full-list orchestration method."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_returns_one_recommendation_per_entity(self):
        entities = [
            make_entity_confidence(entity="Tesla", dominant_sentiment="positive"),
            make_entity_confidence(entity="Microsoft", sufficient_data=False, article_count=1),
        ]
        results = self.engine.recommend_all(entities)
        self.assertEqual(len(results), 2)
        tesla = next(r for r in results if r["entity"] == "Tesla")
        msft = next(r for r in results if r["entity"] == "Microsoft")
        self.assertEqual(tesla["recommendation"], "BUY")
        self.assertEqual(msft["recommendation"], "HOLD")

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(self.engine.recommend_all([]), [])


class TestComponentPropagation(unittest.TestCase):
    """Tests for propagating breakdown fields (volume/diversity/consistency) through."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_propagates_volume_and_diversity_scores(self):
        ec = make_entity_confidence(volume_score=0.6, source_diversity_score=0.75)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["volume_score"], 0.6)
        self.assertEqual(result["source_diversity_score"], 0.75)

    def test_missing_breakdown_fields_default_gracefully(self):
        ec = make_entity_confidence()
        ec.pop("volume_score", None)
        ec.pop("source_diversity_score", None)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["volume_score"], 0.0)
        self.assertEqual(result["source_diversity_score"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
