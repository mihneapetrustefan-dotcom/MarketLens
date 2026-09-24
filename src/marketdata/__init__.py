"""
src/marketdata/
-------------------------------------------
The operational market-data layer (Phase 25.7).

The system's eyes on the market: it observes current prices during a
session, knows how fresh each observation is, stores them separately
from the research corpus, and exposes them to portfolio and risk.

It does not decide trades, place orders, or touch `price_candle_cache`.
"""

from src.marketdata.prices import (
    PriceQuote, PriceSource, operational_price, operational_prices,
    research_price, tradeable_prices,
)
from src.marketdata.service import (
    DEFAULT_INTERVAL_SECONDS, MarketDataService, MarketSessionView,
    cycle_id_for, session_id_for,
)

__all__ = [
    "MarketDataService", "MarketSessionView", "DEFAULT_INTERVAL_SECONDS",
    "session_id_for", "cycle_id_for",
    "PriceQuote", "PriceSource", "operational_price", "operational_prices",
    "research_price", "tradeable_prices",
]
