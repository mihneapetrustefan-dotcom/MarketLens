"""
tests/domain/test_models.py
--------------------------------
Tests protecting the Phase 1 canonical data contracts: timezone
enforcement, Company/Security/Instrument separation, and Decimal
usage for financial figures.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.models import (
    Company, Security, Instrument, Exchange, Sector, Industry,
    NewsSource, NewsArticle, Event, EconomicEvent, MarketObservation, CorporateAction,
)
from src.domain.enums import AssetClass, InstrumentType, SourceType, EventStatus, EventDirection


UTC_NOW = datetime.now(timezone.utc)
BUCHAREST_NOW = UTC_NOW.astimezone(timezone(timedelta(hours=3)))  # a real, non-UTC-but-aware offset


class TestExchangeSectorCompany(unittest.TestCase):
    def test_exchange_creation(self):
        ex = Exchange(exchange_id="NASDAQ", name="Nasdaq Stock Market", country="US")
        self.assertEqual(ex.exchange_id, "NASDAQ")

    def test_sector_creation(self):
        sector = Sector(sector_id="technology", name="Technology")
        self.assertEqual(sector.name, "Technology")

    def test_industry_references_sector(self):
        industry = Industry(industry_id="semiconductors", name="Semiconductors", sector_id="technology")
        self.assertEqual(industry.sector_id, "technology")

    def test_company_creation_with_aliases(self):
        company = Company(company_id="nvidia", canonical_name="Nvidia", aliases=["Nvidia"], sector_id="technology")
        self.assertIn("Nvidia", company.aliases)

    def test_company_without_sector_is_allowed(self):
        company = Company(company_id="x", canonical_name="X Corp")
        self.assertIsNone(company.sector_id)


class TestCompanySecurityInstrumentSeparation(unittest.TestCase):
    """The core structural fix this phase introduces: ticker is never a universal identity."""

    def test_security_independent_of_ticker(self):
        security = Security(security_id="nvidia-common", company_id="nvidia", instrument_type=InstrumentType.COMMON_STOCK)
        self.assertEqual(security.instrument_type, InstrumentType.COMMON_STOCK)
        self.assertFalse(hasattr(security, "ticker"))  # ticker does NOT belong on Security

    def test_instrument_carries_the_ticker_and_exchange(self):
        instrument = Instrument(
            instrument_id="nasdaq-nvda", security_id="nvidia-common",
            exchange_id="NASDAQ", ticker="NVDA", asset_class=AssetClass.STOCK,
        )
        self.assertEqual(instrument.ticker, "NVDA")
        self.assertEqual(instrument.exchange_id, "NASDAQ")

    def test_identity_key_combines_exchange_and_ticker(self):
        instrument = Instrument(
            instrument_id="i1", security_id="s1", exchange_id="BVB", ticker="EL", asset_class=AssetClass.BVB,
        )
        self.assertEqual(instrument.identity_key(), "BVB:EL")

    def test_same_ticker_different_exchange_are_distinct_instruments(self):
        # The real, documented "EL" collision (Electrica/BVB vs Estee Lauder/NYSE)
        # this split is designed to make structurally unambiguous.
        electrica = Instrument(instrument_id="i1", security_id="s-electrica", exchange_id="BVB", ticker="EL", asset_class=AssetClass.BVB)
        estee = Instrument(instrument_id="i2", security_id="s-estee", exchange_id="NYSE", ticker="EL", asset_class=AssetClass.STOCK)
        self.assertNotEqual(electrica.identity_key(), estee.identity_key())

    def test_one_company_can_have_multiple_securities(self):
        common = Security(security_id="c-common", company_id="c", instrument_type=InstrumentType.COMMON_STOCK)
        adr = Security(security_id="c-adr", company_id="c", instrument_type=InstrumentType.COMMON_STOCK, currency="USD")
        self.assertEqual(common.company_id, adr.company_id)
        self.assertNotEqual(common.security_id, adr.security_id)

    def test_security_can_have_no_underlying_company(self):
        # Indices/commodities/forex have no Company at all.
        index_security = Security(security_id="sp500", company_id=None, instrument_type=InstrumentType.INDEX)
        self.assertIsNone(index_security.company_id)


class TestTimezoneEnforcement(unittest.TestCase):
    """Naive or non-UTC timestamps must be rejected, not silently accepted."""

    def test_utc_timestamp_accepted(self):
        article = NewsArticle(article_id="a1", source_id="s1", published_at=UTC_NOW)
        self.assertEqual(article.published_at, UTC_NOW)

    def test_naive_timestamp_rejected(self):
        naive = datetime(2026, 8, 1, 9, 0, 0)
        with self.assertRaises(ValueError):
            NewsArticle(article_id="a1", source_id="s1", published_at=naive)

    def test_non_utc_aware_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            NewsArticle(article_id="a1", source_id="s1", published_at=BUCHAREST_NOW)

    def test_none_timestamp_allowed(self):
        article = NewsArticle(article_id="a1", source_id="s1", published_at=None)
        self.assertIsNone(article.published_at)

    def test_event_detected_at_enforced(self):
        with self.assertRaises(ValueError):
            Event(event_id="e1", event_type="EARNINGS", detected_at=datetime(2026, 8, 1))

    def test_market_observation_observed_at_enforced(self):
        with self.assertRaises(ValueError):
            MarketObservation(instrument_id="i1", observed_at=datetime(2026, 8, 1), timeframe="1d")

    def test_corporate_action_effective_date_enforced(self):
        with self.assertRaises(ValueError):
            CorporateAction(corporate_action_id="ca1", security_id="s1", action_type="split", effective_date=datetime(2026, 8, 1))

    def test_economic_event_allows_either_or_neither_timestamp(self):
        scheduled = EconomicEvent(economic_event_id="fomc-1", name="FOMC Meeting", scheduled_at=UTC_NOW)
        published = EconomicEvent(economic_event_id="gdp-1", name="GDP", published_at=UTC_NOW)
        self.assertIsNotNone(scheduled.scheduled_at)
        self.assertIsNone(scheduled.published_at)
        self.assertIsNotNone(published.published_at)


class TestFinancialPrecision(unittest.TestCase):
    """Monetary/price figures must use Decimal, never float, per the phase's explicit requirement."""

    def test_market_observation_close_is_decimal(self):
        obs = MarketObservation(
            instrument_id="i1", observed_at=UTC_NOW, timeframe="1d",
            close=Decimal("205.10"),
        )
        self.assertIsInstance(obs.close, Decimal)

    def test_event_magnitude_and_confidence_are_decimal(self):
        event = Event(
            event_id="e1", event_type="EARNINGS", detected_at=UTC_NOW,
            magnitude=Decimal("0.87"), confidence=Decimal("0.91"),
        )
        self.assertIsInstance(event.magnitude, Decimal)
        self.assertIsInstance(event.confidence, Decimal)

    def test_economic_event_value_is_decimal(self):
        econ = EconomicEvent(economic_event_id="unrate-1", name="Unemployment Rate", value=Decimal("4.1"))
        self.assertIsInstance(econ.value, Decimal)


class TestNewsSourceAndArticle(unittest.TestCase):
    def test_news_source_creation(self):
        source = NewsSource(source_id="reuters", name="Reuters", source_type=SourceType.WIRE_OR_MAJOR_PRESS)
        self.assertEqual(source.source_type, SourceType.WIRE_OR_MAJOR_PRESS)

    def test_news_source_defaults_to_unclassified(self):
        source = NewsSource(source_id="new-blog", name="Some New Blog")
        self.assertEqual(source.source_type, SourceType.UNCLASSIFIED)

    def test_article_defaults_to_empty_entity_and_event_lists(self):
        article = NewsArticle(article_id="a1", source_id="s1")
        self.assertEqual(article.entity_ids, [])
        self.assertEqual(article.event_ids, [])

    def test_article_entity_lists_are_independent_between_instances(self):
        # Guards against the classic mutable-default-argument bug.
        a1 = NewsArticle(article_id="a1", source_id="s1")
        a2 = NewsArticle(article_id="a2", source_id="s1")
        a1.entity_ids.append("nvidia")
        self.assertEqual(a2.entity_ids, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
