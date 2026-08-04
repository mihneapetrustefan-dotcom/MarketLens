"""
test_company_detector.py
---------------------------
Unit tests for Company Detector v1 (company_detector.py).

TESTING STRATEGY:
Most tests use a SMALL, CONTROLLED registry injected via the
constructor, rather than the real COMPANY_REGISTRY — this isolates the
matching logic itself from the specific companies we happen to have
curated, so tests won't break if the real registry grows or changes.
A few tests at the end also sanity-check against the real registry.
"""

import unittest

from company_detector import CompanyDetector


TEST_REGISTRY = [
    {"canonical_name": "Tesla", "aliases": ["Tesla"], "ticker": "TSLA", "category": "stocks"},
    {"canonical_name": "OMV Petrom", "aliases": ["OMV Petrom", "Petrom"], "ticker": "SNP", "category": "bvb"},
    {"canonical_name": "Banca Transilvania", "aliases": ["Banca Transilvania", "BT"], "ticker": "TLV", "category": "bvb"},
    {"canonical_name": "Meta Platforms", "aliases": ["Meta", "Facebook"], "ticker": "META", "category": "stocks"},
    {"canonical_name": "Bitcoin", "aliases": ["Bitcoin", "BTC"], "ticker": "BTC", "category": "crypto"},
]


def make_article(**overrides):
    base = {
        "article_id": "id-0",
        "title": "Default headline",
        "summary": "",
        "url": "https://example.com/x",
        "source": "TestSource",
        "category": "stocks",
    }
    base.update(overrides)
    return base


class TestDetectInText(unittest.TestCase):
    """Tests for detect_in_text(): the core matching logic."""

    def setUp(self):
        self.detector = CompanyDetector(registry=TEST_REGISTRY)

    def test_finds_company_by_canonical_name(self):
        results = self.detector.detect_in_text("Tesla stock rises after earnings")
        names = {r["company"] for r in results}
        self.assertIn("Tesla", names)

    def test_finds_company_via_alias(self):
        results = self.detector.detect_in_text("Petrom reports higher profit this quarter")
        names = {r["company"] for r in results}
        self.assertIn("OMV Petrom", names)  # matched via the "Petrom" alias

    def test_returns_ticker_and_category(self):
        results = self.detector.detect_in_text("Tesla unveils new factory")
        tesla = next(r for r in results if r["company"] == "Tesla")
        self.assertEqual(tesla["ticker"], "TSLA")
        self.assertEqual(tesla["category"], "stocks")

    def test_word_boundary_avoids_partial_word_match(self):
        # "Teslas" must NOT match the "Tesla" alias — no word boundary
        # exists between "Tesla" and the trailing "s".
        results = self.detector.detect_in_text("Several Teslas were spotted downtown")
        names = {r["company"] for r in results}
        self.assertNotIn("Tesla", names)

    def test_short_alias_is_case_sensitive(self):
        # "BT" (short alias) must match only in its exact capitalization.
        results_upper = self.detector.detect_in_text("BT announces record profits")
        results_lower = self.detector.detect_in_text("bt announces record profits")
        self.assertIn("Banca Transilvania", {r["company"] for r in results_upper})
        self.assertNotIn("Banca Transilvania", {r["company"] for r in results_lower})

    def test_short_alias_meta_does_not_match_generic_lowercase_word(self):
        # "Meta" (len 4, case-sensitive bucket) must not match the
        # common English word "meta" used generically, unrelated to the
        # company Meta Platforms.
        results = self.detector.detect_in_text("This is a meta description of the page")
        names = {r["company"] for r in results}
        self.assertNotIn("Meta Platforms", names)

    def test_short_alias_meta_matches_capitalized_form(self):
        results = self.detector.detect_in_text("Meta announces new VR headset")
        names = {r["company"] for r in results}
        self.assertIn("Meta Platforms", names)

    def test_multiple_distinct_companies_detected_together(self):
        results = self.detector.detect_in_text("Tesla and Bitcoin both rallied today")
        names = {r["company"] for r in results}
        self.assertEqual(names, {"Tesla", "Bitcoin"})

    def test_company_matched_via_two_aliases_reported_once(self):
        results = self.detector.detect_in_text("Meta, formerly known as Facebook, reported earnings")
        matches = [r for r in results if r["company"] == "Meta Platforms"]
        self.assertEqual(len(matches), 1)  # not duplicated despite 2 aliases present

    def test_no_match_returns_empty_list(self):
        results = self.detector.detect_in_text("A quiet day with no notable market news")
        self.assertEqual(results, [])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self.detector.detect_in_text(""), [])


