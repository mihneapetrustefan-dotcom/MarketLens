"""
test_sector_detector.py
--------------------------
Unit tests for Sector Detector v1 (sector_detector.py).

TESTING STRATEGY:
Uses a small, controlled company-sector map and keyword set injected
via the constructor, isolating the classification logic from the real,
larger registry. A few tests at the end sanity-check against the real
COMPANY_SECTOR_MAP / SECTOR_KEYWORDS.
"""

import unittest

from sector_detector import SectorDetector


TEST_COMPANY_SECTOR_MAP = {
    "Tesla": "Automotive",
    "Banca Transilvania": "Financial Services",
    "BRD": "Financial Services",
    "Hidroelectrica": "Energy",
}

TEST_SECTOR_KEYWORDS = {
    "Energy": ["oil prices", "crude oil"],
    "Financial Services": ["interest rate", "central bank"],
}


def make_article(**overrides):
    base = {
        "article_id": "id-0",
        "title": "Default headline",
        "summary": "",
        "companies_mentioned": [],
    }
    base.update(overrides)
    return base


def make_company(name, ticker="X", category="stocks"):
    return {"company": name, "ticker": ticker, "category": category, "matched_alias": name}


class TestCompanyBasedDetection(unittest.TestCase):
    """Tests for the high-confidence, company-based sector path."""

    def setUp(self):
        self.detector = SectorDetector(
            company_sector_map=TEST_COMPANY_SECTOR_MAP,
            sector_keywords=TEST_SECTOR_KEYWORDS,
        )

    def test_maps_known_company_to_its_sector(self):
        article = make_article(companies_mentioned=[make_company("Tesla")])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertEqual(sectors, {"Automotive"})
        self.assertEqual(tagged["sectors"][0]["source"], "company")

    def test_multiple_companies_different_sectors(self):
        article = make_article(companies_mentioned=[
            make_company("Tesla"), make_company("Hidroelectrica"),
        ])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertEqual(sectors, {"Automotive", "Energy"})

    def test_two_companies_same_sector_reported_once(self):
        article = make_article(companies_mentioned=[
            make_company("Banca Transilvania"), make_company("BRD"),
        ])
        tagged = self.detector.detect_in_article(article)
        finance_matches = [s for s in tagged["sectors"] if s["sector"] == "Financial Services"]
        self.assertEqual(len(finance_matches), 1)

    def test_via_lists_every_contributing_company_not_just_first(self):
        # Reproduces a real transparency issue found in production data:
        # an article mentioning Alphabet, Amazon, and Microsoft (all
        # Technology except Amazon) showed the sector as coming from
        # only ONE company, hiding that multiple companies confirmed it.
        article = make_article(companies_mentioned=[
            make_company("Tesla"),
            make_company("BRD"),
            make_company("Banca Transilvania"),
        ])
        tagged = self.detector.detect_in_article(article)
        finance = next(s for s in tagged["sectors"] if s["sector"] == "Financial Services")
        self.assertEqual(set(finance["via"]), {"BRD", "Banca Transilvania"})

    def test_unknown_company_contributes_no_sector(self):
        article = make_article(companies_mentioned=[make_company("Unknown Corp")])
        tagged = self.detector.detect_in_article(article)
        self.assertEqual(tagged["sectors"], [])


