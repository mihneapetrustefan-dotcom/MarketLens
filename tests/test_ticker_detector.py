"""
test_ticker_detector.py
--------------------------
Unit tests for Ticker Detector v1 (ticker_detector.py).

TESTING STRATEGY:
Most tests use a SMALL, CONTROLLED registry injected via the
constructor, isolating the matching logic from the real, larger
registry. A few tests at the end sanity-check against the real
TICKER_REGISTRY.
"""

import unittest

from ticker_detector import TickerDetector


TEST_REGISTRY = [
    {"ticker": "AAPL", "name": "Apple", "category": "stocks"},
    {"ticker": "TLV", "name": "Banca Transilvania", "category": "bvb"},
    {"ticker": "H2O", "name": "Hidroelectrica", "category": "bvb"},
    {"ticker": "BTC", "name": "Bitcoin", "category": "crypto"},
    {"ticker": "EURUSD", "name": "Euro / US Dollar", "category": "forex"},
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


class TestCashtagDetection(unittest.TestCase):
    """Tests for '$SYMBOL' cashtag matching."""

    def setUp(self):
        self.detector = TickerDetector(registry=TEST_REGISTRY)

    def test_detects_known_cashtag(self):
        results = self.detector.detect_in_text("$AAPL is up 3% today")
        aapl = next(r for r in results if r["ticker"] == "AAPL")
        self.assertEqual(aapl["name"], "Apple")
        self.assertEqual(aapl["category"], "stocks")
        self.assertEqual(aapl["match_type"], "cashtag")

    def test_reports_unrecognized_cashtag_without_dropping_it(self):
        results = self.detector.detect_in_text("$XYZ surged after news broke")
        xyz = next(r for r in results if r["ticker"] == "XYZ")
        self.assertIsNone(xyz["name"])
        self.assertIsNone(xyz["category"])
        self.assertEqual(xyz["match_type"], "cashtag")

    def test_cashtag_matching_is_case_insensitive_on_input(self):
        # "$aapl" (lowercase after the $) should still resolve to AAPL.
        results = self.detector.detect_in_text("$aapl gains after earnings")
        tickers = {r["ticker"] for r in results}
        self.assertIn("AAPL", tickers)


class TestBareTickerDetection(unittest.TestCase):
    """Tests for bare (no '$') ticker matching against the whitelist."""

    def setUp(self):
        self.detector = TickerDetector(registry=TEST_REGISTRY)

    def test_detects_known_bare_ticker(self):
        results = self.detector.detect_in_text("TLV gained 2% today on strong earnings")
        tlv = next(r for r in results if r["ticker"] == "TLV")
        self.assertEqual(tlv["name"], "Banca Transilvania")
        self.assertEqual(tlv["match_type"], "bare")

    def test_bare_matching_is_case_sensitive(self):
        # Lowercase "tlv" must NOT match — real tickers are written in
        # capitals, and case-sensitivity is our main defense against
        # false positives from ordinary words.
        results = self.detector.detect_in_text("tlv gained 2% today")
        tickers = {r["ticker"] for r in results}
        self.assertNotIn("TLV", tickers)

    def test_word_boundary_avoids_partial_word_match(self):
        # "AAPLE" must not match "AAPL" — no word boundary between "L"
        # and the trailing "E".
        results = self.detector.detect_in_text("AAPLE is a fictional fruit brand")
        tickers = {r["ticker"] for r in results}
        self.assertNotIn("AAPL", tickers)

    def test_ticker_containing_digit_is_matched(self):
        # H2O (Hidroelectrica) mixes letters and a digit — must still work.
        results = self.detector.detect_in_text("H2O shares rose after the earnings call")
        tickers = {r["ticker"] for r in results}
        self.assertIn("H2O", tickers)

    def test_unknown_bare_uppercase_word_is_not_matched(self):
        # "CEO" is a common all-caps abbreviation NOT in our registry —
        # must never be reported as a ticker.
        results = self.detector.detect_in_text("The CEO announced a new strategy")
        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, set())

    def test_single_character_ticker_is_never_matched_bare(self):
        # Reproduces a real false positive found in production data:
        # a middle initial ("Kathleen M. Hutchinson") being mistaken for
        # a 1-letter ticker ("M"). Single-character tickers must never
        # be matched in bare form, regardless of what they represent.
        registry_with_single_char = TEST_REGISTRY + [
            {"ticker": "M", "name": "MedLife", "category": "bvb"}
        ]
        detector = TickerDetector(registry=registry_with_single_char)
        results = detector.detect_in_text("Kathleen M. Hutchinson was appointed director")
        tickers = {r["ticker"] for r in results}
        self.assertNotIn("M", tickers)

    def test_single_character_ticker_still_matches_as_cashtag(self):
        # The "$" prefix removes the ambiguity a bare single letter has,
        # so cashtag form must still work even for 1-character tickers.
        registry_with_single_char = TEST_REGISTRY + [
            {"ticker": "M", "name": "MedLife", "category": "bvb"}
        ]
        detector = TickerDetector(registry=registry_with_single_char)
        results = detector.detect_in_text("$M shares rose 2% today")
        tickers = {r["ticker"] for r in results}
        self.assertIn("M", tickers)


