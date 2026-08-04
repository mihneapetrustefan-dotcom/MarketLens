"""
test_pipeline_core.py
-------------------------
Smoke test for pipeline_core.py — confirms the processing chain runs
end-to-end and tags the expected fields. The individual steps (News
Cleaner, Duplicate Detector, etc.) are already thoroughly tested in
their own test files; this only verifies they're wired together
correctly, in the right order.
"""

import unittest

from pipeline_core import process_articles


def make_raw_article(article_id, title, url):
    return {
        "article_id": article_id,
        "title": title,
        "summary": "",
        "url": url,
        "source": "TestSource",
        "category": "stocks",
        "published_at": None,
        "collected_at": "2026-08-04T09:00:00+00:00",
    }


class TestProcessArticles(unittest.TestCase):
    def test_chain_tags_all_expected_fields(self):
        raw = [make_raw_article("1", "Tesla shares surge to record high after beating earnings", "http://a/1")]
        processed = process_articles(raw)

        self.assertEqual(len(processed), 1)
        article = processed[0]
        for field in ("companies_mentioned", "tickers_mentioned", "sectors", "sentiment", "impact"):
            self.assertIn(field, article)

    def test_empty_input_returns_empty_output(self):
        self.assertEqual(process_articles([]), [])

    def test_low_content_article_is_filtered_out_by_cleaner(self):
        raw = [make_raw_article("1", "Hi", "http://a/1")]  # title too short
        processed = process_articles(raw)
        self.assertEqual(processed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
