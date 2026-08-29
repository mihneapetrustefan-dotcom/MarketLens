"""
tests/scripts/test_compute_features.py
-----------------------------------------------------------
Tests for scripts/compute_features.py.

Two properties matter most and get dedicated tests: that no computed
feature is ever timestamped after the observation's cutoff (the whole
point of Phase 8's leakage discipline), and that writing Phase 8 rows
never disturbs Phase 7's rows sharing the same table.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.compute_features import (
    load_candles, load_entity_events, load_entity_articles,
    FEATURE_SOURCE, PEER_FEATURE_IDS, main, _FeatureCandle,
)
from src.data_access.schema import initialize_schema
from src.data_access.news_schema import initialize_news_schema
from src.data_access.fusion_schema import initialize_fusion_schema
from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.data_access.research_schema import initialize_research_schema
from src.news_database import NewsDatabase

ANCHOR = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def seed(path, candle_days=120, with_articles=True):
    NewsDatabase(path)
    conn = sqlite3.connect(path)
    for fn in (initialize_schema, initialize_news_schema, initialize_fusion_schema,
               initialize_price_cache_schema, initialize_research_schema):
        fn(conn)

    conn.execute("INSERT OR IGNORE INTO exchanges (exchange_id,name,country) VALUES ('US_AND_INTL','X','US')")
    conn.execute("INSERT OR IGNORE INTO companies (company_id, canonical_name) VALUES ('nvidia','NVIDIA')")
    conn.execute("INSERT OR IGNORE INTO securities (security_id, company_id, instrument_type) "
                 "VALUES ('nvidia-common','nvidia','common_stock')")
    conn.execute("INSERT OR IGNORE INTO instruments (instrument_id, security_id, exchange_id, ticker, asset_class) "
                 "VALUES ('inst-nvda','nvidia-common','US_AND_INTL','NVDA','stock')")

    for i in range(-candle_days, 11):
        ts = (ANCHOR + timedelta(days=i)).replace(hour=20, minute=0)
        price = 100.0 + i * 0.15 + (i % 7) * 0.4
        conn.execute("""INSERT INTO price_candle_cache
            (instrument_id, interval, timestamp, open, high, low, close, adjusted_close, volume, source, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ('inst-nvda', '1d', ts.isoformat(), price, price + 1, price - 1, price, price,
             1_000_000 + (i % 5) * 50_000, 'test', 'now'))

    for j, off in enumerate([-200, -90, -30, -3, 0]):
        ts = ANCHOR + timedelta(days=off)
        conn.execute("""INSERT INTO canonical_events
            (canonical_event_id, event_type, category, lifecycle_state, corroboration_state,
             first_reported_at, independent_source_count, quality_confidence)
            VALUES (?,'acquisition','corporate_action','reported','single_source',?,2,0.8)""",
            (f'ce-{j}', ts.isoformat()))
        conn.execute("INSERT INTO canonical_event_participants (canonical_event_id, entity_id, role) "
                     "VALUES (?,'nvidia','primary')", (f'ce-{j}',))

    if with_articles:
        for k in range(12):
            ts = ANCHOR - timedelta(days=k % 6)
            aid = f'a{k}'
            conn.execute("""INSERT INTO articles (article_id,url,title,summary,source,published_at,sentiment,stored_at)
                VALUES (?,?,?,?,?,?,?,'now')""",
                (aid, f'http://x/{k}', 'T', 'S', f'Source{k%4}', ts.isoformat(),
                 json.dumps({"score": 0.2 + (k % 5) * 0.1, "label": "positive"})))
            conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                         "VALUES (?,'company','nvidia')", (aid,))

    conn.execute("""INSERT INTO research_observations
        (observation_id, event_id, instrument_id, observation_created_at, information_cutoff,
         event_cluster_id, quality_level, dataset_version, event_type)
        VALUES ('obs-1','ce-4','inst-nvda',?,?, 'inst-nvda','high','v1','acquisition')""",
        (ANCHOR.isoformat(), ANCHOR.isoformat()))
    conn.commit()
    conn.close()


