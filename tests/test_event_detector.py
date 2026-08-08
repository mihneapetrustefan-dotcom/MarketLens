"""
test_event_detector.py
--------------------------
Unit tests for Event Detector v1.
"""

import unittest

from event_detector import EventDetector


TEST_LEXICON = {
    "EARNINGS": ["quarterly earnings", "beat estimates"],
    "CEO_CHANGE": ["ceo resigns", "names new ceo"],
    "LAYOFFS": ["lays off", "job cuts"],
    "LAWSUIT": ["files lawsuit", "sues"],
}


def make_article(title, summary=""):
    return {"title": title, "summary": summary}


class TestDetectInText(unittest.TestCase):
    def setUp(self):
        self.detector = EventDetector(lexicon=TEST_LEXICON)

    def test_single_event_type_detected(self):
        results = self.detector.detect_in_text("Company beat estimates in quarterly earnings")
        event_types = {r["event_type"] for r in results}
        self.assertIn("EARNINGS", event_types)

    def test_multiple_event_types_in_one_article(self):
        results = self.detector.detect_in_text("CEO resigns amid shareholder who sues over layoffs and job cuts")
        event_types = {r["event_type"] for r in results}
        self.assertIn("CEO_CHANGE", event_types)
        self.assertIn("LAWSUIT", event_types)
        self.assertIn("LAYOFFS", event_types)

    def test_no_matching_phrase_returns_empty_list(self):
        results = self.detector.detect_in_text("The weather was pleasant today in the city")
        self.assertEqual(results, [])

    def test_case_insensitive_matching(self):
        results = self.detector.detect_in_text("COMPANY BEAT ESTIMATES this quarter")
        event_types = {r["event_type"] for r in results}
        self.assertIn("EARNINGS", event_types)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self.detector.detect_in_text(""), [])

    def test_none_text_returns_empty_list(self):
        self.assertEqual(self.detector.detect_in_text(None), [])

    def test_word_boundary_prevents_partial_word_match(self):
        results = self.detector.detect_in_text("The company issues a new statement about pursuestrategy")
        event_types = {r["event_type"] for r in results}
        self.assertNotIn("LAWSUIT", event_types)

    def test_matched_phrase_is_reported(self):
        results = self.detector.detect_in_text("The firm sues a competitor over patents")
        lawsuit_match = next(r for r in results if r["event_type"] == "LAWSUIT")
        self.assertEqual(lawsuit_match["matched_phrase"].lower(), "sues")


class TestDetectInArticle(unittest.TestCase):
    def setUp(self):
        self.detector = EventDetector(lexicon=TEST_LEXICON)

    def test_checks_both_title_and_summary(self):
        article = make_article("Company update", summary="CEO resigns effective immediately")
        result = self.detector.detect_in_article(article)
        event_types = {e["event_type"] for e in result["events"]}
        self.assertIn("CEO_CHANGE", event_types)

    def test_does_not_mutate_input_article(self):
        article = make_article("CEO resigns today")
        original = dict(article)
        self.detector.detect_in_article(article)
        self.assertEqual(article, original)

    def test_returns_new_dict_with_events_field(self):
        article = make_article("Ordinary news update with nothing special")
        result = self.detector.detect_in_article(article)
        self.assertIn("events", result)
        self.assertEqual(result["events"], [])


class TestDetectBatch(unittest.TestCase):
    def setUp(self):
        self.detector = EventDetector(lexicon=TEST_LEXICON)

    def test_processes_every_article(self):
        articles = [make_article("CEO resigns today"), make_article("Nothing notable happened")]
        results = self.detector.detect_batch(articles)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["events"])
        self.assertFalse(results[1]["events"])

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.detector.detect_batch([]), [])


class TestRealLexicon(unittest.TestCase):
    """Sanity checks against the REAL, full lexicon (not the small test one)."""

    def setUp(self):
        self.detector = EventDetector()  # uses the real EVENT_LEXICON

    def test_real_earnings_headline_is_classified(self):
        results = self.detector.detect_in_text("Apple reports quarterly earnings, beat estimates on strong iPhone sales")
        event_types = {r["event_type"] for r in results}
        self.assertIn("EARNINGS", event_types)

    def test_real_acquisition_headline_is_classified(self):
        results = self.detector.detect_in_text("Microsoft to acquire gaming startup for $2 billion")
        event_types = {r["event_type"] for r in results}
        self.assertIn("ACQUISITION", event_types)

    def test_real_layoffs_headline_is_classified(self):
        results = self.detector.detect_in_text("Amazon lays off 10,000 workers amid restructuring")
        event_types = {r["event_type"] for r in results}
        self.assertIn("LAYOFFS", event_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