class TestKeywordFallbackDetection(unittest.TestCase):
    """Tests for the lower-confidence, keyword-based fallback path."""

    def setUp(self):
        self.detector = SectorDetector(
            company_sector_map=TEST_COMPANY_SECTOR_MAP,
            sector_keywords=TEST_SECTOR_KEYWORDS,
        )

    def test_keyword_fallback_used_when_no_company_present(self):
        article = make_article(title="Oil prices surge amid supply concerns", companies_mentioned=[])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertEqual(sectors, {"Energy"})
        self.assertEqual(tagged["sectors"][0]["source"], "keyword")

    def test_company_based_sector_not_duplicated_by_matching_keyword(self):
        # Tesla already gives "Automotive" via company; if the text ALSO
        # happens to mention a Financial Services keyword, that sector
        # should be added too, but never duplicated for the same sector.
        article = make_article(
            title="Tesla comments on rising interest rate environment",
            companies_mentioned=[make_company("Tesla")],
        )
        tagged = self.detector.detect_in_article(article)
        sectors = [s["sector"] for s in tagged["sectors"]]
        self.assertEqual(sectors.count("Automotive"), 1)
        self.assertIn("Financial Services", sectors)

    def test_keyword_word_boundary_avoids_partial_phrase_match(self):
        # "crude oil painting" must not match "crude oil" as a phrase
        # continuing into unrelated words — but since we match "crude
        # oil" as a substring phrase with boundaries at each end, this
        # actually WOULD still match "crude oil" within "crude oil
        # painting" (the phrase itself is fully present). This test
        # instead confirms a phrase that only partially overlaps does
        # NOT match.
        article = make_article(title="Crude awakening: a documentary review", companies_mentioned=[])
        tagged = self.detector.detect_in_article(article)
        self.assertEqual(tagged["sectors"], [])

    def test_no_company_and_no_keyword_returns_empty_sectors(self):
        article = make_article(title="A quiet day with no notable news", companies_mentioned=[])
        tagged = self.detector.detect_in_article(article)
        self.assertEqual(tagged["sectors"], [])


class TestArticleHandling(unittest.TestCase):
    """Tests for robustness and the copy-don't-mutate discipline."""

    def setUp(self):
        self.detector = SectorDetector(
            company_sector_map=TEST_COMPANY_SECTOR_MAP,
            sector_keywords=TEST_SECTOR_KEYWORDS,
        )

    def test_missing_companies_mentioned_key_defaults_to_empty(self):
        article = {"article_id": "1", "title": "No companies key at all", "summary": ""}
        tagged = self.detector.detect_in_article(article)
        self.assertEqual(tagged["sectors"], [])

    def test_does_not_mutate_original_article(self):
        article = make_article(companies_mentioned=[make_company("Tesla")])
        self.detector.detect_in_article(article)
        self.assertNotIn("sectors", article)


class TestDetectBatch(unittest.TestCase):
    """Tests for detect_batch(): the full-list orchestration method."""

    def setUp(self):
        self.detector = SectorDetector(
            company_sector_map=TEST_COMPANY_SECTOR_MAP,
            sector_keywords=TEST_SECTOR_KEYWORDS,
        )

    def test_tags_every_article_in_batch(self):
        articles = [
            make_article(article_id="1", companies_mentioned=[make_company("Tesla")]),
            make_article(article_id="2", title="Nothing notable happened today"),
        ]
        result = self.detector.detect_batch(articles)
        self.assertTrue(all("sectors" in a for a in result))
        first = next(a for a in result if a["article_id"] == "1")
        self.assertEqual(first["sectors"][0]["sector"], "Automotive")
        second = next(a for a in result if a["article_id"] == "2")
        self.assertEqual(second["sectors"], [])

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.detector.detect_batch([]), [])


class TestRealRegistrySanityCheck(unittest.TestCase):
    """A few checks against the ACTUAL production sector data."""

    def setUp(self):
        self.detector = SectorDetector()  # uses real COMPANY_SECTOR_MAP / SECTOR_KEYWORDS

    def test_real_bvb_company_maps_to_energy(self):
        article = make_article(companies_mentioned=[make_company("Hidroelectrica")])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertIn("Energy", sectors)

    def test_real_crypto_company_maps_to_cryptocurrency(self):
        article = make_article(companies_mentioned=[make_company("Bitcoin")])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertIn("Cryptocurrency", sectors)

    def test_real_keyword_fallback_for_healthcare(self):
        article = make_article(title="New hospital opens amid healthcare system reforms")
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertIn("Healthcare", sectors)

    def test_newly_added_company_maps_to_new_sector_category(self):
        # Boeing was added as part of the registry expansion, mapping
        # to a sector category ("Industrials") that didn't exist before.
        article = make_article(companies_mentioned=[make_company("Boeing")])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertIn("Industrials", sectors)

    def test_newly_added_airline_maps_to_airlines_sector(self):
        article = make_article(companies_mentioned=[make_company("Delta Air Lines")])
        tagged = self.detector.detect_in_article(article)
        sectors = {s["sector"] for s in tagged["sectors"]}
        self.assertIn("Airlines & Aviation", sectors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
