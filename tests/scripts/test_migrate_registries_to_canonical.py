"""
tests/scripts/test_migrate_registries_to_canonical.py
-----------------------------------------------------------
End-to-end tests for the migration script, run against the REAL
existing registries and a disposable temp database file.
"""

import sys
import os
import tempfile
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.migrate_registries_to_canonical import run_migration
from src.data_access.repositories import CompanyRepository, InstrumentRepository, NewsSourceRepository, SectorRepository
from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP
from sources import RSS_FEEDS


class TestMigrationOnRealData(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def test_migrates_every_real_company(self):
        summary = run_migration(self.db_path)
        self.assertEqual(summary["companies"], len(COMPANY_REGISTRY))

    def test_migrates_every_distinct_real_sector(self):
        summary = run_migration(self.db_path)
        self.assertEqual(summary["sectors"], len(set(COMPANY_SECTOR_MAP.values())))

    def test_migrates_every_real_news_source(self):
        summary = run_migration(self.db_path)
        self.assertEqual(summary["news_sources"], len(RSS_FEEDS))

    def test_running_twice_produces_the_same_counts_not_duplicates(self):
        first = run_migration(self.db_path)
        second = run_migration(self.db_path)
        self.assertEqual(first, second)

    def test_a_real_known_company_is_queryable_after_migration(self):
        run_migration(self.db_path)
        conn = sqlite3.connect(self.db_path)
        company_repo = CompanyRepository(conn)
        nvidia = company_repo.get_by_canonical_name("Nvidia")
        self.assertIsNotNone(nvidia)
        self.assertEqual(nvidia.sector_id, "technology")
        conn.close()

    def test_the_real_el_ticker_collision_is_resolved_after_migration(self):
        run_migration(self.db_path)
        conn = sqlite3.connect(self.db_path)
        instrument_repo = InstrumentRepository(conn)
        shared = instrument_repo.list_by_ticker("EL")
        self.assertEqual(len(shared), 2)
        exchanges = {i.exchange_id for i in shared}
        self.assertEqual(exchanges, {"BVB", "US_AND_INTL"})
        conn.close()

    def test_a_real_news_source_is_queryable_with_correct_tier(self):
        run_migration(self.db_path)
        conn = sqlite3.connect(self.db_path)
        source_repo = NewsSourceRepository(conn)
        fed = source_repo.get_by_name("Federal Reserve Press Releases")
        self.assertIsNotNone(fed)
        self.assertEqual(fed.source_type.value, "official")
        conn.close()

    def test_existing_tables_untouched_if_already_present(self):
        # Simulate the real production database: it already has a
        # `recommendations` table (from RecommendationLog) BEFORE this
        # migration ever runs.
        from recommendation_log import RecommendationLog
        rec_log = RecommendationLog(self.db_path)
        rec_log.log_recommendations([{"entity": "Tesla", "recommendation": "SELL", "confidence_score": 0.6}])
        rec_log.close()

        run_migration(self.db_path)

        rec_log_after = RecommendationLog(self.db_path)
        rows = rec_log_after.load_all()
        rec_log_after.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entity"], "Tesla")

    def test_sector_company_counts_sum_correctly(self):
        run_migration(self.db_path)
        conn = sqlite3.connect(self.db_path)
        company_repo = CompanyRepository(conn)
        sector_repo = SectorRepository(conn)
        total_by_sector = sum(len(company_repo.list_by_sector(s.sector_id)) for s in sector_repo.list_all())
        conn.close()
        self.assertEqual(total_by_sector, len(COMPANY_REGISTRY))


if __name__ == "__main__":
    unittest.main(verbosity=2)
