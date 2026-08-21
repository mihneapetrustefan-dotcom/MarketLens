"""
market_instruments.py
-------------------------
Registry of macro market instruments MarketLens tracks purely for
CURRENT PRICE DISPLAY — Indices and Commodities.

WHY A SEPARATE REGISTRY FROM company_registry.py / ticker_registry.py:
these are NOT companies with news-detectable names ("gold" and "oil"
are far too generic/ambiguous to safely detect as text mentions, the
same category of risk already flagged for short/generic aliases
elsewhere in this project) — they're macro instruments whose CURRENT
price is useful context regardless of whether any article mentions
them today. This registry exists purely to drive a factual "Prezentare
macro" price table (same "facts, no verdict" philosophy as the
existing Date de piață table) — NOT to feed Company/Ticker Detector.

Each entry:
- name: human-readable display name
- yfinance_ticker: the exact symbol yfinance expects (indices use a
  "^" prefix, commodity futures use a "=F" suffix — both yfinance's
  own conventions, verified directly, not guessed)
- category: "index" or "commodity"

VERIFICATION NOTE: every ticker below is a long-standing, extremely
widely-used yfinance symbol (the same ones used throughout financial
tooling generally) — high confidence, though not cross-checked live
in this environment (no internet access here) the same honesty
standard already applied to less certain entries elsewhere in this
project (e.g. BVB tickers, some international ADRs).
"""

from typing import List, Dict, Any

INDICES: List[Dict[str, Any]] = [
    {"name": "S&P 500", "yfinance_ticker": "^GSPC", "category": "index"},
    {"name": "Dow Jones Industrial Average", "yfinance_ticker": "^DJI", "category": "index"},
    {"name": "Nasdaq Composite", "yfinance_ticker": "^IXIC", "category": "index"},
    {"name": "Russell 2000", "yfinance_ticker": "^RUT", "category": "index"},
    {"name": "CBOE Volatility Index (VIX)", "yfinance_ticker": "^VIX", "category": "index"},
    {"name": "FTSE 100", "yfinance_ticker": "^FTSE", "category": "index"},
    {"name": "DAX (Germania)", "yfinance_ticker": "^GDAXI", "category": "index"},
    {"name": "Nikkei 225", "yfinance_ticker": "^N225", "category": "index"},
]

COMMODITIES: List[Dict[str, Any]] = [
    {"name": "Aur", "yfinance_ticker": "GC=F", "category": "commodity"},
    {"name": "Argint", "yfinance_ticker": "SI=F", "category": "commodity"},
    {"name": "Petrol brut (WTI)", "yfinance_ticker": "CL=F", "category": "commodity"},
    {"name": "Petrol Brent", "yfinance_ticker": "BZ=F", "category": "commodity"},
    {"name": "Gaze naturale", "yfinance_ticker": "NG=F", "category": "commodity"},
    {"name": "Cupru", "yfinance_ticker": "HG=F", "category": "commodity"},
]

MARKET_INSTRUMENTS: List[Dict[str, Any]] = INDICES + COMMODITIES
