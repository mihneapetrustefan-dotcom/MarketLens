"""
test_google_news_sources.py
-------------------------------
Unit tests for google_news_sources.py.

TESTING STRATEGY: pure string/URL construction — no network involved
at all, so every test is fully deterministic and just checks the
generated source configs' shape and content.
"""

import unittest
from urllib.parse import unquote_plus

from google_news_sources import build_entity_search_sources


class TestBuildEntitySearchSources(unittest.TestCase):
    def test_builds_one_source_per_entity(self):
        sources = build_entity_search_sources(["Tesla", "Amazon"])
        self.assertEqual(len(sources), 2)

    def test_source_shape_matches_rss_feeds_format(self):
        sources = build_entity_search_sources(["Tesla"])
        source = sources[0]
        self.assertIn("name", source)
        self.assertIn("url", source)
        self.assertIn("category", source)

    def test_url_contains_days_back_modifier(self):
        sources = build_entity_search_sources(["Tesla"], days_back=90)
        self.assertIn("when%3A90d", sources[0]["url"])

    def test_url_contains_quoted_entity_name(self):
        sources = build_entity_search_sources(["Amazon"], days_back=60)
        self.assertIn("Amazon", unquote_plus(sources[0]["url"]))

    def test_default_days_back_is_sixty(self):
        sources = build_entity_search_sources(["Tesla"])
        self.assertIn("when%3A60d", sources[0]["url"])

    def test_category_lookup_applied_correctly(self):
        sources = build_entity_search_sources(
            ["Tesla", "Bitcoin"],
            category_lookup={"Tesla": "stocks", "Bitcoin": "crypto"},
        )
        by_name = {s["name"]: s for s in sources}
        self.assertEqual(by_name["Google News: Tesla"]["category"], "stocks")
        self.assertEqual(by_name["Google News: Bitcoin"]["category"], "crypto")

    def test_unknown_entity_defaults_to_stocks_category(self):
        sources = build_entity_search_sources(["Some New Company"])
        self.assertEqual(sources[0]["category"], "stocks")

    def test_language_and_country_reflected_in_url(self):
        sources = build_entity_search_sources(["Hidroelectrica"], language="ro", country="RO")
        url = sources[0]["url"]
        self.assertIn("hl=ro", url)
        self.assertIn("gl=RO", url)

    def test_special_characters_in_entity_name_are_url_encoded(self):
        sources = build_entity_search_sources(["AT&T"])
        url = sources[0]["url"]
        self.assertNotIn('q="AT&T"', url)

    def test_empty_entity_list_returns_empty_list(self):
        self.assertEqual(build_entity_search_sources([]), [])

    def test_source_name_includes_entity_for_traceability(self):
        sources = build_entity_search_sources(["Nvidia"])
        self.assertEqual(sources[0]["name"], "Google News: Nvidia")


if __name__ == "__main__":
    unittest.main(verbosity=2)
