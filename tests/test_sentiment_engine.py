"""
test_sentiment_engine.py
---------------------------
Unit tests for Sentiment Engine v1 (sentiment_engine.py).

TESTING STRATEGY:
Most tests use a SMALL, CONTROLLED lexicon injected via the
constructor, isolating the scoring logic from the real, larger word
lists. A few tests at the end sanity-check against the real
POSITIVE_WORDS / NEGATIVE_WORDS.
"""

import unittest

from sentiment_engine import SentimentEngine


TEST_POSITIVE = ["surge", "beats", "record profit"]
TEST_NEGATIVE = ["plunge", "misses", "lawsuit"]


def make_article(**overrides):
    base = {
        "article_id": "id-0",
        "title": "Default headline",
        "summary": "",
    }
    base.update(overrides)
    return base


class TestAnalyzeText(unittest.TestCase):
    """Tests for analyze_text(): the core scoring logic."""

    def setUp(self):
        self.engine = SentimentEngine(positive_words=TEST_POSITIVE, negative_words=TEST_NEGATIVE)

    def test_all_positive_words_score_positive_one(self):
        result = self.engine.analyze_text("Shares surge as company beats estimates")
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["label"], "positive")

    def test_all_negative_words_score_negative_one(self):
        result = self.engine.analyze_text("Stock plunges after company misses targets amid lawsuit")
        self.assertEqual(result["score"], -1.0)
        self.assertEqual(result["label"], "negative")

    def test_equal_positive_and_negative_scores_neutral(self):
        result = self.engine.analyze_text("Shares surge then plunge in volatile session")
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["label"], "neutral")

    def test_no_sentiment_words_is_neutral_with_zero_confidence(self):
        result = self.engine.analyze_text("The company held its annual shareholder meeting today")
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["label"], "neutral")
        self.assertEqual(result["confidence"], 0.0)

    def test_matched_words_are_reported(self):
        result = self.engine.analyze_text("Shares surge to record profit")
        self.assertIn("surge", result["matched_positive"])
        self.assertIn("record profit", result["matched_positive"])
        self.assertEqual(result["matched_negative"], [])

    def test_word_boundary_avoids_partial_word_match(self):
        # "surgeon" must NOT match the "surge" lexicon entry.
        result = self.engine.analyze_text("The surgeon performed a routine operation")
        self.assertEqual(result["matched_positive"], [])

    def test_matching_is_case_insensitive(self):
        result = self.engine.analyze_text("SURGE in shares reported today")
        self.assertIn("surge", result["matched_positive"])

    def test_confidence_increases_with_more_matches(self):
        one_match = self.engine.analyze_text("Shares surge today")
        three_matches = self.engine.analyze_text("Shares surge, beats estimates, hits record profit")
        self.assertLess(one_match["confidence"], three_matches["confidence"])

    def test_confidence_saturates_at_one(self):
        # 3 distinct lexicon entries max out at 3/5 = 0.6 in this small
        # test lexicon, so confidence can never reach 1.0 here — this
        # test just confirms it never EXCEEDS 1.0, using repeated text.
        result = self.engine.analyze_text("surge surge surge beats beats record profit record profit")
        self.assertLessEqual(result["confidence"], 1.0)

    def test_empty_text_returns_neutral_with_zero_confidence(self):
        result = self.engine.analyze_text("")
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["label"], "neutral")
        self.assertEqual(result["confidence"], 0.0)


class TestAnalyzeArticleAndBatch(unittest.TestCase):
    """Tests for analyze_article() and analyze_batch()."""

    def setUp(self):
        self.engine = SentimentEngine(positive_words=TEST_POSITIVE, negative_words=TEST_NEGATIVE)

    def test_combines_title_and_summary(self):
        article = make_article(title="Markets react", summary="Shares surge on strong demand")
        tagged = self.engine.analyze_article(article)
        self.assertEqual(tagged["sentiment"]["label"], "positive")

    def test_does_not_mutate_original_article(self):
        article = make_article(title="Shares surge today")
        self.engine.analyze_article(article)
        self.assertNotIn("sentiment", article)

    def test_analyze_batch_tags_every_article(self):
        articles = [
            make_article(article_id="1", title="Shares surge to record profit"),
            make_article(article_id="2", title="Stock plunges amid lawsuit"),
            make_article(article_id="3", title="Company holds annual meeting"),
        ]
        result = self.engine.analyze_batch(articles)
        self.assertTrue(all("sentiment" in a for a in result))

        pos = next(a for a in result if a["article_id"] == "1")
        neg = next(a for a in result if a["article_id"] == "2")
        neu = next(a for a in result if a["article_id"] == "3")
        self.assertEqual(pos["sentiment"]["label"], "positive")
        self.assertEqual(neg["sentiment"]["label"], "negative")
        self.assertEqual(neu["sentiment"]["label"], "neutral")

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.engine.analyze_batch([]), [])


class TestRealLexiconSanityCheck(unittest.TestCase):
    """A few checks against the ACTUAL production lexicon."""

    def setUp(self):
        self.engine = SentimentEngine()  # uses real POSITIVE_WORDS / NEGATIVE_WORDS

    def test_real_positive_headline(self):
        result = self.engine.analyze_text("Shares rally after company beats earnings estimates")
        self.assertEqual(result["label"], "positive")

    def test_real_negative_headline(self):
        result = self.engine.analyze_text("Stock crashes as company reports massive losses and layoffs")
        self.assertEqual(result["label"], "negative")

    def test_real_romanian_positive_headline(self):
        result = self.engine.analyze_text("Actiunile companiei cresc dupa un profit record")
        self.assertEqual(result["label"], "positive")

    def test_real_romanian_negative_headline(self):
        result = self.engine.analyze_text("Compania anunta pierderi si o criza financiara")
        self.assertEqual(result["label"], "negative")


if __name__ == "__main__":
    unittest.main(verbosity=2)
