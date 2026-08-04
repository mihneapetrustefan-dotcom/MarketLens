"""
company_registry.py
----------------------
Curated registry of companies MarketLens can recognize by name.

Each entry:
- canonical_name: the standardized name used everywhere downstream
  (this is what gets reported, regardless of which alias matched)
- aliases: every way the company might realistically be written in a
  headline (must include the canonical_name itself, plus common
  short forms/abbreviations)
- ticker: exchange ticker symbol (BVB, NYSE/NASDAQ, or crypto symbol) —
  stored here for convenience; the future Ticker Detector module will
  have its own, separate logic for spotting tickers directly in text
  (e.g. "$AAPL" cashtags), this field just links the two together
- category: "bvb" / "stocks" / "crypto" — mirrors the categories
  already used by the News Collector, so downstream modules can filter
  consistently across the whole pipeline

NOTE: Expanded in v1.1 to cover many more sectors and markets, following
the same rules as the original starting set:
- Short/ambiguous aliases are deliberately AVOIDED where they'd risk
  false positives (e.g. no "GE" alias for General Electric — the full
  name is used instead, since "GE" collides too easily with unrelated
  text). Company Detector's existing case-sensitivity rule for aliases
  of length <=4 (see company_detector.py) still applies automatically
  to any short alias added here.
- Single-character tickers (e.g. "T" for AT&T, "V" for Visa) are safe
  by construction: Ticker Detector already excludes 1-character tickers
  from BARE matching (see _MIN_BARE_TICKER_LENGTH in ticker_detector.py)
  — they're still matched safely via cashtag ("$T", "$V").
- KNOWN REMAINING AMBIGUITY: "Visa" (the company alias) can still
  collide with "visa" the travel document when capitalized at the
  start of a sentence. Flagged here rather than hidden; worth revisiting
  if it produces false positives in practice.
- Romanian (BVB) tickers beyond the original 12 are added with
  reasonable confidence but have NOT been cross-checked against a live
  BVB listing — verify against bvb.ro before using for anything
  beyond internal testing/demo purposes.
- KNOWN REMAINING AMBIGUITY (v1.2): ServiceNow's ticker "NOW" is a very
  common English word/CTA ("buy now", "act now") that commonly appears
  capitalized in real headlines. Ticker Detector's bare-match
  safeguards (case-sensitivity, word boundaries) reduce but do not
  eliminate this risk — flagged here rather than hidden; worth
  revisiting (e.g. requiring a "$NOW" cashtag only) if it produces
  false positives in practice.
- KNOWN REMAINING AMBIGUITY (v1.2): "BVB" (the alias/ticker for Bursa
  de Valori Bucuresti itself) is also the standard media abbreviation
  for the football club Borussia Dortmund. Sports coverage using "BVB"
  will likely be misattributed to the stock exchange. Flagged for the
  same reason as the others above — not hidden, worth revisiting if it
  causes false positives (e.g. adding a sector/keyword exclusion for
  sports contexts).
- KNOWN REMAINING AMBIGUITY (pre-existing, confirmed while testing
  v1.2 additions): "Oracle" (the company alias, 6 characters, matched
  case-INSENSITIVELY per Company Detector's length-based rule) also
  collides with "oracle" as an ordinary lowercase technical term in
  crypto/blockchain articles (a "data oracle" feeding smart contracts,
  unrelated to Oracle Corporation) — e.g. "Chainlink partners on oracle
  infrastructure" incorrectly also detects Oracle Corp. Not fixed here
  since it's a broader trade-off (case-insensitive matching helps
  recall for genuine mentions of the company); flagged for future
  revisiting if it produces too many false positives in practice.
"""

from typing import List, Dict, Any