class TestCombinedDetection(unittest.TestCase):
    """Tests for detect_in_text() combining cashtag + bare results."""

    def setUp(self):
        self.detector = TickerDetector(registry=TEST_REGISTRY)

    def test_multiple_distinct_tickers_detected_together(self):
        results = self.detector.detect_in_text("TLV and BTC both rallied while $AAPL dipped")
        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"TLV", "BTC", "AAPL"})

    def test_cashtag_wins_over_bare_for_same_symbol(self):
        # AAPL mentioned both bare and as a cashtag in the same text ->
        # only one entry, reported as "cashtag" (the higher-confidence form).
        results = self.detector.detect_in_text("AAPL rallied; $AAPL now leads the market")
        matches = [r for r in results if r["ticker"] == "AAPL"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_type"], "cashtag")

    def test_no_match_returns_empty_list(self):
        results = self.detector.detect_in_text("A quiet day with no notable market news")
        self.assertEqual(results, [])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self.detector.detect_in_text(""), [])


class TestDetectInArticleAndBatch(unittest.TestCase):
    """Tests for detect_in_article() and detect_batch()."""

    def setUp(self):
        self.detector = TickerDetector(registry=TEST_REGISTRY)

    def test_combines_title_and_summary(self):
        article = make_article(title="Markets react", summary="TLV shares jump 5%")
        tagged = self.detector.detect_in_article(article)
        tickers = {t["ticker"] for t in tagged["tickers_mentioned"]}
        self.assertIn("TLV", tickers)

    def test_does_not_mutate_original_article(self):
        article = make_article(title="TLV stock update")
        self.detector.detect_in_article(article)
        self.assertNotIn("tickers_mentioned", article)

    def test_detect_batch_tags_every_article(self):
        articles = [
            make_article(article_id="1", title="TLV climbs on earnings beat"),
            make_article(article_id="2", title="Local weather stays mild"),
        ]
        result = self.detector.detect_batch(articles)
        self.assertTrue(all("tickers_mentioned" in a for a in result))
        first = next(a for a in result if a["article_id"] == "1")
        self.assertEqual(first["tickers_mentioned"][0]["ticker"], "TLV")
        second = next(a for a in result if a["article_id"] == "2")
        self.assertEqual(second["tickers_mentioned"], [])

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.detector.detect_batch([]), [])


class TestRealRegistrySanityCheck(unittest.TestCase):
    """A few checks against the ACTUAL production registry."""

    def setUp(self):
        self.detector = TickerDetector()  # uses real TICKER_REGISTRY

    def test_detects_real_bvb_ticker(self):
        results = self.detector.detect_in_text("SNP a raportat un profit in crestere")
        tickers = {r["ticker"] for r in results}
        self.assertIn("SNP", tickers)

    def test_detects_real_etf_ticker(self):
        results = self.detector.detect_in_text("SPY closed slightly higher today")
        tickers = {r["ticker"] for r in results}
        self.assertIn("SPY", tickers)

    def test_detects_real_forex_pair(self):
        results = self.detector.detect_in_text("EURUSD dropped below key support")
        tickers = {r["ticker"] for r in results}
        self.assertIn("EURUSD", tickers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
