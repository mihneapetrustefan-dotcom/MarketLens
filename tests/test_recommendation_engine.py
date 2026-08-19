"""
test_recommendation_engine.py
---------------------------------
Unit tests for Recommendation Engine v1.3 (adds STRONG_BUY/STRONG_SELL).
"""

import unittest

from recommendation_engine import RecommendationEngine


def make_entity_confidence(
    entity="Tesla", confidence_score=0.7, sufficient_data=True,
    dominant_sentiment="positive", average_impact=0.5,
    sentiment_consistency=0.6, article_count=5,
):
    return {
        "entity": entity, "confidence_score": confidence_score, "sufficient_data": sufficient_data,
        "dominant_sentiment": dominant_sentiment, "average_impact": average_impact,
        "sentiment_consistency": sentiment_consistency, "article_count": article_count,
    }


class TestBasicRecommendation(unittest.TestCase):
    def setUp(self):
        self.engine = RecommendationEngine()

    def test_positive_sentiment_sufficient_data_gives_buy(self):
        ec = make_entity_confidence(dominant_sentiment="positive", confidence_score=0.6, average_impact=0.5)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")

    def test_negative_sentiment_gives_sell(self):
        ec = make_entity_confidence(dominant_sentiment="negative", confidence_score=0.6, average_impact=0.5)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "SELL")

    def test_neutral_sentiment_gives_hold(self):
        ec = make_entity_confidence(dominant_sentiment="neutral", confidence_score=0.9, average_impact=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")

    def test_insufficient_data_forces_hold(self):
        ec = make_entity_confidence(sufficient_data=False, dominant_sentiment="positive", confidence_score=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")

    def test_low_confidence_forces_hold(self):
        ec = make_entity_confidence(confidence_score=0.3, dominant_sentiment="positive", average_impact=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")

    def test_low_impact_forces_hold(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="positive", average_impact=0.1)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")


class TestHoldGapTransparency(unittest.TestCase):
    """Tests for the v1.5 hold_gap field — exact distance to actionable, for HOLDs blocked by a numeric gate."""

    def setUp(self):
        self.engine = RecommendationEngine()

    def test_hold_blocked_by_confidence_reports_gap(self):
        ec = make_entity_confidence(confidence_score=0.42, dominant_sentiment="positive", average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIsNotNone(result["hold_gap"])
        self.assertEqual(result["hold_gap"]["blocked_by"], "confidence")
        self.assertAlmostEqual(result["hold_gap"]["gap"], 0.08, places=2)
        self.assertEqual(result["hold_gap"]["threshold"], 0.5)

    def test_hold_blocked_by_impact_reports_gap(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="positive", average_impact=0.22)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIsNotNone(result["hold_gap"])
        self.assertEqual(result["hold_gap"]["blocked_by"], "impact")
        self.assertAlmostEqual(result["hold_gap"]["gap"], 0.08, places=2)

    def test_hold_from_insufficient_data_has_no_gap(self):
        ec = make_entity_confidence(sufficient_data=False, confidence_score=0.9, dominant_sentiment="positive")
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIsNone(result["hold_gap"])

    def test_hold_from_neutral_sentiment_has_no_gap(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="neutral", average_impact=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertIsNone(result["hold_gap"])

    def test_buy_has_no_hold_gap(self):
        ec = make_entity_confidence(confidence_score=0.6, dominant_sentiment="positive", average_impact=0.5)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")
        self.assertIsNone(result["hold_gap"])

    def test_strong_buy_has_no_hold_gap(self):
        ec = make_entity_confidence(confidence_score=0.9, dominant_sentiment="positive", average_impact=0.6, sentiment_consistency=0.9)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "STRONG_BUY")
        self.assertIsNone(result["hold_gap"])

    def test_gap_is_zero_at_exact_threshold_boundary(self):
        # Exactly AT the threshold should qualify as BUY (>=), so this
        # tests just BELOW it produces a tiny, correctly-rounded gap.
        ec = make_entity_confidence(confidence_score=0.499, dominant_sentiment="positive", average_impact=0.6)
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["hold_gap"]["gap"], 0.001)


class TestStrongTier(unittest.TestCase):
    def setUp(self):
        self.engine = RecommendationEngine()

    def test_high_confidence_high_consistency_upgrades_to_strong_buy(self):
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.9,
            average_impact=0.6, sentiment_consistency=0.9,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "STRONG_BUY")

    def test_high_confidence_high_consistency_upgrades_to_strong_sell(self):
        ec = make_entity_confidence(
            dominant_sentiment="negative", confidence_score=0.9,
            average_impact=0.6, sentiment_consistency=0.9,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "STRONG_SELL")

    def test_high_confidence_but_low_consistency_stays_plain_buy(self):
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.95,
            average_impact=0.6, sentiment_consistency=0.4,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")

    def test_high_consistency_but_low_confidence_stays_plain_buy(self):
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.6,
            average_impact=0.6, sentiment_consistency=0.95,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")

    def test_exactly_at_threshold_upgrades(self):
        engine = RecommendationEngine(strong_confidence_threshold=0.85, strong_consistency_threshold=0.85)
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.85,
            average_impact=0.6, sentiment_consistency=0.85,
        )
        result = engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "STRONG_BUY")

    def test_hold_never_becomes_strong(self):
        ec = make_entity_confidence(
            dominant_sentiment="neutral", confidence_score=0.99,
            average_impact=0.99, sentiment_consistency=0.99,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")

    def test_hold_from_insufficient_data_never_becomes_strong_even_with_high_consistency(self):
        ec = make_entity_confidence(
            sufficient_data=False, dominant_sentiment="positive",
            confidence_score=0.99, sentiment_consistency=0.99,
        )
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "HOLD")

    def test_custom_strong_thresholds_are_respected(self):
        engine = RecommendationEngine(strong_confidence_threshold=0.6, strong_consistency_threshold=0.5)
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.65,
            average_impact=0.5, sentiment_consistency=0.55,
        )
        result = engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "STRONG_BUY")

    def test_default_strong_thresholds_unchanged(self):
        engine = RecommendationEngine()
        self.assertEqual(engine.strong_confidence_threshold, 0.85)
        self.assertEqual(engine.strong_consistency_threshold, 0.85)

    def test_strong_explanation_mentions_strong_signal(self):
        ec = make_entity_confidence(
            dominant_sentiment="positive", confidence_score=0.9,
            average_impact=0.6, sentiment_consistency=0.9,
        )
        result = self.engine.recommend_entity(ec)
        self.assertIn("puternic", result["explanation"].lower())

    def test_missing_sentiment_consistency_treated_as_zero_never_strong(self):
        ec = make_entity_confidence(dominant_sentiment="positive", confidence_score=0.99)
        del ec["sentiment_consistency"]
        result = self.engine.recommend_entity(ec)
        self.assertEqual(result["recommendation"], "BUY")


class TestConfigurableThresholds(unittest.TestCase):
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
    def setUp(self):
        self.engine = RecommendationEngine()

    def test_returns_one_recommendation_per_entity(self):
        entities = [make_entity_confidence(entity="Tesla"), make_entity_confidence(entity="Apple", dominant_sentiment="neutral")]
        results = self.engine.recommend_all(entities)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["entity"] for r in results}, {"Tesla", "Apple"})

    def test_empty_list_returns_empty(self):
        self.assertEqual(self.engine.recommend_all([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
