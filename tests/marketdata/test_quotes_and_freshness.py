"""
tests/marketdata/test_quotes_and_freshness.py
-----------------------------------------------------------
Quote acquisition and freshness (Phase 25.7, §36 A and B).

These cases exist because every one of them is a way a market-data
layer lies: a delayed price read as live, a quote with no age read as
current, a clock-skewed quote read as recent, a cold contract read as
an absent market.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.market_data_models import (
    MarketDataAvailability, OPERATIONAL_FRESHNESS, OperationalQuote,
    UniverseEntry,
)
from src.domain.paper_models import DataFreshness
from src.execution.adapters.ibkr.errors import IBKRError, IBKRErrorCategory
from src.marketdata import quotes as acquisition

NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def entry(instrument_id="us_and_intl-aapl", conid="265598"):
    return UniverseEntry(instrument_id=instrument_id, conid=conid,
                         broker_symbol="AAPL", venue="NASDAQ")


def quote(**overrides):
    base = dict(instrument_id="us_and_intl-aapl", conid="265598",
                last=100.0, bid=99.9, ask=100.1, mid=100.0,
                availability=MarketDataAvailability.AVAILABLE,
                broker_at=NOW, received_at=NOW, evaluated_at=NOW)
    base.update(overrides)
    return OperationalQuote(**base)


class _Transport:
    """A transport double that returns exactly what a test dictates."""

    name = "double"

    def __init__(self, payloads=None, error=None):
        self.payloads = payloads if payloads is not None else []
        self.error = error
        self.calls = []

    def market_snapshot(self, conids, fields=()):
        self.calls.append(list(conids))
        if self.error is not None:
            raise self.error
        return self.payloads


class TestFreshnessClassification(unittest.TestCase):

    def test_a_recent_live_quote_is_fresh_and_tradeable(self):
        q = quote()
        self.assertIs(q.freshness(NOW), DataFreshness.FRESH)
        self.assertTrue(q.is_tradeable(NOW))

    def test_an_old_quote_is_stale_and_not_tradeable(self):
        """Inside the stale band: aged past usefulness, still recognised."""
        q = quote(broker_at=NOW - timedelta(seconds=600))
        self.assertIs(q.freshness(NOW), DataFreshness.STALE)
        self.assertFalse(q.is_tradeable(NOW))

    def test_beyond_the_stale_band_it_is_invalid(self):
        """
        Phase 13's own semantics, reused rather than redefined: past
        `stale_seconds` the reading stops being a late observation and
        becomes one that should not be interpreted at all.
        """
        beyond = OPERATIONAL_FRESHNESS.stale_seconds + 60
        q = quote(broker_at=NOW - timedelta(seconds=beyond))
        self.assertIs(q.freshness(NOW), DataFreshness.INVALID)
        self.assertFalse(q.is_tradeable(NOW))

    def test_the_boundary_is_inclusive_at_fresh(self):
        edge = OPERATIONAL_FRESHNESS.fresh_seconds
        self.assertIs(quote(broker_at=NOW - timedelta(seconds=edge - 1)
                            ).freshness(NOW), DataFreshness.FRESH)
        self.assertIs(quote(broker_at=NOW - timedelta(seconds=edge + 1)
                            ).freshness(NOW), DataFreshness.AGING)

    def test_a_delayed_quote_is_never_fresh_however_recent(self):
        """
        The failure this prevents: a delayed price backing a limit
        order because it arrived one second ago.
        """
        q = quote(availability=MarketDataAvailability.DELAYED)
        self.assertIs(q.freshness(NOW), DataFreshness.AGING)
        self.assertFalse(q.is_tradeable(NOW))

    def test_a_quote_with_no_timestamp_has_no_freshness(self):
        q = quote(broker_at=None, received_at=None)
        self.assertIs(q.freshness(NOW), DataFreshness.UNAVAILABLE)
        self.assertFalse(q.is_tradeable(NOW))

    def test_a_negative_age_is_invalid_not_fresh(self):
        """Clock skew or a malformed payload. Never tradeable."""
        q = quote(broker_at=NOW + timedelta(seconds=120))
        self.assertIs(q.freshness(NOW), DataFreshness.INVALID)
        self.assertFalse(q.is_tradeable(NOW))

    def test_a_quote_with_no_price_is_invalid(self):
        q = quote(last=None, bid=None, ask=None, mid=None)
        self.assertIs(q.freshness(NOW), DataFreshness.INVALID)

    def test_unknown_availability_is_not_assumed_live(self):
        q = quote(availability=MarketDataAvailability.UNKNOWN)
        self.assertIs(q.freshness(NOW), DataFreshness.UNAVAILABLE)
        self.assertFalse(q.is_tradeable(NOW))

    def test_restricted_availability_is_unavailable(self):
        q = quote(availability=MarketDataAvailability.RESTRICTED)
        self.assertFalse(q.is_tradeable(NOW))

    def test_mid_is_preferred_over_last_and_never_invented(self):
        self.assertEqual(quote(mid=101.0, last=99.0).reference_price, 101.0)
        self.assertEqual(quote(mid=None, last=99.0).reference_price, 99.0)
        self.assertIsNone(quote(mid=None, last=None).reference_price)


class TestAvailabilityParsing(unittest.TestCase):
    """IBKR field 6509 carries the live/delayed marker."""

    def test_realtime_marker_is_available(self):
        self.assertIs(acquisition._availability_of({"6509": "RB"}),
                      MarketDataAvailability.AVAILABLE)

    def test_delayed_marker_is_delayed(self):
        self.assertIs(acquisition._availability_of({"6509": "DZ"}),
                      MarketDataAvailability.DELAYED)

    def test_a_missing_marker_is_unknown_not_live(self):
        """Assuming live is how delayed data backs an order."""
        self.assertIs(acquisition._availability_of({}),
                      MarketDataAvailability.UNKNOWN)


class TestAcquisition(unittest.TestCase):

    def test_the_whole_universe_costs_one_request(self):
        """
        Batching is the reason a 60s cycle fits a 50/min budget.
        """
        transport = _Transport(payloads=[])
        entries = [entry("a", "1"), entry("b", "2"), entry("c", "3")]
        _quotes, requests, _error = acquisition.acquire(
            transport, entries, NOW)
        self.assertEqual(requests, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sorted(transport.calls[0]), ["1", "2", "3"])

    def test_every_requested_instrument_comes_back(self):
        """
        A missing instrument must be present-and-unavailable, never
        simply absent: a caller counting results would otherwise read a
        blind instrument as one it never asked about.
        """
        transport = _Transport(payloads=[
            {"conid": "1", "31": 100.0, "6509": "RB"}])
        entries = [entry("a", "1"), entry("b", "2")]
        results, _r, _e = acquisition.acquire(transport, entries, NOW)
        self.assertEqual(len(results), 2)
        by_id = {q.instrument_id: q for q in results}
        self.assertTrue(by_id["a"].is_tradeable(NOW))
        self.assertIs(by_id["b"].availability,
                      MarketDataAvailability.UNAVAILABLE)
        self.assertIn("not present", by_id["b"].note)

    def test_a_cold_contract_is_unavailable_not_priced(self):
        """
        Measured live: IBKR's first snapshot for an unsubscribed conid
        returns no fields. It must never be filled in from history.
        """
        transport = _Transport(payloads=[{"conid": "1", "6509": "RB"}])
        results, _r, _e = acquisition.acquire(transport, [entry("a", "1")], NOW)
        self.assertIs(results[0].availability,
                      MarketDataAvailability.UNAVAILABLE)
        self.assertIn("cold", results[0].note)
        self.assertIsNone(results[0].reference_price)

    def test_a_transport_failure_blinds_the_universe_explicitly(self):
        transport = _Transport(error=IBKRError(
            category=IBKRErrorCategory.CONNECTION_ERROR,
            message="gateway down", endpoint="/x"))
        entries = [entry("a", "1"), entry("b", "2")]
        results, requests, error = acquisition.acquire(transport, entries, NOW)
        self.assertEqual(error, "gateway down")
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertIs(result.availability,
                          MarketDataAvailability.UNAVAILABLE)
            self.assertFalse(result.is_tradeable(NOW))

    def test_an_empty_universe_sends_no_request(self):
        transport = _Transport()
        results, requests, _e = acquisition.acquire(transport, [], NOW)
        self.assertEqual(results, [])
        self.assertEqual(requests, 0)
        self.assertEqual(transport.calls, [])

    def test_an_excluded_entry_is_never_polled(self):
        transport = _Transport()
        blocked = UniverseEntry(instrument_id="bvb-tlv", conid="",
                                excluded_reason="no resolved IBKR contract")
        acquisition.acquire(transport, [blocked], NOW)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
