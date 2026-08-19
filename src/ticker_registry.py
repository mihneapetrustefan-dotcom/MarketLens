"""
ticker_registry.py
---------------------
Registry of known ticker symbols MarketLens can recognize directly in
text, covering all target markets: stocks, BVB, ETF, forex, crypto.
DESIGN DECISION: rather than duplicating ticker->name data that already
exists in company_registry.py, we DERIVE the stock/BVB/crypto portion
of this registry directly from COMPANY_REGISTRY (one source of truth
for "what is this ticker's name/category"). We then ADD entries that
have no natural "company" (ETFs track an index, not a company; forex
pairs are a currency exchange rate, not a company) — those are defined
here only.
Each entry:
- ticker: the exact symbol as it appears in text (e.g. "AAPL", "EURUSD")
- name: human-readable name of what the ticker represents
- category: "stocks" / "bvb" / "etf" / "forex" / "crypto"

CHANGE LOG (v1.3): no logic change — this file's derivation from
COMPANY_REGISTRY is automatic, so it grew from ~123 to 389 entries
purely as a side effect of company_registry.py's own v1.3 expansion.
A small number of additional ETFs were added directly below.
"""
from typing import List, Dict, Any
from company_registry import COMPANY_REGISTRY
# Derived automatically: one ticker entry per company that HAS a ticker
# (all of them currently do, but the check keeps this robust if a future
# company entry is added without one).
_FROM_COMPANIES: List[Dict[str, Any]] = [
    {"ticker": company["ticker"], "name": company["canonical_name"], "category": company["category"]}
    for company in COMPANY_REGISTRY
    if company.get("ticker")
]
# ETFs and forex pairs have no underlying "company", so they can't be
# derived from company_registry.py — they're defined directly here.
_ADDITIONAL_TICKERS: List[Dict[str, Any]] = [
    # --- ETFs ---
    {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "category": "etf"},
    {"ticker": "QQQ", "name": "Invesco QQQ Trust", "category": "etf"},
    {"ticker": "DIA", "name": "SPDR Dow Jones Industrial Average ETF", "category": "etf"},
    {"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "category": "etf"},
    {"ticker": "VTI", "name": "Vanguard Total Stock Market ETF", "category": "etf"},
    {"ticker": "IWM", "name": "iShares Russell 2000 ETF", "category": "etf"},
    {"ticker": "XLK", "name": "Technology Select Sector SPDR Fund", "category": "etf"},
    {"ticker": "XLE", "name": "Energy Select Sector SPDR Fund", "category": "etf"},
    {"ticker": "XLF", "name": "Financial Select Sector SPDR Fund", "category": "etf"},
    {"ticker": "GLD", "name": "SPDR Gold Shares", "category": "etf"},
    # --- Forex pairs ---
    {"ticker": "EURUSD", "name": "Euro / US Dollar", "category": "forex"},
    {"ticker": "GBPUSD", "name": "British Pound / US Dollar", "category": "forex"},
    {"ticker": "USDJPY", "name": "US Dollar / Japanese Yen", "category": "forex"},
    {"ticker": "EURRON", "name": "Euro / Romanian Leu", "category": "forex"},
    {"ticker": "USDRON", "name": "US Dollar / Romanian Leu", "category": "forex"},
]
TICKER_REGISTRY: List[Dict[str, Any]] = _FROM_COMPANIES + _ADDITIONAL_TICKERS