class TestDetectInArticle(unittest.TestCase):
    """Tests for detect_in_article(): applies detection to a full article dict."""

    def setUp(self):
        self.detector = CompanyDetector(registry=TEST_REGISTRY)

    def test_combines_title_and_summary(self):
        article = make_article(title="Markets react", summary="Tesla shares jump 5%")
        tagged = self.detector.detect_in_article(article)
        names = {c["company"] for c in tagged["companies_mentioned"]}
        self.assertIn("Tesla", names)

    def test_does_not_mutate_original_article(self):
        article = make_article(title="Tesla stock update")
        self.detector.detect_in_article(article)
        self.assertNotIn("companies_mentioned", article)  # original untouched

    def test_article_with_no_company_gets_empty_list(self):
        article = make_article(title="Weather forecast for the weekend", summary="")
        tagged = self.detector.detect_in_article(article)
        self.assertEqual(tagged["companies_mentioned"], [])


class TestDetectBatch(unittest.TestCase):
    """Tests for detect_batch(): the full-list orchestration method."""

    def setUp(self):
        self.detector = CompanyDetector(registry=TEST_REGISTRY)

    def test_tags_every_article_in_batch(self):
        articles = [
            make_article(article_id="1", title="Tesla unveils new model"),
            make_article(article_id="2", title="Local weather stays mild"),
            make_article(article_id="3", title="Bitcoin surges past key level"),
        ]
        result = self.detector.detect_batch(articles)
        self.assertEqual(len(result), 3)
        self.assertTrue(all("companies_mentioned" in a for a in result))

        tesla_article = next(a for a in result if a["article_id"] == "1")
        self.assertEqual(tesla_article["companies_mentioned"][0]["company"], "Tesla")

        weather_article = next(a for a in result if a["article_id"] == "2")
        self.assertEqual(weather_article["companies_mentioned"], [])

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.detector.detect_batch([]), [])


class TestRealRegistrySanityCheck(unittest.TestCase):
    """A few checks against the ACTUAL production registry, not the test one."""

    def setUp(self):
        self.detector = CompanyDetector()  # uses real COMPANY_REGISTRY

    def test_detects_a_bvb_company(self):
        results = self.detector.detect_in_text("Hidroelectrica anunta rezultate financiare")
        names = {r["company"] for r in results}
        self.assertIn("Hidroelectrica", names)

    def test_detects_a_crypto_asset(self):
        results = self.detector.detect_in_text("Bitcoin climbs above key resistance level")
        names = {r["company"] for r in results}
        self.assertIn("Bitcoin", names)

    def test_detects_an_international_stock(self):
        results = self.detector.detect_in_text("Nvidia reports record quarterly revenue")
        names = {r["company"] for r in results}
        self.assertIn("Nvidia", names)

    def test_detects_newly_added_industrials_company(self):
        results = self.detector.detect_in_text("Boeing delivers new aircraft to airline customers")
        names = {r["company"] for r in results}
        self.assertIn("Boeing", names)

    def test_detects_newly_added_healthcare_company(self):
        results = self.detector.detect_in_text("Pfizer announces new vaccine trial results")
        names = {r["company"] for r in results}
        self.assertIn("Pfizer", names)

    def test_detects_newly_added_crypto_asset(self):
        results = self.detector.detect_in_text("Solana price rallies after network upgrade")
        names = {r["company"] for r in results}
        self.assertIn("Solana", names)


class TestKnownAmbiguities(unittest.TestCase):
    """
    Documents known, ACCEPTED false-positive risks in the real
    registry — tracked here deliberately so a future change to the
    registry or matching rules that alters this behavior is a visible,
    intentional decision, not a silent regression.
    """

    def setUp(self):
        self.detector = CompanyDetector()

    def test_lowercase_blockchain_oracle_term_is_falsely_matched_as_oracle_corp(self):
        # KNOWN AMBIGUITY: "Oracle" (6 chars, case-insensitive match)
        # collides with the generic blockchain term "oracle" (a data
        # feed for smart contracts). This assertion documents the
        # CURRENT (accepted) behavior, not a desired outcome.
        results = self.detector.detect_in_text("Chainlink partners with major banks on oracle infrastructure")
        names = {r["company"] for r in results}
        self.assertIn("Oracle", names)  # documents the false positive, not endorses it


if __name__ == "__main__":
    unittest.main(verbosity=2)
