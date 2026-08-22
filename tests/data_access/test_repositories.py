"""
tests/data_access/test_repositories.py
-------------------------------------------
Tests for the Internal Data Access Layer (repositories.py).
"""

import sys
import os
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.schema import initialize_schema
from src.data_access.repositories import (
    ExchangeRepository, SectorRepository, CompanyRepository,
    SecurityRepository, InstrumentRepository, NewsSourceRepository,
)
from src.domain.models import Exchange, Sector, Company, Security, Instrument, NewsSource
from src.domain.enums import AssetClass, InstrumentType, SourceType


def new_conn():
    conn = sqlite3.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestExchangeRepository(unittest.TestCase):
    def setUp(self):
        self.repo = ExchangeRepository(new_conn())

    def test_save_and_get(self):
        self.repo.save(Exchange(exchange_id="NASDAQ", name="Nasdaq", country="US"))
        result = self.repo.get("NASDAQ")
        self.assertEqual(result.name, "Nasdaq")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get("NOPE"))

    def test_save_is_idempotent(self):
        self.repo.save(Exchange(exchange_id="BVB", name="Old Name", country="RO"))
        self.repo.save(Exchange(exchange_id="BVB", name="New Name", country="RO"))
        self.assertEqual(self.repo.get("BVB").name, "New Name")

    def test_list_all(self):
        self.repo.save(Exchange(exchange_id="A", name="A", country="US"))
        self.repo.save(Exchange(exchange_id="B", name="B", country="RO"))
        self.assertEqual(len(self.repo.list_all()), 2)


class TestSectorRepository(unittest.TestCase):
    def setUp(self):
        self.repo = SectorRepository(new_conn())

    def test_save_and_get(self):
        self.repo.save(Sector(sector_id="technology", name="Technology"))
        self.assertEqual(self.repo.get("technology").name, "Technology")

    def test_list_all_sorted_by_name(self):
        self.repo.save(Sector(sector_id="energy", name="Energy"))
        self.repo.save(Sector(sector_id="automotive", name="Automotive"))
        names = [s.name for s in self.repo.list_all()]
        self.assertEqual(names, sorted(names))


class TestCompanyRepository(unittest.TestCase):
    def setUp(self):
        self.repo = CompanyRepository(new_conn())

    def test_save_and_get_preserves_aliases(self):
        self.repo.save(Company(company_id="nvidia", canonical_name="Nvidia", aliases=["Nvidia", "NVDA Corp"], sector_id="technology"))
        result = self.repo.get("nvidia")
        self.assertEqual(result.aliases, ["Nvidia", "NVDA Corp"])

    def test_get_by_canonical_name(self):
        self.repo.save(Company(company_id="tesla", canonical_name="Tesla"))
        result = self.repo.get_by_canonical_name("Tesla")
        self.assertEqual(result.company_id, "tesla")

    def test_get_by_canonical_name_missing_returns_none(self):
        self.assertIsNone(self.repo.get_by_canonical_name("Nonexistent Co"))

    def test_list_by_sector(self):
        self.repo.save(Company(company_id="a", canonical_name="A", sector_id="technology"))
        self.repo.save(Company(company_id="b", canonical_name="B", sector_id="energy"))
        results = self.repo.list_by_sector("technology")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].company_id, "a")

    def test_count(self):
        self.repo.save(Company(company_id="a", canonical_name="A"))
        self.repo.save(Company(company_id="b", canonical_name="B"))
        self.assertEqual(self.repo.count(), 2)

    def test_company_with_no_aliases_round_trips_as_empty_list(self):
        self.repo.save(Company(company_id="x", canonical_name="X"))
        self.assertEqual(self.repo.get("x").aliases, [])


class TestSecurityRepository(unittest.TestCase):
    def setUp(self):
        self.repo = SecurityRepository(new_conn())

    def test_save_and_get(self):
        self.repo.save(Security(security_id="nvda-common", company_id="nvidia", instrument_type=InstrumentType.COMMON_STOCK))
        result = self.repo.get("nvda-common")
        self.assertEqual(result.instrument_type, InstrumentType.COMMON_STOCK)

    def test_security_with_no_company(self):
        self.repo.save(Security(security_id="sp500", company_id=None, instrument_type=InstrumentType.INDEX))
        self.assertIsNone(self.repo.get("sp500").company_id)

    def test_list_by_company_multiple_securities(self):
        self.repo.save(Security(security_id="c-common", company_id="c", instrument_type=InstrumentType.COMMON_STOCK))
        self.repo.save(Security(security_id="c-adr", company_id="c", instrument_type=InstrumentType.COMMON_STOCK))
        results = self.repo.list_by_company("c")
        self.assertEqual(len(results), 2)


class TestInstrumentRepository(unittest.TestCase):
    def setUp(self):
        self.repo = InstrumentRepository(new_conn())

    def test_save_and_get(self):
        self.repo.save(Instrument(instrument_id="i1", security_id="s1", exchange_id="NASDAQ", ticker="NVDA", asset_class=AssetClass.STOCK))
        self.assertEqual(self.repo.get("i1").ticker, "NVDA")

    def test_get_by_exchange_and_ticker_disambiguates_shared_ticker(self):
        self.repo.save(Instrument(instrument_id="i1", security_id="s-electrica", exchange_id="BVB", ticker="EL", asset_class=AssetClass.BVB))
        self.repo.save(Instrument(instrument_id="i2", security_id="s-estee", exchange_id="NYSE", ticker="EL", asset_class=AssetClass.STOCK))

        electrica = self.repo.get_by_exchange_and_ticker("BVB", "EL")
        estee = self.repo.get_by_exchange_and_ticker("NYSE", "EL")
        self.assertEqual(electrica.security_id, "s-electrica")
        self.assertEqual(estee.security_id, "s-estee")

    def test_list_by_ticker_returns_all_sharing_the_bare_ticker(self):
        self.repo.save(Instrument(instrument_id="i1", security_id="s-electrica", exchange_id="BVB", ticker="EL", asset_class=AssetClass.BVB))
        self.repo.save(Instrument(instrument_id="i2", security_id="s-estee", exchange_id="NYSE", ticker="EL", asset_class=AssetClass.STOCK))
        results = self.repo.list_by_ticker("EL")
        self.assertEqual(len(results), 2)

    def test_get_by_exchange_and_ticker_missing_returns_none(self):
        self.assertIsNone(self.repo.get_by_exchange_and_ticker("NASDAQ", "NOPE"))

    def test_count(self):
        self.repo.save(Instrument(instrument_id="i1", security_id="s1", exchange_id="NASDAQ", ticker="NVDA", asset_class=AssetClass.STOCK))
        self.assertEqual(self.repo.count(), 1)


class TestNewsSourceRepository(unittest.TestCase):
    def setUp(self):
        self.repo = NewsSourceRepository(new_conn())

    def test_save_and_get(self):
        self.repo.save(NewsSource(source_id="reuters", name="Reuters", source_type=SourceType.WIRE_OR_MAJOR_PRESS))
        self.assertEqual(self.repo.get("reuters").source_type, SourceType.WIRE_OR_MAJOR_PRESS)

    def test_get_by_name(self):
        self.repo.save(NewsSource(source_id="fed", name="Federal Reserve Press Releases", source_type=SourceType.OFFICIAL))
        result = self.repo.get_by_name("Federal Reserve Press Releases")
        self.assertEqual(result.source_id, "fed")

    def test_active_flag_round_trips(self):
        self.repo.save(NewsSource(source_id="s1", name="S1", active=False))
        self.assertFalse(self.repo.get("s1").active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
