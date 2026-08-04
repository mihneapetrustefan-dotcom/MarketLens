"""
test_news_cleaner.py
----------------------
Unit tests for News Cleaner v1 (news_cleaner.py).

TESTING STRATEGY:
All tests use small, synthetic article dicts (no dependency on the RSS
Collector or the network) so they are fast, deterministic, and isolate
exactly what this module is responsible for: text/URL cleaning and
content-validity filtering.
"""

import unittest

from news_cleaner import NewsCleaner


def make_article(**overrides):
    """Helper: builds a minimal valid article dict, with overrides applied."""
    base = {
        "article_id": "abc-123",
        "title": "Company X reports record quarterly earnings",
        "summary": "A short summary.",
        "url": "https://example.com/article",
        "source": "TestSource",
        "category": "stocks",
    }
    base.update(overrides)
    return base


class TestTextCleaning(unittest.TestCase):
    """Tests for HTML stripping and whitespace normalization."""

    def setUp(self):
        self.cleaner = NewsCleaner()

    def test_strip_html_removes_tags(self):
        result = self.cleaner.strip_html("<p>Hello <b>world</b></p>")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertIn("Hello", result)
        self.assertIn("world", result)

    def test_strip_html_decodes_entities(self):
        result = self.cleaner.strip_html("Fitch &amp; Moody&#39;s cut ratings")
        self.assertIn("&", result)
        self.assertIn("'", result)
        self.assertNotIn("&amp;", result)

    def test_strip_html_does_not_reintroduce_tags_from_entities(self):
        # "&lt;script&gt;" must stay as literal text describing a tag,
        # not become a real <script> tag that then vanishes silently.
        result = self.cleaner.strip_html("Report says &lt;script&gt;alert()&lt;/script&gt;")
        self.assertIn("script", result)  # the word survived as plain text

    def test_normalize_whitespace_collapses_and_trims(self):
        result = self.cleaner.normalize_whitespace("  Too   many\n\n spaces  ")
        self.assertEqual(result, "Too many spaces")

    def test_clean_text_full_pipeline(self):
        result = self.cleaner.clean_text("<p>Stocks   rise  &amp; bonds fall</p>")
        self.assertEqual(result, "Stocks rise & bonds fall")

    def test_clean_text_handles_empty_string(self):
        self.assertEqual(self.cleaner.clean_text(""), "")
        self.assertEqual(self.cleaner.clean_text(None), "")


class TestUrlCleaning(unittest.TestCase):
    """Tests for tracking-parameter removal from URLs."""

    def setUp(self):
        self.cleaner = NewsCleaner()

    def test_removes_known_tracking_params(self):
        url = "https://www.profit.ro/some-article?utm_source=Rss&utm_medium=Referral&utm_campaign=Cross"
        result = self.cleaner.clean_url(url)
        self.assertEqual(result, "https://www.profit.ro/some-article")

    def test_keeps_non_tracking_params(self):
        url = "https://example.com/article?id=42&utm_source=Rss"
        result = self.cleaner.clean_url(url)
        self.assertIn("id=42", result)
        self.assertNotIn("utm_source", result)

    def test_drops_fragment(self):
        url = "https://example.com/article#section2"
        result = self.cleaner.clean_url(url)
        self.assertNotIn("#", result)

    def test_empty_url_returns_empty_string(self):
        self.assertEqual(self.cleaner.clean_url(""), "")


class TestArticleValidity(unittest.TestCase):
    """Tests for the is_valid() content-quality check."""

    def setUp(self):
        self.cleaner = NewsCleaner()

    def test_valid_article_passes(self):
        article = make_article(title="Stocks rally after strong jobs report")
        self.assertTrue(self.cleaner.is_valid(article))

    def test_empty_title_is_invalid(self):
        article = make_article(title="")
        self.assertFalse(self.cleaner.is_valid(article))

    def test_too_short_title_is_invalid(self):
        article = make_article(title="Breaking")  # 1 word
        self.assertFalse(self.cleaner.is_valid(article))


class TestCleanArticle(unittest.TestCase):
    """Tests for clean_article(): full per-article cleaning + filtering."""

    def setUp(self):
        self.cleaner = NewsCleaner()

    def test_cleans_all_fields_together(self):
        article = make_article(
            title="<b>Company X</b> beats earnings &amp; raises guidance",
            summary="<p>Some   summary &nbsp; text</p>",
            url="https://example.com/x?utm_source=Rss",
        )
        cleaned = self.cleaner.clean_article(article)

        self.assertIsNotNone(cleaned)
        self.assertNotIn("<", cleaned["title"])
        self.assertNotIn("&amp;", cleaned["title"])
        self.assertNotIn("<", cleaned["summary"])
        self.assertNotIn("utm_source", cleaned["url"])

    def test_discards_low_content_article(self):
        article = make_article(title="No")
        cleaned = self.cleaner.clean_article(article)
        self.assertIsNone(cleaned)

    def test_does_not_mutate_original_dict(self):
        article = make_article(title="<b>Original</b> title with tags")
        original_title = article["title"]
        self.cleaner.clean_article(article)
        # The input dict must be untouched — clean_article must return
        # a new dict, never mutate the caller's data in place.
        self.assertEqual(article["title"], original_title)

    def test_preserves_non_text_fields(self):
        article = make_article(article_id="keep-me-123", source="Reuters", category="crypto")
        cleaned = self.cleaner.clean_article(article)
        self.assertEqual(cleaned["article_id"], "keep-me-123")
        self.assertEqual(cleaned["source"], "Reuters")
        self.assertEqual(cleaned["category"], "crypto")


class TestCleanBatch(unittest.TestCase):
    """Tests for clean_batch(): the full-list orchestration method."""

    def setUp(self):
        self.cleaner = NewsCleaner()

    def test_filters_out_invalid_articles_from_batch(self):
        batch = [
            make_article(article_id="1", title="A perfectly valid headline here"),
            make_article(article_id="2", title="Bad"),          # too short -> discarded
            make_article(article_id="3", title=""),              # empty -> discarded
            make_article(article_id="4", title="Another valid headline for testing"),
        ]
        cleaned = self.cleaner.clean_batch(batch)
        ids = {a["article_id"] for a in cleaned}
        self.assertEqual(ids, {"1", "4"})

    def test_empty_batch_returns_empty_list(self):
        self.assertEqual(self.cleaner.clean_batch([]), [])

    def test_batch_output_is_list_of_dicts(self):
        batch = [make_article(article_id="1")]
        cleaned = self.cleaner.clean_batch(batch)
        self.assertIsInstance(cleaned, list)
        self.assertIsInstance(cleaned[0], dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
