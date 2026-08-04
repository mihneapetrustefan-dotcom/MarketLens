"""
test_confidence_engine.py
----------------------------
Unit tests for Confidence Score v1 (confidence_engine.py).

TESTING STRATEGY:
Synthetic article dicts with hand-crafted `companies_mentioned`,
`tickers_mentioned`, `source`, `sentiment`, and `impact` fields. The
"never a single article" rule — this module's central design decision
— gets dedicated tests, as does the ticker-to-company entity merge.
"""

import unittest
from datetime import datetime, timedelta, timezone

from confidence_engine import ConfidenceEngine
from time_decay import TimeDecayCalculator


def make_article(**overrides):
    base = {
        "article_id": "id-0",
        "source": "TestSource",
        "companies_mentioned": [],
        "tickers_mentioned": [],
        "sentiment": {"label": "neutral"},
        "impact": {"score": 0.0},
    }
    base.update(overrides)
    return base


def company_mention(name):
    return {"company": name, "ticker": "X", "category": "stocks"}


def ticker_mention(symbol, name):
    return {"ticker": symbol, "name": name, "category": "bvb", "match_type": "bare"}


class TestEntityAggregation(unittest.TestCase):
    """Tests for aggregate_by_entity() and the ticker/company merge."""

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_groups_articles_by_company_name(self):
        articles = [
            make_article(article_id="1", companies_mentioned=[company_mention("Tesla")]),
            make_article(article_id="2", companies_mentioned=[company_mention("Tesla")]),
        ]
        entity_map = self.engine.aggregate_by_entity(articles)
        self.assertIn("Tesla", entity_map)
        self.assertEqual(len(entity_map["Tesla"]), 2)

    def test_ticker_mention_merges_into_same_entity_as_company_mention(self):
        # "TLV" (ticker only) and "Banca Transilvania" (company name)
        # must be combined under ONE entity, since ticker_registry
        # derives its "name" field from the same canonical company name.
        articles = [
            make_article(article_id="1", companies_mentioned=[company_mention("Banca Transilvania")]),
            make_article(article_id="2", tickers_mentioned=[ticker_mention("TLV", "Banca Transilvania")]),
        ]
        entity_map = self.engine.aggregate_by_entity(articles)
        self.assertIn("Banca Transilvania", entity_map)
        self.assertEqual(len(entity_map["Banca Transilvania"]), 2)

    def test_article_with_multiple_companies_appears_under_each(self):
        articles = [
            make_article(
                article_id="1",
                companies_mentioned=[company_mention("Tesla"), company_mention("Apple")],
            ),
        ]
        entity_map = self.engine.aggregate_by_entity(articles)
        self.assertIn("Tesla", entity_map)
        self.assertIn("Apple", entity_map)

    def test_unrecognized_ticker_contributes_no_entity(self):
        articles = [make_article(tickers_mentioned=[{"ticker": "XYZ", "name": None, "category": None}])]
        entity_map = self.engine.aggregate_by_entity(articles)
        self.assertEqual(entity_map, {})

    def test_empty_article_list_returns_empty_map(self):
        self.assertEqual(self.engine.aggregate_by_entity([]), {})


