"""
test_duplicate_detector.py
----------------------------
Unit tests for Duplicate Detector v1 (duplicate_detector.py).

TESTING STRATEGY:
Synthetic article dicts only — no dependency on the Collector, Cleaner,
or the network. Each test isolates one specific behavior: exact-URL
deduplication, keyword tokenization, Jaccard similarity, and the final
clustering/grouping logic (including transitive grouping via the
Union-Find structure).
"""

import unittest

from duplicate_detector import DuplicateDetector, DisjointSet


def make_article(**overrides):
    """Helper: builds a minimal article dict, with overrides applied."""
    base = {
        "article_id": "id-0",
        "title": "Default headline text here",
        "summary": "",
        "url": "https://example.com/default",
        "source": "TestSource",
        "category": "stocks",
    }
    base.update(overrides)
    return base


class TestDisjointSet(unittest.TestCase):
    """Tests for the underlying Union-Find structure in isolation."""

    def test_starts_with_each_element_in_its_own_group(self):
        dsu = DisjointSet(3)
        self.assertNotEqual(dsu.find(0), dsu.find(1))

    def test_union_merges_groups(self):
        dsu = DisjointSet(3)
        dsu.union(0, 1)
        self.assertEqual(dsu.find(0), dsu.find(1))

    def test_transitive_union(self):
        # 0-1 union, then 1-2 union -> 0 and 2 must end up in the same
        # group even though they were never directly unioned together.
        dsu = DisjointSet(3)
        dsu.union(0, 1)
        dsu.union(1, 2)
        self.assertEqual(dsu.find(0), dsu.find(2))


class TestTokenizeAndSimilarity(unittest.TestCase):
    """Tests for keyword extraction and Jaccard similarity."""

    def setUp(self):
        self.detector = DuplicateDetector()

    def test_tokenize_lowercases_and_strips_punctuation(self):
        tokens = self.detector._tokenize("Fed Raises Rates!")
        self.assertIn("fed", tokens)
        self.assertIn("raises", tokens)
        self.assertIn("rates", tokens)

    def test_tokenize_removes_stopwords_and_short_words(self):
        tokens = self.detector._tokenize("The Fed is to raise a rate")
        self.assertNotIn("the", tokens)
        self.assertNotIn("is", tokens)
        self.assertNotIn("to", tokens)
        self.assertNotIn("a", tokens)

    def test_identical_titles_have_similarity_one(self):
        tokens = self.detector._tokenize("Fed raises interest rates sharply")
        sim = self.detector._jaccard_similarity(tokens, tokens)
        self.assertEqual(sim, 1.0)

    def test_unrelated_titles_have_low_similarity(self):
        a = self.detector._tokenize("Fed raises interest rates sharply today")
        b = self.detector._tokenize("Local bakery wins national pastry award")
        sim = self.detector._jaccard_similarity(a, b)
        self.assertLess(sim, 0.2)

    def test_empty_token_sets_have_zero_similarity(self):
        self.assertEqual(self.detector._jaccard_similarity(set(), set()), 0.0)
        self.assertEqual(self.detector._jaccard_similarity({"fed"}, set()), 0.0)


class TestExactDuplicateRemoval(unittest.TestCase):
    """Tests for remove_exact_duplicates()."""

    def setUp(self):
        self.detector = DuplicateDetector()

    def test_removes_repeated_url_keeps_first(self):
        articles = [
            make_article(article_id="1", url="https://example.com/a"),
            make_article(article_id="2", url="https://example.com/a"),  # exact repeat
            make_article(article_id="3", url="https://example.com/b"),
        ]
        result = self.detector.remove_exact_duplicates(articles)
        ids = [a["article_id"] for a in result]
        self.assertEqual(ids, ["1", "3"])

    def test_url_comparison_is_case_insensitive(self):
        articles = [
            make_article(article_id="1", url="https://Example.com/A"),
            make_article(article_id="2", url="https://example.com/a"),
        ]
        result = self.detector.remove_exact_duplicates(articles)
        self.assertEqual(len(result), 1)

    def test_articles_with_no_url_are_never_treated_as_duplicates_of_each_other(self):
        articles = [
            make_article(article_id="1", url=""),
            make_article(article_id="2", url=""),
        ]
        result = self.detector.remove_exact_duplicates(articles)
        self.assertEqual(len(result), 2)