COMPANY_REGISTRY: List[Dict[str, Any]] = [
    # --- BVB (Romanian market) ---
    {"canonical_name": "Banca Transilvania", "aliases": ["Banca Transilvania", "BT"], "ticker": "TLV", "category": "bvb"},
    {"canonical_name": "Hidroelectrica", "aliases": ["Hidroelectrica"], "ticker": "H2O", "category": "bvb"},
    {"canonical_name": "OMV Petrom", "aliases": ["OMV Petrom", "Petrom"], "ticker": "SNP", "category": "bvb"},
    {"canonical_name": "Nuclearelectrica", "aliases": ["Nuclearelectrica"], "ticker": "SNN", "category": "bvb"},
    {"canonical_name": "Romgaz", "aliases": ["Romgaz"], "ticker": "SNG", "category": "bvb"},
    {"canonical_name": "Transgaz", "aliases": ["Transgaz"], "ticker": "TGN", "category": "bvb"},
    {"canonical_name": "Electrica", "aliases": ["Electrica"], "ticker": "EL", "category": "bvb"},
    {"canonical_name": "BRD - Groupe Societe Generale", "aliases": ["BRD"], "ticker": "BRD", "category": "bvb"},
    {"canonical_name": "Digi Communications", "aliases": ["Digi Communications", "Digi"], "ticker": "DIGI", "category": "bvb"},
    {"canonical_name": "Fondul Proprietatea", "aliases": ["Fondul Proprietatea"], "ticker": "FP", "category": "bvb"},
    {"canonical_name": "Purcari Wineries", "aliases": ["Purcari"], "ticker": "WINE", "category": "bvb"},
    {"canonical_name": "MedLife", "aliases": ["MedLife"], "ticker": "M", "category": "bvb"},
    {"canonical_name": "Sphera Franchise Group", "aliases": ["Sphera Franchise Group", "Sphera"], "ticker": "SFG", "category": "bvb"},
    {"canonical_name": "TeraPlast", "aliases": ["TeraPlast"], "ticker": "TRP", "category": "bvb"},
    {"canonical_name": "One United Properties", "aliases": ["One United Properties"], "ticker": "ONE", "category": "bvb"},
    {"canonical_name": "Transelectrica", "aliases": ["Transelectrica"], "ticker": "TEL", "category": "bvb"},
    {"canonical_name": "Antibiotice", "aliases": ["Antibiotice"], "ticker": "ATB", "category": "bvb"},
    {"canonical_name": "Aquila", "aliases": ["Aquila"], "ticker": "AQ", "category": "bvb"},
    {"canonical_name": "Bursa de Valori Bucuresti", "aliases": ["Bursa de Valori Bucuresti", "BVB"], "ticker": "BVB", "category": "bvb"},
    {"canonical_name": "Conpet", "aliases": ["Conpet"], "ticker": "COTE", "category": "bvb"},
    {"canonical_name": "Alro", "aliases": ["Alro"], "ticker": "ALR", "category": "bvb"},
    {"canonical_name": "Vrancart", "aliases": ["Vrancart"], "ticker": "VNC", "category": "bvb"},
    {"canonical_name": "Bittnet Systems", "aliases": ["Bittnet Systems", "Bittnet"], "ticker": "BNET", "category": "bvb"},
    {"canonical_name": "Patria Bank", "aliases": ["Patria Bank"], "ticker": "PBK", "category": "bvb"},

    # --- International: Technology ---
    {"canonical_name": "Apple", "aliases": ["Apple", "Apple Inc", "Apple Inc."], "ticker": "AAPL", "category": "stocks"},
    {"canonical_name": "Microsoft", "aliases": ["Microsoft"], "ticker": "MSFT", "category": "stocks"},
    {"canonical_name": "Amazon", "aliases": ["Amazon"], "ticker": "AMZN", "category": "stocks"},
    {"canonical_name": "Alphabet", "aliases": ["Alphabet", "Google"], "ticker": "GOOGL", "category": "stocks"},
    {"canonical_name": "Meta Platforms", "aliases": ["Meta", "Facebook"], "ticker": "META", "category": "stocks"},
    {"canonical_name": "Nvidia", "aliases": ["Nvidia"], "ticker": "NVDA", "category": "stocks"},
    {"canonical_name": "Intel", "aliases": ["Intel"], "ticker": "INTC", "category": "stocks"},
    {"canonical_name": "AMD", "aliases": ["AMD", "Advanced Micro Devices"], "ticker": "AMD", "category": "stocks"},
    {"canonical_name": "Oracle", "aliases": ["Oracle"], "ticker": "ORCL", "category": "stocks"},
    {"canonical_name": "Salesforce", "aliases": ["Salesforce"], "ticker": "CRM", "category": "stocks"},
    {"canonical_name": "Adobe", "aliases": ["Adobe"], "ticker": "ADBE", "category": "stocks"},
    {"canonical_name": "IBM", "aliases": ["IBM"], "ticker": "IBM", "category": "stocks"},
    {"canonical_name": "Qualcomm", "aliases": ["Qualcomm"], "ticker": "QCOM", "category": "stocks"},
    {"canonical_name": "Cisco", "aliases": ["Cisco"], "ticker": "CSCO", "category": "stocks"},

    # --- International: Automotive ---
    {"canonical_name": "Tesla", "aliases": ["Tesla"], "ticker": "TSLA", "category": "stocks"},
    {"canonical_name": "Ford Motor Company", "aliases": ["Ford"], "ticker": "F", "category": "stocks"},
    {"canonical_name": "General Motors", "aliases": ["General Motors"], "ticker": "GM", "category": "stocks"},
    {"canonical_name": "Toyota", "aliases": ["Toyota"], "ticker": "TM", "category": "stocks"},

    # --- International: Healthcare ---
    {"canonical_name": "Johnson & Johnson", "aliases": ["Johnson & Johnson"], "ticker": "JNJ", "category": "stocks"},
    {"canonical_name": "Pfizer", "aliases": ["Pfizer"], "ticker": "PFE", "category": "stocks"},
    {"canonical_name": "UnitedHealth Group", "aliases": ["UnitedHealth"], "ticker": "UNH", "category": "stocks"},
    {"canonical_name": "Moderna", "aliases": ["Moderna"], "ticker": "MRNA", "category": "stocks"},
    {"canonical_name": "Eli Lilly", "aliases": ["Eli Lilly"], "ticker": "LLY", "category": "stocks"},
    {"canonical_name": "Abbott Laboratories", "aliases": ["Abbott"], "ticker": "ABT", "category": "stocks"},
    {"canonical_name": "Merck & Co", "aliases": ["Merck"], "ticker": "MRK", "category": "stocks"},
    {"canonical_name": "Bristol Myers Squibb", "aliases": ["Bristol Myers Squibb", "Bristol-Myers Squibb"], "ticker": "BMY", "category": "stocks"},
    {"canonical_name": "CVS Health", "aliases": ["CVS Health"], "ticker": "CVS", "category": "stocks"},

    # --- International: Financial Services ---
    {"canonical_name": "JPMorgan Chase", "aliases": ["JPMorgan", "JP Morgan"], "ticker": "JPM", "category": "stocks"},
    {"canonical_name": "Goldman Sachs", "aliases": ["Goldman Sachs"], "ticker": "GS", "category": "stocks"},
    {"canonical_name": "Bank of America", "aliases": ["Bank of America"], "ticker": "BAC", "category": "stocks"},
    {"canonical_name": "Wells Fargo", "aliases": ["Wells Fargo"], "ticker": "WFC", "category": "stocks"},
    {"canonical_name": "Visa", "aliases": ["Visa"], "ticker": "V", "category": "stocks"},
    {"canonical_name": "Mastercard", "aliases": ["Mastercard"], "ticker": "MA", "category": "stocks"},
    {"canonical_name": "Morgan Stanley", "aliases": ["Morgan Stanley"], "ticker": "MS", "category": "stocks"},
    {"canonical_name": "Charles Schwab", "aliases": ["Charles Schwab"], "ticker": "SCHW", "category": "stocks"},
    {"canonical_name": "American Express", "aliases": ["American Express"], "ticker": "AXP", "category": "stocks"},
    {"canonical_name": "BlackRock", "aliases": ["BlackRock"], "ticker": "BLK", "category": "stocks"},
    {"canonical_name": "PayPal", "aliases": ["PayPal"], "ticker": "PYPL", "category": "stocks"},

    # --- International: Consumer Goods & Retail ---
    {"canonical_name": "Walmart", "aliases": ["Walmart"], "ticker": "WMT", "category": "stocks"},
    {"canonical_name": "Costco", "aliases": ["Costco"], "ticker": "COST", "category": "stocks"},
    {"canonical_name": "Procter & Gamble", "aliases": ["Procter & Gamble"], "ticker": "PG", "category": "stocks"},
    {"canonical_name": "Coca-Cola", "aliases": ["Coca-Cola", "Coca Cola"], "ticker": "KO", "category": "stocks"},
    {"canonical_name": "PepsiCo", "aliases": ["PepsiCo"], "ticker": "PEP", "category": "stocks"},
    {"canonical_name": "Nike", "aliases": ["Nike"], "ticker": "NKE", "category": "stocks"},
    {"canonical_name": "McDonald's", "aliases": ["McDonald's", "McDonalds"], "ticker": "MCD", "category": "stocks"},
    {"canonical_name": "Starbucks", "aliases": ["Starbucks"], "ticker": "SBUX", "category": "stocks"},
    {"canonical_name": "Target", "aliases": ["Target"], "ticker": "TGT", "category": "stocks"},
    {"canonical_name": "Home Depot", "aliases": ["Home Depot"], "ticker": "HD", "category": "stocks"},
    {"canonical_name": "Lowe's", "aliases": ["Lowe's", "Lowes"], "ticker": "LOW", "category": "stocks"},
    {"canonical_name": "Colgate-Palmolive", "aliases": ["Colgate-Palmolive", "Colgate"], "ticker": "CL", "category": "stocks"},

    # --- International: Energy ---
    {"canonical_name": "ExxonMobil", "aliases": ["ExxonMobil", "Exxon Mobil", "Exxon"], "ticker": "XOM", "category": "stocks"},
    {"canonical_name": "Chevron", "aliases": ["Chevron"], "ticker": "CVX", "category": "stocks"},
    {"canonical_name": "Shell", "aliases": ["Shell"], "ticker": "SHEL", "category": "stocks"},
    {"canonical_name": "ConocoPhillips", "aliases": ["ConocoPhillips"], "ticker": "COP", "category": "stocks"},
    {"canonical_name": "Occidental Petroleum", "aliases": ["Occidental Petroleum", "Occidental"], "ticker": "OXY", "category": "stocks"},

    # --- International: Media & Entertainment ---
    {"canonical_name": "Netflix", "aliases": ["Netflix"], "ticker": "NFLX", "category": "stocks"},
    {"canonical_name": "Walt Disney", "aliases": ["Disney"], "ticker": "DIS", "category": "stocks"},
    {"canonical_name": "Warner Bros Discovery", "aliases": ["Warner Bros Discovery", "Warner Bros"], "ticker": "WBD", "category": "stocks"},
    {"canonical_name": "Comcast", "aliases": ["Comcast"], "ticker": "CMCSA", "category": "stocks"},
    {"canonical_name": "Spotify", "aliases": ["Spotify"], "ticker": "SPOT", "category": "stocks"},

    # --- International: Industrials ---
    {"canonical_name": "Boeing", "aliases": ["Boeing"], "ticker": "BA", "category": "stocks"},
    {"canonical_name": "Caterpillar", "aliases": ["Caterpillar"], "ticker": "CAT", "category": "stocks"},
    {"canonical_name": "General Electric", "aliases": ["General Electric"], "ticker": "GE", "category": "stocks"},
    {"canonical_name": "Honeywell", "aliases": ["Honeywell"], "ticker": "HON", "category": "stocks"},
    {"canonical_name": "3M", "aliases": ["3M"], "ticker": "MMM", "category": "stocks"},
    {"canonical_name": "Lockheed Martin", "aliases": ["Lockheed Martin"], "ticker": "LMT", "category": "stocks"},
    {"canonical_name": "RTX Corporation", "aliases": ["Raytheon", "RTX Corporation"], "ticker": "RTX", "category": "stocks"},

    # --- International: Airlines ---
    {"canonical_name": "Delta Air Lines", "aliases": ["Delta Air Lines", "Delta Airlines"], "ticker": "DAL", "category": "stocks"},
    {"canonical_name": "United Airlines", "aliases": ["United Airlines"], "ticker": "UAL", "category": "stocks"},
    {"canonical_name": "Southwest Airlines", "aliases": ["Southwest Airlines"], "ticker": "LUV", "category": "stocks"},
    {"canonical_name": "American Airlines", "aliases": ["American Airlines"], "ticker": "AAL", "category": "stocks"},

    # --- International: Telecommunications ---
    {"canonical_name": "AT&T", "aliases": ["AT&T"], "ticker": "T", "category": "stocks"},
    {"canonical_name": "Verizon", "aliases": ["Verizon"], "ticker": "VZ", "category": "stocks"},
    {"canonical_name": "T-Mobile US", "aliases": ["T-Mobile"], "ticker": "TMUS", "category": "stocks"},

    # --- International: Semiconductors ---
    {"canonical_name": "Broadcom", "aliases": ["Broadcom"], "ticker": "AVGO", "category": "stocks"},
    {"canonical_name": "Texas Instruments", "aliases": ["Texas Instruments"], "ticker": "TXN", "category": "stocks"},
    {"canonical_name": "Micron Technology", "aliases": ["Micron Technology", "Micron"], "ticker": "MU", "category": "stocks"},
    {"canonical_name": "ASML Holding", "aliases": ["ASML"], "ticker": "ASML", "category": "stocks"},

    # --- International: Technology (additional) ---
    {"canonical_name": "Uber Technologies", "aliases": ["Uber"], "ticker": "UBER", "category": "stocks"},
    {"canonical_name": "ServiceNow", "aliases": ["ServiceNow"], "ticker": "NOW", "category": "stocks"},
    {"canonical_name": "Palantir Technologies", "aliases": ["Palantir"], "ticker": "PLTR", "category": "stocks"},
    {"canonical_name": "Shopify", "aliases": ["Shopify"], "ticker": "SHOP", "category": "stocks"},
    {"canonical_name": "Zoom Communications", "aliases": ["Zoom"], "ticker": "ZM", "category": "stocks"},
    {"canonical_name": "Snowflake", "aliases": ["Snowflake Inc"], "ticker": "SNOW", "category": "stocks"},

    # --- International: Automotive (additional) ---
    {"canonical_name": "Rivian Automotive", "aliases": ["Rivian"], "ticker": "RIVN", "category": "stocks"},
    {"canonical_name": "Lucid Group", "aliases": ["Lucid Motors", "Lucid Group"], "ticker": "LCID", "category": "stocks"},

    # --- Crypto ---
    {"canonical_name": "Bitcoin", "aliases": ["Bitcoin", "BTC"], "ticker": "BTC", "category": "crypto"},
    {"canonical_name": "Ethereum", "aliases": ["Ethereum", "ETH"], "ticker": "ETH", "category": "crypto"},
    {"canonical_name": "Binance", "aliases": ["Binance"], "ticker": "BNB", "category": "crypto"},
    {"canonical_name": "Coinbase", "aliases": ["Coinbase"], "ticker": "COIN", "category": "crypto"},
    {"canonical_name": "Ripple", "aliases": ["Ripple", "XRP"], "ticker": "XRP", "category": "crypto"},
    {"canonical_name": "Solana", "aliases": ["Solana"], "ticker": "SOL", "category": "crypto"},
    {"canonical_name": "Cardano", "aliases": ["Cardano"], "ticker": "ADA", "category": "crypto"},
    {"canonical_name": "Dogecoin", "aliases": ["Dogecoin"], "ticker": "DOGE", "category": "crypto"},
    {"canonical_name": "Polkadot", "aliases": ["Polkadot"], "ticker": "DOT", "category": "crypto"},
    {"canonical_name": "Litecoin", "aliases": ["Litecoin"], "ticker": "LTC", "category": "crypto"},
    {"canonical_name": "Avalanche", "aliases": ["Avalanche"], "ticker": "AVAX", "category": "crypto"},
    {"canonical_name": "Chainlink", "aliases": ["Chainlink"], "ticker": "LINK", "category": "crypto"},
    {"canonical_name": "Polygon", "aliases": ["Polygon"], "ticker": "MATIC", "category": "crypto"},
]