class TestLoaders(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed(self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    def test_candles_expose_price_attribute_for_feature_context(self):
        candles = load_candles(self.conn, "inst-nvda")
        self.assertTrue(len(candles) > 0)
        self.assertIsInstance(candles[0], _FeatureCandle)
        self.assertIsNotNone(candles[0].price)

    def test_candles_are_sorted_oldest_first(self):
        candles = load_candles(self.conn, "inst-nvda")
        timestamps = [c.timestamp for c in candles]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_events_loaded_for_the_entity(self):
        events = load_entity_events(self.conn, "inst-nvda")
        self.assertEqual(len(events), 5)

    def test_articles_carry_extracted_sentiment_score(self):
        articles = load_entity_articles(self.conn, "inst-nvda")
        self.assertTrue(len(articles) > 0)
        self.assertIsNotNone(articles[0].sentiment_score)

    def test_malformed_sentiment_json_does_not_raise(self):
        self.conn.execute(
            "INSERT INTO articles (article_id,url,title,summary,source,published_at,sentiment,stored_at) "
            "VALUES ('bad','http://x/bad','T','S','Src',?,'{not json','now')", (ANCHOR.isoformat(),))
        self.conn.execute("INSERT INTO article_entities (article_id, entity_type, entity_id) "
                          "VALUES ('bad','company','nvidia')")
        self.conn.commit()
        articles = load_entity_articles(self.conn, "inst-nvda")  # must not raise
        bad = [a for a in articles if a.sentiment_score is None]
        self.assertTrue(len(bad) >= 1)


class TestComputeEndToEnd(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed(self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    def _run(self, extra):
        argv = sys.argv
        sys.argv = ["x", "--db", self.db_path] + extra
        try:
            return main()
        finally:
            sys.argv = argv

    def _rows(self, source=FEATURE_SOURCE):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT qualified_name, value_json, as_of FROM research_features WHERE source = ?",
                (source,)).fetchall()
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self):
        self._run([])
        self.assertEqual(len(self._rows()), 0)

    def test_apply_writes_all_registry_features(self):
        self._run(["--apply"])
        self.assertEqual(len(self._rows()), 24)

    def test_no_feature_is_timestamped_after_the_cutoff(self):
        # The core leakage guarantee.
        self._run(["--apply"])
        conn = sqlite3.connect(self.db_path)
        cutoff = datetime.fromisoformat(
            conn.execute("SELECT information_cutoff FROM research_observations").fetchone()[0])
        conn.close()
        for _, _, as_of in self._rows():
            self.assertLessEqual(datetime.fromisoformat(as_of), cutoff)

    def test_peer_features_are_recorded_as_null_not_omitted(self):
        # An absent row and a null row mean different things.
        self._run(["--apply"])
        by_name = {name: value for name, value, _ in self._rows()}
        for feature_id in PEER_FEATURE_IDS:
            self.assertIn(feature_id, by_name)
            self.assertEqual(by_name[feature_id], "null")

    def test_market_features_actually_compute(self):
        self._run(["--apply"])
        by_name = {name: value for name, value, _ in self._rows()}
        for feature_id in ("market.return_1d", "market.return_5d", "technical.rsi_14",
                           "volatility.realized_20d"):
            self.assertNotEqual(by_name.get(feature_id), "null", f"{feature_id} should compute")

    def test_event_metadata_features_compute(self):
        # These read from context.metadata, not the events list — a
        # previous version of this script forgot to pass them.
        self._run(["--apply"])
        by_name = {name: value for name, value, _ in self._rows()}
        self.assertEqual(by_name.get("event.confidence"), "0.8")
        self.assertEqual(by_name.get("event.independent_source_count"), "2")

    def test_phase7_rows_in_same_table_are_untouched(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO research_features (observation_id, qualified_name, namespace, value_json, source) "
            "VALUES ('obs-1','event.event_type','event','\"acquisition\"','phase6_event_study')")
        conn.commit()
        conn.close()

        self._run(["--apply"])
        self.assertEqual(len(self._rows(source="phase6_event_study")), 1)

    def test_rerunning_replaces_rather_than_accumulates(self):
        self._run(["--apply"])
        self._run(["--apply"])
        self.assertEqual(len(self._rows()), 24)

    def test_insufficient_history_yields_nulls_not_failures(self):
        # Only 10 candles: long-lookback features cannot compute, but
        # that is a null, not an exception.
        os.remove(self.db_path)
        seed(self.db_path, candle_days=10)
        self._run(["--apply"])
        by_name = {name: value for name, value, _ in self._rows()}
        self.assertEqual(by_name.get("market.return_60d"), "null")
        self.assertEqual(len(self._rows()), 24)


if __name__ == "__main__":
    unittest.main()