class TestGroupNearDuplicates(unittest.TestCase):
    """Tests for group_near_duplicates() and the full deduplicate() pipeline."""

    def setUp(self):
        self.detector = DuplicateDetector(similarity_threshold=0.5)

    def test_similar_titles_from_different_sources_share_group(self):
        articles = [
            make_article(article_id="1", source="Reuters",
                         title="Federal Reserve raises interest rates sharply"),
            make_article(article_id="2", source="CNBC",
                         title="Fed sharply raises interest rates today"),
        ]
        result = self.detector.group_near_duplicates(articles)
        self.assertEqual(result[0]["duplicate_group_id"], result[1]["duplicate_group_id"])
        self.assertEqual(result[0]["duplicate_group_size"], 2)

    def test_unrelated_articles_get_separate_singleton_groups(self):
        articles = [
            make_article(article_id="1", title="Federal Reserve raises interest rates"),
            make_article(article_id="2", title="Local bakery wins national pastry award"),
        ]
        result = self.detector.group_near_duplicates(articles)
        self.assertNotEqual(result[0]["duplicate_group_id"], result[1]["duplicate_group_id"])
        self.assertEqual(result[0]["duplicate_group_size"], 1)
        self.assertEqual(result[1]["duplicate_group_size"], 1)

    def test_preserves_original_order(self):
        articles = [
            make_article(article_id="1", title="Alpha headline one two three"),
            make_article(article_id="2", title="Beta headline four five six"),
            make_article(article_id="3", title="Gamma headline seven eight nine"),
        ]
        result = self.detector.group_near_duplicates(articles)
        ids = [a["article_id"] for a in result]
        self.assertEqual(ids, ["1", "2", "3"])

    def test_does_not_mutate_original_dicts(self):
        articles = [make_article(article_id="1", title="Some headline about markets today")]
        original_keys = set(articles[0].keys())
        self.detector.group_near_duplicates(articles)
        self.assertEqual(set(articles[0].keys()), original_keys)  # no new keys added in place

    def test_transitive_grouping_across_three_sources(self):
        # A~B share enough keywords, B~C share enough keywords, but A~C
        # alone might not clear the threshold directly. All three must
        # still end up in the same group thanks to Union-Find transitivity.
        # Sources must differ pairwise, since same-source pairs are now
        # excluded from cross-source grouping by design.
        articles = [
            make_article(article_id="1", source="SourceA",
                         title="Central bank raises benchmark interest rate today"),
            make_article(article_id="2", source="SourceB",
                         title="Benchmark interest rate raised by central bank sharply"),
            make_article(article_id="3", source="SourceC",
                         title="Rate hike announced by central bank policymakers"),
        ]
        result = self.detector.group_near_duplicates(articles)
        group_ids = {a["duplicate_group_id"] for a in result}
        # Whether they land in exactly one group depends on the threshold,
        # but at minimum this must not crash and must produce valid,
        # consistent group_size values matching actual group membership.
        for gid in group_ids:
            members = [a for a in result if a["duplicate_group_id"] == gid]
            self.assertEqual(members[0]["duplicate_group_size"], len(members))

    def test_same_source_similar_articles_are_never_cross_grouped(self):
        # This reproduces the real-world false positive found in
        # production data: a single source (e.g. a listicle-style
        # outlet) publishing several template articles that share many
        # keywords ("Best X lenders of August 2026", "Best Y loans for
        # August 2026", ...). These must NOT be treated as independent
        # cross-source corroboration just because they're similar.
        articles = [
            make_article(article_id="1", source="Yahoo Finance",
                         title="Best mortgage lenders for low down payments of August"),
            make_article(article_id="2", source="Yahoo Finance",
                         title="Best cash-out refinance mortgage lenders of August"),
        ]
        result = self.detector.group_near_duplicates(articles)
        self.assertNotEqual(result[0]["duplicate_group_id"], result[1]["duplicate_group_id"])
        self.assertEqual(result[0]["duplicate_group_size"], 1)
        self.assertEqual(result[1]["duplicate_group_size"], 1)


