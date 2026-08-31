"""
test_dashboard.py
---------------------
Unit tests for the MarketLens Terminal generator (dashboard.py).

WHAT CHANGED FROM THE OLD SUITE
--------------------------------
The previous dashboard rendered every card server-side in Python, so
its tests asserted on exact HTML fragments (badge classes, hold-gap
markup, etc). The Terminal renders client-side from one embedded JSON
blob (see the <script> in _HTML_TEMPLATE) — Python's job is now to
collect correct, safely-escaped DATA, not to produce final markup. So
these tests exercise the data layer: the collectors degrade correctly
when tables are missing, real rows come through untouched, and the
embedded JSON is valid and safe to inject into a <script> block.
"""

import json
import sqlite3
import unittest

from dashboard import DashboardGenerator


def _extract_data(html: str) -> dict:
    marker = "window.ML_DATA = "
    # not used directly; data is embedded as `var D = {...};` — extract
    # the object literal between the marker and its trailing semicolon.
    start = html.index("var D = ") + len("var D = ")
    end = html.index(";\n", start)
    return json.loads(html[start:end])


class TestReportStructure(unittest.TestCase):
    def setUp(self):
        self.generator = DashboardGenerator()

    def test_generates_valid_html_document(self):
        html = self.generator.generate_report()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)

    def test_no_connection_and_no_args_does_not_crash(self):
        html = self.generator.generate_report()
        data = _extract_data(html)
        self.assertFalse(data["legacy"]["available"])
        self.assertFalse(data["signals"]["available"])
        self.assertFalse(data["events"]["available"])

    def test_embedded_json_is_valid_and_matches_python_data(self):
        html = self.generator.generate_report(daily_summary_text="Rezumatul zilei")
        data = _extract_data(html)
        self.assertEqual(data["meta"]["daily_summary"], "Rezumatul zilei")

    def test_universe_is_populated_from_real_registry(self):
        html = self.generator.generate_report()
        data = _extract_data(html)
        self.assertGreater(len(data["universe"]), 300)
        tickers = {c["t"] for c in data["universe"]}
        self.assertIn("AAPL", tickers)

    def test_sector_summary_has_twelve_sectors(self):
        html = self.generator.generate_report()
        data = _extract_data(html)
        self.assertEqual(len(data["sector_summary"]), 12)
        for s in data["sector_summary"]:
            self.assertIn("company_count", s)
            self.assertIn("keyword_count", s)

    def test_event_lexicon_present_with_phrases(self):
        html = self.generator.generate_report()
        data = _extract_data(html)
        self.assertIn("EARNINGS", data["lexicon"])
        self.assertGreater(len(data["lexicon"]["EARNINGS"]["phrases"]), 0)

    def test_script_breakout_is_escaped_in_json_blob(self):
        html = self.generator.generate_report(daily_summary_text="</script><script>alert(1)</script>")
        self.assertNotIn("</script><script>alert(1)</script>", html)
        data = _extract_data(html)
        self.assertIn("</script>", data["meta"]["daily_summary"])  # original value preserved, just escaped on the wire


class TestWithSyntheticDatabase(unittest.TestCase):
    """Exercises every collector against a small hand-built database
    covering each phase's tables, the same way build_dashboard.py's
    manual DB-only rebuild path does."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        c = self.conn.cursor()
        c.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, source TEXT, published_at TEXT)")
        c.execute("CREATE TABLE article_entities (article_id TEXT, entity TEXT)")
        c.execute("INSERT INTO articles VALUES ('a1','Reuters','2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO articles VALUES ('a2','Bloomberg','2026-01-02T00:00:00Z')")
        c.execute("INSERT INTO article_entities VALUES ('a1','Apple')")

        c.execute("""CREATE TABLE recommendations (
            entity TEXT, ticker TEXT, recommendation TEXT, confidence_score REAL,
            time_horizon TEXT, generated_at TEXT, checked_at TEXT, was_correct INTEGER)""")
        c.execute("INSERT INTO recommendations VALUES ('Apple','AAPL','STRONG_BUY',0.8,'1W','2026-01-01T00:00:00Z','2026-01-05T00:00:00Z',1)")
        c.execute("INSERT INTO recommendations VALUES ('Apple','AAPL','BUY',0.6,'1W','2025-12-01T00:00:00Z',NULL,NULL)")
        c.execute("INSERT INTO recommendations VALUES ('Tesla','TSLA','SELL',0.55,'1W','2026-01-02T00:00:00Z','2026-01-06T00:00:00Z',0)")

        c.execute("CREATE TABLE canonical_events (canonical_event_id TEXT PRIMARY KEY, event_type TEXT, corroboration_state TEXT)")
        c.execute("INSERT INTO canonical_events VALUES ('e1','EARNINGS','single_source')")
        c.execute("INSERT INTO canonical_events VALUES ('e2','EARNINGS','multi_source')")

        c.execute("CREATE TABLE signals (signal_id TEXT PRIMARY KEY, instrument_id TEXT, direction TEXT, status TEXT, strength REAL, confidence REAL, expected_return REAL, source_information_cutoff TEXT)")
        c.execute("INSERT INTO signals VALUES ('s1','crypto-BTC','long','active',0.6,0.7,0.02,'2026-01-01T00:00:00Z')")
        self.conn.commit()
        self.generator = DashboardGenerator()

    def tearDown(self):
        self.conn.close()

    def test_legacy_recommendations_available_and_correct_totals(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertTrue(data["legacy"]["available"])
        self.assertEqual(data["legacy"]["total_recs"], 3)
        self.assertEqual(data["legacy"]["checked"], 2)

    def test_rec_index_picks_latest_per_entity(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertEqual(data["rec_index"]["Apple"]["recommendation"], "STRONG_BUY")

    def test_events_available_with_real_counts(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertTrue(data["events"]["available"])
        self.assertEqual(data["events"]["total"], 2)

    def test_signals_available_and_instrument_present(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertTrue(data["signals"]["available"])
        instruments = [row[1] for row in data["signals"]["recent"]]
        self.assertIn("crypto-BTC", instruments)

    def test_impact_and_models_unavailable_when_tables_missing(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertFalse(data["impact"]["available"])
        self.assertFalse(data["models"]["available"])
        self.assertFalse(data["research"]["available"])

    def test_health_reflects_article_and_entity_counts(self):
        html = self.generator.generate_report(conn=self.conn)
        data = _extract_data(html)
        self.assertEqual(data["health"]["total_articles"], 2)
        self.assertEqual(data["health"]["linked_articles"], 1)

    def test_watchlist_count_passed_through(self):
        html = self.generator.generate_report(conn=self.conn, watchlist=["Apple", "Tesla"])
        data = _extract_data(html)
        self.assertEqual(data["meta"]["watchlist_count"], 2)
        self.assertEqual(data["legacy"]["watchlist"], ["Apple", "Tesla"])

    def test_save_report_writes_file(self):
        import tempfile
        import os
        html = self.generator.generate_report(conn=self.conn)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.html")
            self.generator.save_report(html, path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), html)


if __name__ == "__main__":
    unittest.main()
