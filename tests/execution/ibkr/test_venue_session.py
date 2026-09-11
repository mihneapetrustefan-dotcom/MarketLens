"""
tests/execution/ibkr/test_venue_session.py
-----------------------------------------------------------
The venue as a second source for "is this market open".

WHY THIS EXISTS

`MarketCalendar.is_open` answers from cached daily bars: true only
where a bar carries that exact date. Nothing in this repository
fetches a bar for today -- `cache_price_candles.py` fetches windows
around canonical events, for reproducible event studies. So the
calendar can never confirm that TODAY's session is open, and every
live cycle would refuse on the session gate, permanently.

Measured on 2026-09-11 against the real gateway: the newest daily bar
for any instrument was 2026-09-05, while IBKR quoted us_and_intl-aapl
live at 334.37.

IBKR knows, because it is the venue. These cases pin down exactly how
far that answer is allowed to go.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from src.domain.broker_models import MarketStatus
from src.execution.adapters.ibkr.gateway import (
    IBKRGateway, MarketDataAvailability,
)

from tests.execution.ibkr.helpers import (
    AlwaysOpenCalendar, ClosedCalendar, INSTRUMENT, build_ibkr,
)


class _NoDataCalendar(ClosedCalendar):
    """A calendar that holds nothing for the instrument."""

    def has_data(self, instrument_id) -> bool:
        return False


class TestTheCalendarStillLeads(unittest.TestCase):

    def test_an_open_calendar_is_returned_unchanged(self):
        stack = build_ibkr(calendar=AlwaysOpenCalendar())
        status = stack["gateway"].market_status(
            INSTRUMENT, datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.OPEN)

    def test_an_unresolved_instrument_is_unknown_before_anything_else(self):
        """No contract means no opinion, from either source."""
        stack = build_ibkr(resolve=False, calendar=AlwaysOpenCalendar())
        status = stack["gateway"].market_status(
            "i-never-resolved", datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.UNKNOWN)


class TestTheVenueCanOnlyMoveTowardsOpen(unittest.TestCase):

    def test_a_closed_calendar_plus_a_live_quote_is_open(self):
        """
        The production case: stale bars say closed, the venue is
        trading.
        """
        stack = build_ibkr(calendar=ClosedCalendar())
        status = stack["gateway"].market_status(
            INSTRUMENT, datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.OPEN)

    def test_a_calendar_with_no_data_plus_a_live_quote_is_open(self):
        stack = build_ibkr(calendar=_NoDataCalendar())
        status = stack["gateway"].market_status(
            INSTRUMENT, datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.OPEN)

    def test_a_delayed_quote_does_not_open_a_closed_market(self):
        """
        DELAYED is explicitly not tradeable. Treating it as proof of a
        session is how a delayed price ends up backing a limit order.
        """
        stack = build_ibkr(calendar=ClosedCalendar())
        gateway = stack["gateway"]
        original = gateway.quote

        def delayed(instrument_id, now):
            quote = original(instrument_id, now)
            if quote is not None:
                quote.availability = MarketDataAvailability.DELAYED
            return quote

        gateway.quote = delayed
        status = gateway.market_status(INSTRUMENT, datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.CLOSED)

    def test_no_quote_at_all_leaves_the_calendar_verdict_standing(self):
        stack = build_ibkr(calendar=ClosedCalendar())
        gateway = stack["gateway"]
        gateway.quote = lambda instrument_id, now: None
        status = gateway.market_status(INSTRUMENT, datetime.now(timezone.utc))
        self.assertIs(status, MarketStatus.CLOSED)

    def test_a_failing_quote_is_not_evidence_that_the_market_shut(self):
        """A transport failure is a fact about us, not about the venue."""
        stack = build_ibkr(calendar=_NoDataCalendar())
        gateway = stack["gateway"]

        def boom(instrument_id, now):
            raise RuntimeError("transport exploded")

        gateway.quote = boom
        with self.assertRaises(RuntimeError):
            gateway.market_status(INSTRUMENT, datetime.now(timezone.utc))


class TestAQuoteOnlyDescribesNow(unittest.TestCase):

    def test_a_historical_anchor_does_not_consult_the_venue(self):
        """
        A replay, a backtest or an --as-of evaluation asks about
        another moment. The venue has nothing to say about it, and the
        calendar must remain the only source.
        """
        stack = build_ibkr(calendar=ClosedCalendar())
        gateway = stack["gateway"]
        asked = []
        gateway.quote = lambda instrument_id, now: asked.append(now)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        status = gateway.market_status(INSTRUMENT, past)
        self.assertIs(status, MarketStatus.CLOSED)
        self.assertEqual(asked, [], "the venue was asked about a past moment")

    def test_a_future_anchor_does_not_consult_the_venue_either(self):
        stack = build_ibkr(calendar=ClosedCalendar())
        gateway = stack["gateway"]
        asked = []
        gateway.quote = lambda instrument_id, now: asked.append(now)
        ahead = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertIs(gateway.market_status(INSTRUMENT, ahead),
                      MarketStatus.CLOSED)
        self.assertEqual(asked, [])


class TestBrokerageSessionInitIsBestEffort(unittest.TestCase):

    def test_a_transport_without_the_concept_reports_false(self):
        stack = build_ibkr()
        self.assertFalse(stack["transport"].init_brokerage_session())

    def test_connect_survives_a_failing_brokerage_init(self):
        """
        It must never break a path that already works.
        """
        stack = build_ibkr()
        gateway = stack["gateway"]

        def boom():
            raise RuntimeError("ssodh/init exploded")

        gateway.transport.init_brokerage_session = boom
        state = gateway.connect()
        self.assertIsNotNone(state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
