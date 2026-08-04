"""
sector_registry.py
---------------------
Sector classification data for MarketLens.

Two complementary sources of sector information:

1. COMPANY_SECTOR_MAP — maps each company already known to Company
   Detector (by canonical_name) to its primary economic sector. This is
   the HIGH-CONFIDENCE path: if an article already mentions a known
   company, we already know precisely which one, so its sector follows
   directly — no guessing involved.

2. SECTOR_KEYWORDS — a FALLBACK path for articles that discuss a sector
   generically without naming any specific known company (e.g. "oil
   prices surge amid supply concerns" mentions no company but is
   clearly an Energy story). Matched with lower confidence than the
   company-based path, and only ever used when it doesn't.

Kept as a separate file so growing either list never requires touching
sector_detector.py's logic — the same "add a line, not code" principle
used by company_registry.py and ticker_registry.py.
"""

from typing import Dict, List

# Canonical company name (must match company_registry.py's
# canonical_name exactly) -> primary economic sector.
COMPANY_SECTOR_MAP: Dict[str, str] = {
    # --- BVB ---
    "Banca Transilvania": "Financial Services",
    "Hidroelectrica": "Energy",
    "OMV Petrom": "Energy",
    "Nuclearelectrica": "Energy",
    "Romgaz": "Energy",
    "Transgaz": "Utilities",
    "Electrica": "Utilities",
    "BRD - Groupe Societe Generale": "Financial Services",
    "Digi Communications": "Telecommunications",
    "Fondul Proprietatea": "Financial Services",
    "Purcari Wineries": "Consumer Goods",
    "MedLife": "Healthcare",
    "Sphera Franchise Group": "Consumer Goods",
    "TeraPlast": "Industrials",
    "One United Properties": "Real Estate",
    "Transelectrica": "Utilities",
    "Antibiotice": "Healthcare",
    "Aquila": "Industrials",
    "Bursa de Valori Bucuresti": "Financial Services",
    "Conpet": "Energy",
    "Alro": "Industrials",
    "Vrancart": "Industrials",
    "Bittnet Systems": "Technology",
    "Patria Bank": "Financial Services",

    # --- International: Technology ---
    "Apple": "Technology",
    "Microsoft": "Technology",
    "Alphabet": "Technology",
    "Meta Platforms": "Technology",
    "Nvidia": "Technology",
    "Intel": "Technology",
    "AMD": "Technology",
    "Oracle": "Technology",
    "Salesforce": "Technology",
    "Adobe": "Technology",
    "IBM": "Technology",
    "Qualcomm": "Technology",
    "Cisco": "Technology",
    "Uber Technologies": "Technology",
    "ServiceNow": "Technology",
    "Palantir Technologies": "Technology",
    "Shopify": "Technology",
    "Zoom Communications": "Technology",
    "Snowflake": "Technology",

    # --- International: Semiconductors ---
    "Broadcom": "Semiconductors",
    "Texas Instruments": "Semiconductors",
    "Micron Technology": "Semiconductors",
    "ASML Holding": "Semiconductors",

    # --- International: Automotive ---
    "Tesla": "Automotive",
    "Ford Motor Company": "Automotive",
    "General Motors": "Automotive",
    "Toyota": "Automotive",
    "Rivian Automotive": "Automotive",
    "Lucid Group": "Automotive",

    # --- International: Healthcare ---
    "Johnson & Johnson": "Healthcare",
    "Pfizer": "Healthcare",
    "UnitedHealth Group": "Healthcare",
    "Moderna": "Healthcare",
    "Eli Lilly": "Healthcare",
    "Abbott Laboratories": "Healthcare",
    "Merck & Co": "Healthcare",
    "Bristol Myers Squibb": "Healthcare",
    "CVS Health": "Healthcare",

    # --- International: Financial Services ---
    "JPMorgan Chase": "Financial Services",
    "Goldman Sachs": "Financial Services",
    "Bank of America": "Financial Services",
    "Wells Fargo": "Financial Services",
    "Visa": "Financial Services",
    "Mastercard": "Financial Services",
    "Morgan Stanley": "Financial Services",
    "Charles Schwab": "Financial Services",
    "American Express": "Financial Services",
    "BlackRock": "Financial Services",
    "PayPal": "Financial Services",

    # --- International: Consumer Goods & Retail ---
    "Amazon": "Retail & E-commerce",
    "Walmart": "Retail & E-commerce",
    "Costco": "Retail & E-commerce",
    "Target": "Retail & E-commerce",
    "Home Depot": "Retail & E-commerce",
    "Lowe's": "Retail & E-commerce",
    "Procter & Gamble": "Consumer Goods",
    "Coca-Cola": "Consumer Goods",
    "PepsiCo": "Consumer Goods",
    "Nike": "Consumer Goods",
    "McDonald's": "Consumer Goods",
    "Starbucks": "Consumer Goods",
    "Colgate-Palmolive": "Consumer Goods",

    # --- International: Energy ---
    "ExxonMobil": "Energy",
    "Chevron": "Energy",
    "Shell": "Energy",
    "ConocoPhillips": "Energy",
    "Occidental Petroleum": "Energy",

    # --- International: Media & Entertainment ---
    "Netflix": "Media & Entertainment",
    "Walt Disney": "Media & Entertainment",
    "Warner Bros Discovery": "Media & Entertainment",
    "Comcast": "Media & Entertainment",
    "Spotify": "Media & Entertainment",

    # --- International: Industrials ---
    "Boeing": "Industrials",
    "Caterpillar": "Industrials",
    "General Electric": "Industrials",
    "Honeywell": "Industrials",
    "3M": "Industrials",
    "Lockheed Martin": "Industrials",
    "RTX Corporation": "Industrials",

    # --- International: Airlines ---
    "Delta Air Lines": "Airlines & Aviation",
    "United Airlines": "Airlines & Aviation",
    "Southwest Airlines": "Airlines & Aviation",
    "American Airlines": "Airlines & Aviation",

    # --- International: Telecommunications ---
    "AT&T": "Telecommunications",
    "Verizon": "Telecommunications",
    "T-Mobile US": "Telecommunications",

    # --- Crypto ---
    "Bitcoin": "Cryptocurrency",
    "Ethereum": "Cryptocurrency",
    "Binance": "Cryptocurrency",
    "Coinbase": "Cryptocurrency",
    "Ripple": "Cryptocurrency",
    "Solana": "Cryptocurrency",
    "Cardano": "Cryptocurrency",
    "Dogecoin": "Cryptocurrency",
    "Polkadot": "Cryptocurrency",
    "Litecoin": "Cryptocurrency",
    "Avalanche": "Cryptocurrency",
    "Chainlink": "Cryptocurrency",
    "Polygon": "Cryptocurrency",
}