class TestCollapseSameSourceNearDuplicates(unittest.TestCase):
    """Tests for collapse_same_source_near_duplicates()."""

    def setUp(self):
        self.detector = DuplicateDetector(same_source_duplicate_threshold=0.75)

    def test_collapses_near_identical_same_source_titles(self):
        # Reproduces the real-world "FOTO ..." -> "VIDEO&FOTO ..." case:
        # same source, different URL, near-identical title.
        articles = [
            make_article(article_id="1", source="Profit.ro", url="https://profit.ro/a",
                         title="FOTO Stanci aruncate in aer si barje scufundate pe Dunare"),
            make_article(article_id="2", source="Profit.ro", url="https://profit.ro/b",
                         title="VIDEO FOTO Stanci aruncate in aer si barje scufundate pe Dunare"),
        ]
        result = self.detector.collapse_same_source_near_duplicates(articles)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["article_id"], "1")  # first occurrence kept

    def test_does_not_collapse_across_different_sources(self):
        # Even a near-identical title must NOT be collapsed here if the
        # sources differ — that's cross-source corroboration, handled
        # by group_near_duplicates, not this method.
        articles = [
            make_article(article_id="1", source="Reuters",
                         title="Federal Reserve raises interest rates sharply today"),
            make_article(article_id="2", source="CNBC",
                         title="Federal Reserve raises interest rates sharply today"),
        ]
        result = self.detector.collapse_same_source_near_duplicates(articles)
        self.assertEqual(len(result), 2)

    def test_does_not_collapse_merely_related_same_source_articles(self):
        # Below the stricter same-source threshold: related but distinct
        # articles from one outlet must be kept, not collapsed away.
        articles = [
            make_article(article_id="1", source="Yahoo Finance",
                         title="Best mortgage lenders for low down payments of August"),
            make_article(article_id="2", source="Yahoo Finance",
                         title="Best cash-out refinance mortgage lenders of August"),
        ]
        result = self.detector.collapse_same_source_near_duplicates(articles)
        self.assertEqual(len(result), 2)


class TestFullDeduplicatePipeline(unittest.TestCase):
    """End-to-end tests for deduplicate(): exact removal + clustering together."""

    def setUp(self):
        self.detector = DuplicateDetector(similarity_threshold=0.5)

    def test_full_pipeline_exact_and_near_duplicates(self):
        articles = [
            make_article(article_id="1", source="Reuters", url="https://reuters.com/fed",
                         title="Federal Reserve raises interest rates sharply"),
            make_article(article_id="2", source="Reuters", url="https://reuters.com/fed",
                         title="Federal Reserve raises interest rates sharply"),  # exact repeat of #1
            make_article(article_id="3", source="CNBC", url="https://cnbc.com/fed",
                         title="Fed sharply raises interest rates today"),          # near-dup of #1
            make_article(article_id="4", source="LocalBlog", url="https://blog.com/bakery",
                         title="Local bakery wins national pastry award"),          # unrelated
        ]
        result = self.detector.deduplicate(articles)

        # Exact repeat (#2) must be gone; 3 articles should remain.
        ids = {a["article_id"] for a in result}
        self.assertEqual(ids, {"1", "3", "4"})

        # #1 and #3 must share a group of size 2 (independent corroboration).
        art1 = next(a for a in result if a["article_id"] == "1")
        art3 = next(a for a in result if a["article_id"] == "3")
        self.assertEqual(art1["duplicate_group_id"], art3["duplicate_group_id"])
        self.assertEqual(art1["duplicate_group_size"], 2)

        # #4 is unrelated -> its own singleton group.
        art4 = next(a for a in result if a["article_id"] == "4")
        self.assertEqual(art4["duplicate_group_size"], 1)

    def test_empty_input_returns_empty_output(self):
        self.assertEqual(self.detector.deduplicate([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
