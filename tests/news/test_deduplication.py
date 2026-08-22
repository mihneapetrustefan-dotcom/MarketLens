"""
tests/news/test_deduplication.py
-------------------------------------
Tests for the multi-level deduplication engine, including every
scenario the Phase 2 spec (§21) explicitly requires simulating.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.news.deduplication import (
    DeduplicationEngine, canonicalize_url, normalize_title,
    compute_fingerprint, compute_content_fingerprint, jaccard_similarity,
)
from src.domain.news_models import NormalizedArticle, DuplicateMatchLevel

PUB = datetime(2026, 8, 20, 14, 31, tzinfo=timezone.utc)


def make_article(article_id="a1", provider="rss", provider_article_id=None,
                 source_name="Reuters", source_url=None, canonical_url=None,
                 title="Nvidia reports record quarterly revenue",
                 summary="Nvidia beat analyst estimates on strong AI chip demand.",
                 published_at=PUB):
    return NormalizedArticle(
        article_id=article_id, provider=provider, provider_article_id=provider_article_id,
        source_name=source_name, source_url=source_url, canonical_url=canonical_url,
        title=title, summary=summary, published_at=published_at,
    )


class TestUrlCanonicalization(unittest.TestCase):
    def test_strips_tracking_parameters(self):
        url = "https://reuters.com/article/nvidia?utm_source=twitter&id=123"
        self.assertEqual(canonicalize_url(url), "https://reuters.com/article/nvidia?id=123")

    def test_strips_www_and_lowercases_host(self):
        self.assertEqual(canonicalize_url("https://WWW.Reuters.com/x"), "https://reuters.com/x")

    def test_strips_fragment_and_trailing_slash(self):
        self.assertEqual(canonicalize_url("https://reuters.com/x/#section"), "https://reuters.com/x")

    def test_none_and_garbage_return_none(self):
        self.assertIsNone(canonicalize_url(None))
        self.assertIsNone(canonicalize_url(""))
        self.assertIsNone(canonicalize_url("not-a-url"))

    def test_two_urls_differing_only_by_tracking_are_equal(self):
        a = canonicalize_url("https://cnbc.com/story?utm_campaign=a")
        b = canonicalize_url("https://www.cnbc.com/story/?fbclid=xyz")
        self.assertEqual(a, b)


class TestFingerprints(unittest.TestCase):
    def test_normalize_title_removes_punctuation_and_case(self):
        self.assertEqual(normalize_title("Nvidia's Q2: Record Revenue!"), "nvidia s q2 record revenue")

    def test_fingerprint_same_for_same_title_source_day(self):
        later_same_day = PUB + timedelta(hours=5)
        fp1 = compute_fingerprint("Nvidia beats", "Reuters", PUB)
        fp2 = compute_fingerprint("NVIDIA BEATS!", "reuters", later_same_day)
        self.assertEqual(fp1, fp2)

    def test_fingerprint_differs_across_days(self):
        fp1 = compute_fingerprint("Nvidia beats", "Reuters", PUB)
        fp2 = compute_fingerprint("Nvidia beats", "Reuters", PUB + timedelta(days=1))
        self.assertNotEqual(fp1, fp2)

    def test_fingerprint_differs_across_sources(self):
        self.assertNotEqual(
            compute_fingerprint("Nvidia beats", "Reuters", PUB),
            compute_fingerprint("Nvidia beats", "CNBC", PUB),
        )

    def test_empty_title_yields_no_fingerprint(self):
        self.assertIsNone(compute_fingerprint("", "Reuters", PUB))
        self.assertIsNone(compute_content_fingerprint(None, None))


class TestJaccardSimilarity(unittest.TestCase):
    def test_identical_articles_score_one(self):
        a, b = make_article("a"), make_article("b")
        self.assertEqual(jaccard_similarity(a, b), 1.0)

    def test_completely_different_articles_score_low(self):
        a = make_article("a", title="Nvidia reports record revenue", summary="AI chips demand")
        b = make_article("b", title="Oil prices fall on supply glut", summary="OPEC output rises")
        self.assertLess(jaccard_similarity(a, b), 0.2)

    def test_empty_content_scores_zero_not_error(self):
        a = make_article("a", title="", summary="")
        b = make_article("b")
        self.assertEqual(jaccard_similarity(a, b), 0.0)

    def test_short_numeric_tokens_are_kept_and_distinguish_quarters(self):
        """
        Regression guard: dropping short tokens made "Q2 results" and
        "Q3 results" tokenize identically, so two genuinely different
        quarterly reports scored 1.0 similarity and would have been
        wrongly merged. Digit-bearing tokens must survive tokenization.
        """
        q2 = make_article("a", title="Nvidia Q2 results beat estimates", summary="Revenue rose.")
        q3 = make_article("b", title="Nvidia Q3 results beat estimates", summary="Revenue rose.")
        self.assertLess(jaccard_similarity(q2, q3), 1.0)

    def test_differing_percentages_are_distinguishable(self):
        a = make_article("a", title="Shares climb 5% after earnings", summary="Investors reacted.")
        b = make_article("b", title="Shares climb 9% after earnings", summary="Investors reacted.")
        self.assertLess(jaccard_similarity(a, b), 1.0)


class TestSpecRequiredScenarios(unittest.TestCase):
    """The exact scenarios spec §21 requires simulating."""

    def setUp(self):
        self.engine = DeduplicationEngine()

    def test_same_article_from_same_provider_multiple_times(self):
        original = make_article("a1", provider="finnhub", provider_article_id="fh-999")
        repeat = make_article("a2", provider="finnhub", provider_article_id="fh-999")
        match, level = self.engine.find_duplicate(repeat, [original])
        self.assertEqual(match.article_id, "a1")
        self.assertEqual(level, DuplicateMatchLevel.PROVIDER_ID)

    def test_same_article_from_different_providers_matches_on_url(self):
        original = make_article("a1", provider="rss", source_url="https://reuters.com/nvidia-q2")
        other_provider = make_article("a2", provider="finnhub", provider_article_id="fh-1",
                                       source_url="https://www.reuters.com/nvidia-q2?utm_source=x")
        match, level = self.engine.find_duplicate(other_provider, [original])
        self.assertEqual(match.article_id, "a1")
        self.assertEqual(level, DuplicateMatchLevel.CANONICAL_URL)

    def test_two_similar_but_genuinely_different_articles_are_not_merged(self):
        a = make_article("a1", title="Nvidia reports record Q2 revenue",
                          summary="Nvidia beat estimates driven by data center sales growth.")
        b = make_article("a2", title="AMD reports weaker Q2 revenue",
                          summary="AMD missed estimates as client segment sales declined sharply.")
        match, level = self.engine.find_duplicate(b, [a])
        self.assertIsNone(match)
        self.assertEqual(level, DuplicateMatchLevel.NONE)

    def test_syndicated_copy_with_different_id_and_url_matches_on_title(self):
        original = make_article("a1", provider="rss", provider_article_id="r-1",
                                 source_url="https://reuters.com/a")
        syndicated = make_article("a2", provider="rss", provider_article_id="r-2",
                                   source_url="https://reuters.com/b")
        match, level = self.engine.find_duplicate(syndicated, [original])
        self.assertEqual(match.article_id, "a1")
        self.assertEqual(level, DuplicateMatchLevel.TITLE_SOURCE_TIME)

    def test_lightly_rewritten_copy_matches_on_content_similarity(self):
        original = make_article(
            "a1", source_name="Reuters", title="Nvidia posts record quarterly revenue on AI demand",
            summary="The chipmaker reported stronger than expected data center revenue growth this quarter.",
        )
        rewritten = make_article(
            "a2", source_name="CNBC", title="Nvidia posts record quarterly revenue amid AI demand",
            summary="The chipmaker reported stronger than expected data center revenue growth this quarter.",
        )
        match, level = self.engine.find_duplicate(rewritten, [original])
        self.assertEqual(match.article_id, "a1")
        self.assertEqual(level, DuplicateMatchLevel.CONTENT_SIMILARITY)


class TestDeduplicationEngineBehaviour(unittest.TestCase):
    def setUp(self):
        self.engine = DeduplicationEngine()

    def test_article_never_matches_itself(self):
        a = make_article("a1", provider_article_id="x")
        match, level = self.engine.find_duplicate(a, [a])
        self.assertIsNone(match)

    def test_empty_candidate_set_returns_no_match(self):
        match, level = self.engine.find_duplicate(make_article(), [])
        self.assertIsNone(match)
        self.assertEqual(level, DuplicateMatchLevel.NONE)

    def test_mark_if_duplicate_sets_fields_but_keeps_article(self):
        original = make_article("a1", provider_article_id="x")
        dupe = make_article("a2", provider_article_id="x")
        result = self.engine.mark_if_duplicate(dupe, [original])
        self.assertEqual(result.duplicate_of, "a1")
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.article_id, "a2")  # article itself is preserved, not discarded

    def test_duplicate_chains_point_to_the_original_not_another_duplicate(self):
        original = make_article("a1", provider_article_id="x")
        first_dupe = make_article("a2", provider_article_id="x")
        self.engine.mark_if_duplicate(first_dupe, [original])

        second_dupe = make_article("a3", provider_article_id="x")
        self.engine.mark_if_duplicate(second_dupe, [first_dupe])
        # Points at the ORIGINAL (a1), not at the intermediate duplicate (a2).
        self.assertEqual(second_dupe.duplicate_of, "a1")

    def test_non_duplicate_is_left_untouched(self):
        a = make_article("a1", title="Nvidia revenue", provider_article_id="x")
        b = make_article("a2", title="Oil prices collapse on oversupply", summary="OPEC raises output",
                          provider_article_id="y", source_url="https://other.com/z")
        result = self.engine.mark_if_duplicate(b, [a])
        self.assertIsNone(result.duplicate_of)
        self.assertEqual(result.duplicate_match_level, DuplicateMatchLevel.NONE)

    def test_higher_threshold_makes_engine_stricter(self):
        strict = DeduplicationEngine(similarity_threshold=0.99)
        a = make_article("a1", source_name="Reuters", title="Nvidia posts record revenue on AI demand growth",
                          summary="Data center sales rose sharply.")
        b = make_article("a2", source_name="CNBC", title="Nvidia posts record revenue amid AI demand surge",
                          summary="Data center sales climbed sharply.")
        match, _ = strict.find_duplicate(b, [a])
        self.assertIsNone(match)


if __name__ == "__main__":
    unittest.main(verbosity=2)