# Fallback keyword/phrase lists (English + Romanian) per sector, used
# ONLY when no known company was found in the article. Intentionally
# short and high-precision (whole words/phrases, not single generic
# words) to avoid noisy, low-confidence guesses.
SECTOR_KEYWORDS: Dict[str, List[str]] = {
    "Technology": [
        "technology", "software", "artificial intelligence",
        "semiconductor", "cloud computing", "tehnologie",
    ],
    "Energy": [
        "oil prices", "crude oil", "natural gas", "energy prices",
        "petrol", "energie electrica", "gaze naturale",
    ],
    "Financial Services": [
        "interest rate", "central bank", "mortgage rate",
        "banking sector", "dobanda cheie", "sistem bancar",
    ],
    "Healthcare": [
        "hospital", "pharmaceutical", "healthcare system",
        "spital", "sanatate publica",
    ],
    "Automotive": [
        "automaker", "electric vehicle", "car manufacturer",
        "masini electrice",
    ],
    "Telecommunications": [
        "telecom operator", "mobile network", "broadband",
        "telecomunicatii",
    ],
    "Cryptocurrency": [
        "cryptocurrency", "blockchain", "digital asset", "criptomoneda",
    ],
    "Utilities": [
        "power grid", "water utility", "electricity grid",
        "retea electrica",
    ],
    "Media & Entertainment": [
        "streaming service", "box office", "film studio",
    ],
    "Retail & E-commerce": [
        "online retailer", "e-commerce", "retail sales",
    ],
}
