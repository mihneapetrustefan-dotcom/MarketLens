"""
test_source_credibility.py
------------------------------
Unit tests for Source Credibility v1.
"""

import unittest
from collections import Counter

from source_credibility import (
    CHANNEL_TIERS, SOURCE_TIERS, TIER_ORDER, get_source_tier,
    normalize_tier, summarize_sources,
)


def make_article(source):
    return {"source": source, "title": "x"}


class TestGetSourceTier(unittest.TestCase):
    def test_known_official_source(self):
        self.assertEqual(get_source_tier("Federal Reserve Press Releases"), "official")

    def test_known_wire_source(self):
        self.assertEqual(get_source_tier("Reuters"), "wire_and_major_press")

    def test_known_specialized_source(self):
        self.assertEqual(get_source_tier("CoinDesk"), "specialized_or_aggregator")

    def test_unknown_source_is_unclassified(self):
        self.assertEqual(get_source_tier("Some New Blog"), "unclassified")

    def test_none_source_is_unclassified(self):
        self.assertEqual(get_source_tier(None), "unclassified")

    def test_empty_string_source_is_unclassified(self):
        self.assertEqual(get_source_tier(""), "unclassified")


class TestChannelTiering(unittest.TestCase):
    """
    83% of this corpus arrives as "Google News: <Company>".

    Those are not 385 unknown publishers -- they are one aggregator
    queried 385 ways. Reporting them as unclassified was accurate about
    the NAME and wrong about the SOURCE, and it pinned the extractor's
    source_quality component to its 0.4 floor for every event ever
    stored.
    """

    def test_a_google_news_feed_is_an_aggregator(self):
        self.assertEqual(get_source_tier("Google News: Apple"),
                         "specialized_or_aggregator")

    def test_every_google_news_feed_resolves_regardless_of_company(self):
        for company in ("Apple", "Rivian Automotive", "Procter & Gamble",
                        "Bristol Myers Squibb", "ASML Holding"):
            self.assertEqual(get_source_tier(f"Google News: {company}"),
                             "specialized_or_aggregator", company)

    def test_an_exact_match_beats_the_channel_rule(self):
        """
        A named publisher keeps its own tier. The channel rule is a
        fallback for retrieval paths that hide the publisher, not an
        override of what we actually know.
        """
        self.assertEqual(get_source_tier("Reuters"), "wire_and_major_press")

    def test_an_aggregator_tier_is_the_CEILING_not_the_publisher_tier(self):
        """
        Google News does not say who wrote the piece, so wire-service
        quality cannot be claimed even when the underlying article came
        from one. Asserted so nobody later "improves" this to 0.8.
        """
        self.assertNotEqual(get_source_tier("Google News: Reuters"),
                            "wire_and_major_press")
        self.assertEqual(get_source_tier("Google News: Reuters"),
                         "specialized_or_aggregator")

    def test_a_source_merely_containing_the_prefix_is_not_matched(self):
        self.assertEqual(get_source_tier("Not Google News: Something"),
                         "unclassified")

    def test_the_channel_table_is_not_empty(self):
        self.assertTrue(CHANNEL_TIERS)


class TestTierAliases(unittest.TestCase):
    """
    The `news_sources` table spells one tier with "or", this module
    with "and". The same tier resolved through one path and fell
    through to "unclassified" on the other.
    """

    def test_the_or_spelling_normalises_to_the_and_spelling(self):
        self.assertEqual(normalize_tier("wire_or_major_press"),
                         "wire_and_major_press")

    def test_a_canonical_tier_is_unchanged(self):
        for tier in TIER_ORDER:
            self.assertEqual(normalize_tier(tier), tier)

    def test_none_normalises_to_unclassified(self):
        self.assertEqual(normalize_tier(None), "unclassified")

    def test_the_extractor_accepts_both_spellings(self):
        """
        Falling through to the 0.4 unclassified default is silent and
        looks exactly like a genuinely unknown source, so the extractor
        accepts both rather than trusting every caller to normalise.
        """
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from src.events.extractor import _SOURCE_QUALITY_BY_TIER
        self.assertEqual(_SOURCE_QUALITY_BY_TIER["wire_or_major_press"],
                         _SOURCE_QUALITY_BY_TIER["wire_and_major_press"])


class TestSummarizeSources(unittest.TestCase):
    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(summarize_sources([]), [])

    def test_single_tier_summary(self):
        articles = [make_article("Reuters"), make_article("Reuters"), make_article("CNBC")]
        summary = summarize_sources(articles)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["tier"], "wire_and_major_press")
        self.assertEqual(summary[0]["article_count"], 3)

    def test_per_source_breakdown_within_tier(self):
        articles = [make_article("Reuters"), make_article("Reuters"), make_article("CNBC")]
        summary = summarize_sources(articles)
        sources = {s["name"]: s["article_count"] for s in summary[0]["sources"]}
        self.assertEqual(sources["Reuters"], 2)
        self.assertEqual(sources["CNBC"], 1)

    def test_multiple_tiers_ordered_correctly(self):
        articles = [
            make_article("Decrypt"),                              # specialized
            make_article("Federal Reserve Press Releases"),        # official
            make_article("Reuters"),                                # wire
        ]
        summary = summarize_sources(articles)
        tiers = [s["tier"] for s in summary]
        self.assertEqual(tiers, ["official", "wire_and_major_press", "specialized_or_aggregator"])

    def test_unclassified_sources_grouped_together(self):
        articles = [make_article("Random Blog A"), make_article("Random Blog B")]
        summary = summarize_sources(articles)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["tier"], "unclassified")
        self.assertEqual(summary[0]["article_count"], 2)

    def test_missing_source_field_counted_as_unclassified(self):
        articles = [{"title": "no source field"}]
        summary = summarize_sources(articles)
        self.assertEqual(summary[0]["tier"], "unclassified")

    def test_tier_with_zero_articles_omitted(self):
        articles = [make_article("Reuters")]
        summary = summarize_sources(articles)
        tiers_present = {s["tier"] for s in summary}
        self.assertNotIn("official", tiers_present)
        self.assertNotIn("specialized_or_aggregator", tiers_present)


class TestRegistryIntegrity(unittest.TestCase):
    def test_no_source_mapped_to_invalid_tier(self):
        valid_tiers = set(TIER_ORDER)
        for source, tier in SOURCE_TIERS.items():
            self.assertIn(tier, valid_tiers, f"'{source}' mapped to invalid tier '{tier}'")

    def test_no_accidental_case_variant_duplicates(self):
        names_lower = [n.lower() for n in SOURCE_TIERS.keys()]
        dupes = [name for name, count in Counter(names_lower).items() if count > 1]
        self.assertEqual(dupes, [])

    def test_known_aliases_for_same_outlet_map_to_same_tier(self):
        # "CNBC Top News" and "CNBC" are both present on purpose (RSS
        # feed display name vs. a shorter name that might appear via
        # Finnhub/Alpha Vantage's own source field) — same for
        # MarketWatch and Investing.com. Confirm both forms map to the
        # same tier, so which one an API happens to use doesn't matter.
        self.assertEqual(SOURCE_TIERS["CNBC Top News"], SOURCE_TIERS["CNBC"])
        self.assertEqual(SOURCE_TIERS["MarketWatch Top Stories"], SOURCE_TIERS["MarketWatch"])
        self.assertEqual(SOURCE_TIERS["Investing.com Stock Market News"], SOURCE_TIERS["Investing.com"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
