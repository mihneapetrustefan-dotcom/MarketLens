"""
tests/providers/test_registry_adapter.py
---------------------------------------------
Tests for registry_adapter.py, run against the REAL, existing
company_registry.py / sector_registry.py / sources.py /
source_credibility.py — proving the Phase 1 foundation actually holds
today's real data, not a synthetic fixture.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.providers.registry_adapter import (
    SectorRegistryAdapter, CompanyRegistryAdapter, NewsSourceRegistryAdapter, _slugify,
)
from src.domain.enums import AssetClass, InstrumentType, SourceType

from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP
from sources import RSS_FEEDS
from source_credibility import SOURCE_TIERS


class TestSlugify(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(_slugify("Nvidia"), "nvidia")

    def test_name_with_spaces_and_punctuation(self):
        self.assertEqual(_slugify("BRD - Groupe Societe Generale"), "brd-groupe-societe-generale")

    def test_name_with_ampersand(self):
        self.assertEqual(_slugify("Johnson & Johnson"), "johnson-johnson")

    def test_deterministic_across_calls(self):
        self.assertEqual(_slugify("Coca-Cola"), _slugify("Coca-Cola"))


class TestSectorRegistryAdapterOnRealData(unittest.TestCase):
    def setUp(self):
        self.adapter = SectorRegistryAdapter()
        self.sector_names = list(set(COMPANY_SECTOR_MAP.values()))

    def test_produces_one_sector_per_distinct_name(self):
        sectors = self.adapter.normalize(self.sector_names)
        self.assertEqual(len(sectors), len(self.sector_names))

    def test_no_duplicate_sector_ids(self):
        sectors = self.adapter.normalize(self.sector_names)
        ids = [s.sector_id for s in sectors]
        self.assertEqual(len(ids), len(set(ids)))

    def test_real_technology_sector_present(self):
        sectors = self.adapter.normalize(self.sector_names)
        names = {s.name for s in sectors}
        self.assertIn("Technology", names)

    def test_duplicate_input_names_collapse_to_one_sector(self):
        sectors = self.adapter.normalize(["Technology", "Technology", "Energy"])
        self.assertEqual(len(sectors), 2)


class TestCompanyRegistryAdapterOnRealData(unittest.TestCase):
    def setUp(self):
        self.adapter = CompanyRegistryAdapter(sector_map=COMPANY_SECTOR_MAP)

    def test_produces_one_tuple_per_company(self):
        results = self.adapter.normalize(COMPANY_REGISTRY)
        self.assertEqual(len(results), len(COMPANY_REGISTRY))

    def test_every_result_is_a_4_tuple(self):
        results = self.adapter.normalize(COMPANY_REGISTRY[:5])
        for company, security, instrument, exchange in results:
            self.assertIsNotNone(company)
            self.assertIsNotNone(security)
            self.assertIsNotNone(instrument)
            self.assertIsNotNone(exchange)

    def test_security_correctly_linked_to_company(self):
        results = self.adapter.normalize(COMPANY_REGISTRY)
        for company, security, instrument, exchange in results:
            self.assertEqual(security.company_id, company.company_id)

    def test_instrument_correctly_linked_to_security(self):
        results = self.adapter.normalize(COMPANY_REGISTRY)
        for company, security, instrument, exchange in results:
            self.assertEqual(instrument.security_id, security.security_id)

    def test_no_duplicate_company_ids_across_entire_real_registry(self):
        results = self.adapter.normalize(COMPANY_REGISTRY)
        ids = [company.company_id for company, _, _, _ in results]
        self.assertEqual(len(ids), len(set(ids)))

    def test_nvidia_maps_correctly(self):
        nvidia_entry = next(e for e in COMPANY_REGISTRY if e["canonical_name"] == "Nvidia")
        company, security, instrument, exchange = self.adapter.normalize([nvidia_entry])[0]
        self.assertEqual(company.canonical_name, "Nvidia")
        self.assertEqual(instrument.ticker, "NVDA")
        self.assertEqual(instrument.asset_class, AssetClass.STOCK)
        self.assertEqual(company.sector_id, "technology")

    def test_electrica_and_estee_lauder_share_ticker_but_not_identity(self):
        # The real, documented "EL" collision — confirms the new model
        # keeps them structurally distinct even though the OLD
        # ticker_registry.py flags them as sharing a bare ticker string.
        electrica_entry = next(e for e in COMPANY_REGISTRY if e["canonical_name"] == "Electrica")
        estee_entry = next(e for e in COMPANY_REGISTRY if e["canonical_name"] == "Estee Lauder")
        self.assertEqual(electrica_entry["ticker"], estee_entry["ticker"])  # both "EL", confirmed

        electrica = self.adapter.normalize([electrica_entry])[0]
        estee = self.adapter.normalize([estee_entry])[0]
        self.assertNotEqual(electrica[2].identity_key(), estee[2].identity_key())
        self.assertNotEqual(electrica[0].company_id, estee[0].company_id)

    def test_crypto_company_gets_crypto_asset_class(self):
        bitcoin_entry = next(e for e in COMPANY_REGISTRY if e["canonical_name"] == "Bitcoin")
        _, _, instrument, exchange = self.adapter.normalize([bitcoin_entry])[0]
        self.assertEqual(instrument.asset_class, AssetClass.CRYPTO)
        self.assertEqual(exchange.exchange_id, "CRYPTO")

    def test_bvb_company_gets_bvb_exchange(self):
        bt_entry = next(e for e in COMPANY_REGISTRY if e["canonical_name"] == "Banca Transilvania")
        _, _, instrument, exchange = self.adapter.normalize([bt_entry])[0]
        self.assertEqual(exchange.exchange_id, "BVB")

    def test_company_with_no_sector_mapping_gets_none(self):
        fake_entry = {"canonical_name": "Totally Unmapped Co", "aliases": [], "ticker": "TUC", "category": "stocks"}
        company, _, _, _ = self.adapter.normalize([fake_entry])[0]
        self.assertIsNone(company.sector_id)


class TestNewsSourceRegistryAdapterOnRealData(unittest.TestCase):
    def setUp(self):
        self.adapter = NewsSourceRegistryAdapter(tier_map=SOURCE_TIERS)

    def test_produces_one_source_per_feed(self):
        sources = self.adapter.normalize(RSS_FEEDS)
        self.assertEqual(len(sources), len(RSS_FEEDS))

    def test_no_duplicate_source_ids(self):
        sources = self.adapter.normalize(RSS_FEEDS)
        ids = [s.source_id for s in sources]
        self.assertEqual(len(ids), len(set(ids)))

    def test_federal_reserve_classified_as_official(self):
        sources = self.adapter.normalize(RSS_FEEDS)
        fed = next(s for s in sources if s.name == "Federal Reserve Press Releases")
        self.assertEqual(fed.source_type, SourceType.OFFICIAL)

    def test_decrypt_classified_as_specialized(self):
        sources = self.adapter.normalize(RSS_FEEDS)
        decrypt = next(s for s in sources if s.name == "Decrypt")
        self.assertEqual(decrypt.source_type, SourceType.SPECIALIZED_OR_AGGREGATOR)

    def test_url_preserved(self):
        sources = self.adapter.normalize(RSS_FEEDS)
        for source, feed in zip(sources, RSS_FEEDS):
            self.assertEqual(source.url, feed["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