class TestMinimumArticleGate(unittest.TestCase):
    """
    Tests for the 'never a single article' hard rule — the module's
    central design decision.
    """

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_single_article_entity_is_flagged_insufficient(self):
        articles = [make_article(sentiment={"label": "positive"}, impact={"score": 1.0})]
        result = self.engine.score_entity("Tesla", articles)
        self.assertFalse(result["sufficient_data"])
        self.assertIn("Only 1 article", result["reason"])

    def test_single_article_confidence_is_capped_even_with_strong_signal(self):
        # Even a single article with maximum impact and clear sentiment
        # must not produce a high confidence score.
        articles = [make_article(sentiment={"label": "positive"}, impact={"score": 1.0}, source="A")]
        result = self.engine.score_entity("Tesla", articles)
        self.assertLessEqual(result["confidence_score"], 0.3)

    def test_two_articles_pass_the_gate(self):
        articles = [
            make_article(article_id="1", source="A", sentiment={"label": "positive"}),
            make_article(article_id="2", source="B", sentiment={"label": "positive"}),
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertTrue(result["sufficient_data"])


class TestSentimentConsistency(unittest.TestCase):
    """Tests for the sentiment-consistency factor."""

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_all_positive_articles_give_full_consistency(self):
        articles = [
            make_article(article_id=str(i), source=f"S{i}", sentiment={"label": "positive"})
            for i in range(3)
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertEqual(result["sentiment_consistency"], 1.0)
        self.assertEqual(result["dominant_sentiment"], "positive")

    def test_evenly_split_sentiment_gives_half_consistency(self):
        articles = [
            make_article(article_id="1", source="A", sentiment={"label": "positive"}),
            make_article(article_id="2", source="B", sentiment={"label": "negative"}),
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertEqual(result["sentiment_consistency"], 0.5)
        self.assertEqual(result["dominant_sentiment"], "mixed")

    def test_all_neutral_gives_zero_consistency(self):
        articles = [
            make_article(article_id="1", source="A", sentiment={"label": "neutral"}),
            make_article(article_id="2", source="B", sentiment={"label": "neutral"}),
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertEqual(result["sentiment_consistency"], 0.0)
        self.assertEqual(result["dominant_sentiment"], "neutral")


class TestVolumeAndSourceDiversity(unittest.TestCase):
    """Tests distinguishing article volume from source diversity."""

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_many_articles_from_one_source_have_low_source_diversity(self):
        # 5 articles, but all from the SAME source, should score high on
        # volume but low on source diversity — these are separate axes.
        articles = [
            make_article(article_id=str(i), source="OnlyOneSource", sentiment={"label": "positive"})
            for i in range(5)
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertEqual(result["distinct_source_count"], 1)
        self.assertEqual(result["article_count"], 5)

    def test_confidence_higher_with_diverse_sources_than_single_source(self):
        same_source = [
            make_article(article_id=str(i), source="OnlyOne", sentiment={"label": "positive"})
            for i in range(4)
        ]
        diverse_sources = [
            make_article(article_id=str(i), source=f"Source{i}", sentiment={"label": "positive"})
            for i in range(4)
        ]
        result_same = self.engine.score_entity("Tesla", same_source)
        result_diverse = self.engine.score_entity("Tesla", diverse_sources)
        self.assertGreater(result_diverse["confidence_score"], result_same["confidence_score"])


class TestTimeDecayIntegration(unittest.TestCase):
    """
    Tests for how time-decay weighting affects Confidence Score's
    aggregation — the reason this integration exists at all.
    """

    def setUp(self):
        self.reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        # A short half-life makes the effect easy to observe within a
        # few hours/days of simulated age, without needing huge gaps.
        self.engine = ConfidenceEngine(time_decay=TimeDecayCalculator(half_life_hours=24.0))

    def test_old_negative_article_no_longer_dominates_recent_positive_one(self):
        # An OLD negative article (10 half-lives ago -> weight ~0) and a
        # BRAND NEW positive article (weight ~1) about the same entity.
        # Without decay, this would be a 50/50 split (dominant="mixed").
        # With decay, the recent positive article should clearly win.
        old_negative = make_article(
            article_id="old", source="A",
            sentiment={"label": "negative"},
            collected_at=(self.reference - timedelta(hours=240)).isoformat(),
        )
        new_positive = make_article(
            article_id="new", source="B",
            sentiment={"label": "positive"},
            collected_at=self.reference.isoformat(),
        )
        result = self.engine.score_entity("Tesla", [old_negative, new_positive], reference_time=self.reference)
        self.assertEqual(result["dominant_sentiment"], "positive")
        self.assertGreater(result["sentiment_consistency"], 0.9)

    def test_missing_collected_at_behaves_as_full_weight(self):
        # Articles with no `collected_at` at all (as in every OTHER
        # test in this file) must behave exactly as if freshly
        # collected — this is what keeps every prior test in this file
        # passing unchanged after adding time decay.
        article = make_article(sentiment={"label": "positive"})
        self.assertNotIn("collected_at", article)
        result = self.engine.score_entity("Tesla", [article, article], reference_time=self.reference)
        self.assertEqual(result["average_recency_weight"], 1.0)

    def test_average_recency_weight_reflects_article_ages(self):
        very_old = make_article(
            article_id="1", source="A",
            collected_at=(self.reference - timedelta(hours=480)).isoformat(),  # 20 half-lives -> ~0 weight
        )
        brand_new = make_article(
            article_id="2", source="B",
            collected_at=self.reference.isoformat(),
        )
        result = self.engine.score_entity("Tesla", [very_old, brand_new], reference_time=self.reference)
        # Average of (~0 + ~1) / 2 should land close to 0.5, not 1.0.
        self.assertLess(result["average_recency_weight"], 0.6)

    def test_sufficient_data_gate_uses_raw_count_not_decayed_weight(self):
        # Even if both articles are extremely old (near-zero weight),
        # the gate must still pass on RAW count — the rule is "at least
        # 2 articles exist", not "at least 2 articles' worth of weight".
        very_old_1 = make_article(
            article_id="1", source="A",
            collected_at=(self.reference - timedelta(hours=1000)).isoformat(),
        )
        very_old_2 = make_article(
            article_id="2", source="B",
            collected_at=(self.reference - timedelta(hours=1000)).isoformat(),
        )
        result = self.engine.score_entity("Tesla", [very_old_1, very_old_2], reference_time=self.reference)
        self.assertTrue(result["sufficient_data"])

    def test_score_all_entities_accepts_reference_time(self):
        articles = [
            make_article(article_id="1", source="A", companies_mentioned=[company_mention("Tesla")],
                         collected_at=self.reference.isoformat()),
            make_article(article_id="2", source="B", companies_mentioned=[company_mention("Tesla")],
                         collected_at=self.reference.isoformat()),
        ]
        results = self.engine.score_all_entities(articles, reference_time=self.reference)
        self.assertEqual(results[0]["average_recency_weight"], 1.0)

    def test_published_at_takes_priority_over_collected_at_for_decay(self):
        # Reproduces a real bug found in production: Google News
        # Historical Backfill articles all share the SAME collected_at
        # (the moment of the collection run) regardless of how old the
        # actual news is. Using collected_at for decay would treat a
        # 2-month-old backfilled article as brand new; published_at
        # (the real article date) must be used instead when present.
        same_collection_moment = self.reference.isoformat()
        old_article = make_article(
            article_id="old", source="A", sentiment={"label": "negative"},
            published_at=(self.reference - timedelta(days=60)).isoformat(),
            collected_at=same_collection_moment,
        )
        new_article = make_article(
            article_id="new", source="B", sentiment={"label": "positive"},
            published_at=self.reference.isoformat(),
            collected_at=same_collection_moment,
        )
        result = self.engine.score_entity("Tesla", [old_article, new_article], reference_time=self.reference)
        # The old article (60 days back, 2.5 half-lives at 24h) should
        # weigh much less than the brand-new one, despite an IDENTICAL
        # collected_at — proving published_at drove the calculation.
        self.assertLess(result["average_recency_weight"], 0.6)


class TestConfigurableThresholds(unittest.TestCase):
    """
    Tests confirming weights/thresholds are now real, working
    constructor parameters (v1.3) — not just present but inert.
    """

    def test_lower_min_articles_lets_single_article_pass_the_gate(self):
        engine = ConfidenceEngine(min_articles_for_confidence=1)
        article = make_article(sentiment={"label": "positive"})
        result = engine.score_entity("Tesla", [article])
        self.assertTrue(result["sufficient_data"])

    def test_custom_max_single_article_confidence_is_respected(self):
        engine = ConfidenceEngine(max_single_article_confidence=0.1)
        article = make_article(sentiment={"label": "positive"}, impact={"score": 1.0})
        result = engine.score_entity("Tesla", [article])
        self.assertLessEqual(result["confidence_score"], 0.1)

    def test_custom_saturation_points_change_scores(self):
        articles = [
            make_article(article_id="1", source="A"),
            make_article(article_id="2", source="B"),
        ]
        default_engine = ConfidenceEngine()
        easier_engine = ConfidenceEngine(volume_saturation=2, source_saturation=2)

        default_result = default_engine.score_entity("Tesla", articles)
        easier_result = easier_engine.score_entity("Tesla", articles)
        # With a lower saturation point, the same 2 articles/2 sources
        # should produce a HIGHER (or equal) confidence than the default.
        self.assertGreaterEqual(easier_result["confidence_score"], default_result["confidence_score"])

    def test_default_values_unchanged_from_original_constants(self):
        # Locks in that the defaults still match the values that were
        # previously hardcoded — this refactor must not silently change
        # behavior for any existing caller.
        engine = ConfidenceEngine()
        self.assertEqual(engine.volume_weight, 0.25)
        self.assertEqual(engine.source_diversity_weight, 0.25)
        self.assertEqual(engine.consistency_weight, 0.30)
        self.assertEqual(engine.impact_weight, 0.20)
        self.assertEqual(engine.volume_saturation, 5)
        self.assertEqual(engine.source_saturation, 4)
        self.assertEqual(engine.min_articles_for_confidence, 2)
        self.assertEqual(engine.max_single_article_confidence, 0.3)

    def test_default_engine_decays_crypto_faster_than_stocks(self):
        # ConfidenceEngine's own default TimeDecayCalculator applies a
        # shorter half-life to "crypto" category articles than to
        # "stocks" — verified here through the real, wired-up default,
        # not just at the TimeDecayCalculator level in isolation.
        reference = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        old_timestamp = (reference - timedelta(hours=200)).isoformat()

        crypto_article = make_article(
            article_id="1", source="A", category="crypto", collected_at=old_timestamp,
        )
        stock_article = make_article(
            article_id="2", source="A", category="stocks", collected_at=old_timestamp,
        )

        engine = ConfidenceEngine()
        crypto_result = engine.score_entity("Bitcoin", [crypto_article, crypto_article], reference_time=reference)
        stock_result = engine.score_entity("Tesla", [stock_article, stock_article], reference_time=reference)

        self.assertLess(crypto_result["average_recency_weight"], stock_result["average_recency_weight"])


class TestScoreAllEntities(unittest.TestCase):
    """Tests for score_all_entities(): the full-batch orchestration method."""

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_returns_one_entry_per_entity_sorted_by_confidence(self):
        articles = [
            make_article(article_id="1", source="A", companies_mentioned=[company_mention("Tesla")],
                         sentiment={"label": "positive"}, impact={"score": 0.8}),
            make_article(article_id="2", source="B", companies_mentioned=[company_mention("Tesla")],
                         sentiment={"label": "positive"}, impact={"score": 0.8}),
            make_article(article_id="3", source="C", companies_mentioned=[company_mention("Apple")],
                         sentiment={"label": "neutral"}, impact={"score": 0.1}),
        ]
        results = self.engine.score_all_entities(articles)
        entity_names = [r["entity"] for r in results]
        self.assertIn("Tesla", entity_names)
        self.assertIn("Apple", entity_names)
        # Tesla (2 articles, consistent positive, high impact) should
        # rank above Apple (1 article, neutral, low impact).
        self.assertEqual(results[0]["entity"], "Tesla")

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.engine.score_all_entities([]), [])


class TestScoreComponentTransparency(unittest.TestCase):
    """Tests for the volume_score/source_diversity_score breakdown fields."""

    def setUp(self):
        self.engine = ConfidenceEngine()

    def test_volume_and_source_diversity_scores_are_present_and_bounded(self):
        articles = [
            make_article(article_id="1", source="A"),
            make_article(article_id="2", source="B"),
        ]
        result = self.engine.score_entity("Tesla", articles)
        self.assertIn("volume_score", result)
        self.assertIn("source_diversity_score", result)
        self.assertGreaterEqual(result["volume_score"], 0.0)
        self.assertLessEqual(result["volume_score"], 1.0)
        self.assertGreaterEqual(result["source_diversity_score"], 0.0)
        self.assertLessEqual(result["source_diversity_score"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
