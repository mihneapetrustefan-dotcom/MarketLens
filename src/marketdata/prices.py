"""
src/marketdata/prices.py
-------------------------------------------
The price boundary (Phase 25.7, §22, §23, §24).

THE ONE RULE
----------------
There are two price sources in this project and they answer different
questions:

    OPERATIONAL   market_data_state    "what is it worth right now"
    RESEARCH      price_candle_cache   "what did it close at, reproducibly"

A trading consumer must never receive the second while believing it
got the first. That single confusion -- IBKR unavailable, fall back to
a five-day-old research close, present it as current, trade on it --
is the forbidden behaviour named in §22.

HOW IT IS PREVENTED
-----------------------
Structurally, not by discipline. `operational_price()` reads ONLY
`market_data_state` and returns None when there is nothing fresh. It
has no access to the research cache at all, so there is no fallback
for it to take. A research consumer that wants a cached close calls
`research_price()`, which always returns the age and the source
alongside the number, so a caller cannot receive one without the
other.

`PriceSource` names which question was asked. Every result carries it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, Sequence

from src.domain.market_data_models import (
    MarketDataAvailability, OPERATIONAL_FRESHNESS, OperationalQuote,
)
from src.domain.paper_models import DataFreshness, FreshnessPolicy


class PriceSource(str, Enum):
    """Which question a price answers."""
    OPERATIONAL = "operational"      # live venue state, for trading
    RESEARCH = "research"            # cached history, for reproducibility


@dataclass
class PriceQuote:
    """
    A price with everything needed to decide whether to trust it.

    `price` alone is never returned by this module. A bare float is
    exactly what allows a stale research close to be mistaken for a
    current quote, so the age, the source and the freshness always
    travel with it.
    """
    instrument_id: str
    price: Optional[float]
    source: PriceSource
    freshness: DataFreshness
    as_of: Optional[datetime] = None
    age_seconds: Optional[float] = None
    availability: str = "unknown"
    note: str = ""

    @property
    def is_tradeable(self) -> bool:
        """
        Only OPERATIONAL data ever backs an order.

        A research price is refused here regardless of how recent it
        looks: recency is not the point, provenance is. A daily close
        from this morning is still not the current market.
        """
        return (self.source is PriceSource.OPERATIONAL
                and self.availability == "available"
                and self.freshness.is_tradeable
                and self.price is not None)


def operational_price(conn: sqlite3.Connection, instrument_id: str,
                      now: Optional[datetime] = None,
                      policy: FreshnessPolicy = OPERATIONAL_FRESHNESS
                      ) -> PriceQuote:
    """
    The current market price, or an explicit absence.

    Reads `market_data_state` and nothing else. Freshness is re-judged
    against `now` rather than trusting the verdict stored at write
    time, because that verdict was true when written and says nothing
    about this moment -- which is the whole failure mode after a
    restart or an idle gap.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        row = conn.execute("""
            SELECT last, bid, ask, mid, availability, broker_at, received_at,
                   note
            FROM market_data_state WHERE instrument_id = ?
        """, (instrument_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return PriceQuote(
                instrument_id=instrument_id, price=None,
                source=PriceSource.OPERATIONAL,
                freshness=DataFreshness.UNAVAILABLE,
                note="market_data_state does not exist; the market-data "
                     "service has never run against this database")
        raise

    if row is None:
        return PriceQuote(
            instrument_id=instrument_id, price=None,
            source=PriceSource.OPERATIONAL,
            freshness=DataFreshness.UNAVAILABLE,
            note="no operational state recorded for this instrument")

    last, bid, ask, mid, availability, broker_at, received_at, note = row
    price = mid if mid is not None else last
    reference = None
    for raw in (broker_at, received_at):
        if raw:
            try:
                reference = datetime.fromisoformat(str(raw))
                break
            except ValueError:
                continue

    # Freshness is decided by OperationalQuote and nowhere else.
    #
    # Reimplementing the rules here produced two answers for one quote:
    # the acquisition cycle counted an UNKNOWN-availability quote as
    # unavailable while this function called it fresh. Two definitions
    # of fresh is precisely what a market-data layer must not have, so
    # the row is rebuilt into the domain object and asked.
    try:
        resolved = MarketDataAvailability(str(availability or "unknown"))
    except ValueError:
        resolved = MarketDataAvailability.UNKNOWN

    quote = OperationalQuote(
        instrument_id=instrument_id, last=last, bid=bid, ask=ask, mid=mid,
        availability=resolved,
        broker_at=reference if str(broker_at or "") else None,
        received_at=reference, evaluated_at=now, note=note or "")
    freshness = quote.freshness(now, policy)
    age = quote.age_seconds(now)

    return PriceQuote(
        instrument_id=instrument_id, price=price,
        source=PriceSource.OPERATIONAL, freshness=freshness,
        as_of=reference, age_seconds=age,
        availability=resolved.value, note=note or "")


def research_price(conn: sqlite3.Connection, instrument_id: str,
                   now: Optional[datetime] = None,
                   interval: str = "1d") -> PriceQuote:
    """
    The most recent CACHED close, for research consumers.

    Always labelled `PriceSource.RESEARCH` and always carrying its age,
    so it can be reported honestly and can never satisfy
    `is_tradeable`. This exists so a research consumer has a supported
    way to ask -- not as a fallback for a trading consumer.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        row = conn.execute("""
            SELECT close, timestamp FROM price_candle_cache
            WHERE instrument_id = ? AND interval = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (instrument_id, interval)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error).lower():
            return PriceQuote(
                instrument_id=instrument_id, price=None,
                source=PriceSource.RESEARCH,
                freshness=DataFreshness.UNAVAILABLE,
                note="price_candle_cache does not exist")
        raise

    if row is None or row[0] is None:
        return PriceQuote(
            instrument_id=instrument_id, price=None,
            source=PriceSource.RESEARCH,
            freshness=DataFreshness.UNAVAILABLE,
            note="no cached candle for this instrument")

    close, stamp = row
    reference = None
    try:
        reference = datetime.fromisoformat(str(stamp))
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    age = ((now - reference).total_seconds()
           if reference is not None else None)
    return PriceQuote(
        instrument_id=instrument_id, price=close,
        source=PriceSource.RESEARCH,
        #: Deliberately never FRESH. A cached research close is a
        #: historical fact; calling it fresh would be the exact
        #: confusion this module exists to prevent.
        freshness=DataFreshness.AGING if age is not None else DataFreshness.UNAVAILABLE,
        as_of=reference, age_seconds=age, availability="research_cache",
        note="cached research candle, NOT a current market price")


def operational_prices(conn: sqlite3.Connection,
                       instrument_ids: Sequence[str],
                       now: Optional[datetime] = None
                       ) -> Dict[str, PriceQuote]:
    """Current prices for several instruments, absences included."""
    return {instrument_id: operational_price(conn, instrument_id, now)
            for instrument_id in instrument_ids}


def tradeable_prices(conn: sqlite3.Connection,
                     instrument_ids: Sequence[str],
                     now: Optional[datetime] = None
                     ) -> Dict[str, float]:
    """
    Only the prices that may back an order.

    Instruments whose data is stale, delayed or missing are ABSENT
    from the result rather than present with a suspect number. A caller
    iterating this cannot accidentally act on one.
    """
    result: Dict[str, float] = {}
    for instrument_id, quote in operational_prices(
            conn, instrument_ids, now).items():
        if quote.is_tradeable and quote.price is not None:
            result[instrument_id] = quote.price
    return result
