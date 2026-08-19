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

CHANGE LOG (v1.3) — expanded from 123 to 388+ companies, across many
new sectors (Real Estate, Materials, expanded Semiconductors,
Biotech/Healthcare, Financial Services, Industrials, Automotive,
Crypto). Every new alias was checked against the FULL existing list
above before being added, to avoid silent collisions. Several new
entries carry their OWN "known remaining ambiguity" note, following
the exact same policy as the pre-existing ones above (flag, don't
hide, don't silently avoid adding a real company just because its
name is a common word — same trade-off already accepted for Oracle,
Visa, NOW). Newly flagged:
- "Dow Inc" — alias deliberately kept as the 2-word "Dow Inc" (NOT
  bare "Dow"), since bare "Dow" would constantly collide with "Dow
  Jones" index headlines.
- "Vale" (4-char alias, case-sensitive per existing length rule) can
  still collide with capitalized sentence-starting uses of the
  ordinary word "Vale" (as in valley) in non-financial text.
- "Waste Management", "American Tower", "Equity Residential", "State
  Street Corporation", "Southern Company", "Public Storage" — each is
  a real company name built from ordinary English words/phrases,
  carrying the same generic-phrase collision risk already accepted
  for "Discover" (deliberately NOT added as a bare alias — see
  Financial Services section) and similar cases.
- "Applied Materials", "Analog Devices" — sector-generic phrases that
  could appear in unrelated scientific/technical writing.
- Not added as BARE aliases (too generic/risky, full multi-word name
  used instead, or omitted entirely): "Discover" (Discover Financial
  Services), "Progressive" (Progressive Corporation), "Travelers"
  (Travelers Companies), "Nasdaq" (Nasdaq Inc — bare "Nasdaq" is used
  constantly as a generic exchange reference), "ICE" (Intercontinental
  Exchange — far too ambiguous), "Maker" (MakerDAO/MKR — extremely
  common word, alias restricted to "MakerDAO" only).
- Romanian (BVB) additions beyond the original set follow the SAME
  "not cross-checked against a live listing" caveat already stated
  above — verify against bvb.ro before relying on them beyond
  internal testing.
- KNOWN REMAINING AMBIGUITY (v1.3): the ticker "EL" is used by BOTH
  "Electrica" (BVB) and the newly-added "Estee Lauder" (NYSE) — a
  genuine real-world coincidence, not a data-entry error; both are
  each company's real, correct ticker on their own exchange. This
  registry has no per-exchange namespacing, so any downstream code
  that looks a company up BY TICKER ALONE (rather than by category +
  ticker together) could ambiguously match either one. Flagged here
  rather than silently dropping either real company.
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
    # --- BVB additions (v1.3) ---
    {"canonical_name": "SIF Banat-Crisana", "aliases": ["SIF Banat-Crisana"], "ticker": "SIF1", "category": "bvb"},
    {"canonical_name": "SIF Moldova", "aliases": ["SIF Moldova"], "ticker": "SIF2", "category": "bvb"},
    {"canonical_name": "SIF Muntenia", "aliases": ["SIF Muntenia"], "ticker": "SIF4", "category": "bvb"},
    {"canonical_name": "SIF Oltenia", "aliases": ["SIF Oltenia"], "ticker": "SIF5", "category": "bvb"},
    {"canonical_name": "SIF Transilvania", "aliases": ["SIF Transilvania"], "ticker": "SIF3", "category": "bvb"},
    {"canonical_name": "Aerostar", "aliases": ["Aerostar"], "ticker": "ARS", "category": "bvb"},
    {"canonical_name": "Romcarbon", "aliases": ["Romcarbon"], "ticker": "ROCE", "category": "bvb"},
    {"canonical_name": "IAR SA", "aliases": ["IAR SA", "IAR Ghimbav"], "ticker": "IARV", "category": "bvb"},

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
    # --- Technology additions (v1.3) ---
    {"canonical_name": "SAP", "aliases": ["SAP"], "ticker": "SAP", "category": "stocks"},
    {"canonical_name": "Intuit", "aliases": ["Intuit"], "ticker": "INTU", "category": "stocks"},
    {"canonical_name": "Autodesk", "aliases": ["Autodesk"], "ticker": "ADSK", "category": "stocks"},
    {"canonical_name": "Synopsys", "aliases": ["Synopsys"], "ticker": "SNPS", "category": "stocks"},
    {"canonical_name": "Cadence Design Systems", "aliases": ["Cadence Design Systems", "Cadence"], "ticker": "CDNS", "category": "stocks"},
    {"canonical_name": "Palo Alto Networks", "aliases": ["Palo Alto Networks"], "ticker": "PANW", "category": "stocks"},
    {"canonical_name": "CrowdStrike", "aliases": ["CrowdStrike"], "ticker": "CRWD", "category": "stocks"},
    {"canonical_name": "Fortinet", "aliases": ["Fortinet"], "ticker": "FTNT", "category": "stocks"},
    {"canonical_name": "Datadog", "aliases": ["Datadog"], "ticker": "DDOG", "category": "stocks"},
    {"canonical_name": "MongoDB", "aliases": ["MongoDB"], "ticker": "MDB", "category": "stocks"},
    {"canonical_name": "Atlassian", "aliases": ["Atlassian"], "ticker": "TEAM", "category": "stocks"},
    {"canonical_name": "HubSpot", "aliases": ["HubSpot"], "ticker": "HUBS", "category": "stocks"},
    {"canonical_name": "DocuSign", "aliases": ["DocuSign"], "ticker": "DOCU", "category": "stocks"},
    {"canonical_name": "Dropbox", "aliases": ["Dropbox"], "ticker": "DBX", "category": "stocks"},
    {"canonical_name": "Twilio", "aliases": ["Twilio"], "ticker": "TWLO", "category": "stocks"},
    {"canonical_name": "Okta", "aliases": ["Okta"], "ticker": "OKTA", "category": "stocks"},
    {"canonical_name": "Akamai Technologies", "aliases": ["Akamai"], "ticker": "AKAM", "category": "stocks"},
    {"canonical_name": "Dell Technologies", "aliases": ["Dell Technologies", "Dell"], "ticker": "DELL", "category": "stocks"},
    {"canonical_name": "HP Inc", "aliases": ["HP Inc", "HP Inc."], "ticker": "HPQ", "category": "stocks"},
    {"canonical_name": "Hewlett Packard Enterprise", "aliases": ["Hewlett Packard Enterprise", "HPE"], "ticker": "HPE", "category": "stocks"},
    {"canonical_name": "Western Digital", "aliases": ["Western Digital"], "ticker": "WDC", "category": "stocks"},
    {"canonical_name": "Seagate Technology", "aliases": ["Seagate Technology", "Seagate"], "ticker": "STX", "category": "stocks"},
    {"canonical_name": "Corning", "aliases": ["Corning"], "ticker": "GLW", "category": "stocks"},
    {"canonical_name": "Motorola Solutions", "aliases": ["Motorola Solutions"], "ticker": "MSI", "category": "stocks"},
    {"canonical_name": "Garmin", "aliases": ["Garmin"], "ticker": "GRMN", "category": "stocks"},
    {"canonical_name": "Logitech", "aliases": ["Logitech"], "ticker": "LOGI", "category": "stocks"},
    {"canonical_name": "NetApp", "aliases": ["NetApp"], "ticker": "NTAP", "category": "stocks"},
    {"canonical_name": "Juniper Networks", "aliases": ["Juniper Networks"], "ticker": "JNPR", "category": "stocks"},
    {"canonical_name": "Arista Networks", "aliases": ["Arista Networks"], "ticker": "ANET", "category": "stocks"},
    {"canonical_name": "F5 Inc", "aliases": ["F5 Inc", "F5 Networks"], "ticker": "FFIV", "category": "stocks"},
    {"canonical_name": "Trimble", "aliases": ["Trimble"], "ticker": "TRMB", "category": "stocks"},
    {"canonical_name": "Workday", "aliases": ["Workday"], "ticker": "WDAY", "category": "stocks"},
    {"canonical_name": "ANSYS", "aliases": ["ANSYS"], "ticker": "ANSS", "category": "stocks"},
    {"canonical_name": "PTC Inc", "aliases": ["PTC Inc", "PTC Inc."], "ticker": "PTC", "category": "stocks"},
    {"canonical_name": "Check Point Software Technologies", "aliases": ["Check Point Software", "Check Point"], "ticker": "CHKP", "category": "stocks"},
    {"canonical_name": "Zscaler", "aliases": ["Zscaler"], "ticker": "ZS", "category": "stocks"},

    # --- International: Semiconductors ---
    {"canonical_name": "Broadcom", "aliases": ["Broadcom"], "ticker": "AVGO", "category": "stocks"},
    {"canonical_name": "Texas Instruments", "aliases": ["Texas Instruments"], "ticker": "TXN", "category": "stocks"},
    {"canonical_name": "Micron Technology", "aliases": ["Micron Technology", "Micron"], "ticker": "MU", "category": "stocks"},
    {"canonical_name": "ASML Holding", "aliases": ["ASML"], "ticker": "ASML", "category": "stocks"},
    # --- Semiconductor additions (v1.3) ---
    {"canonical_name": "Applied Materials", "aliases": ["Applied Materials"], "ticker": "AMAT", "category": "stocks"},
    {"canonical_name": "Lam Research", "aliases": ["Lam Research"], "ticker": "LRCX", "category": "stocks"},
    {"canonical_name": "KLA Corporation", "aliases": ["KLA Corporation", "KLA"], "ticker": "KLAC", "category": "stocks"},
    {"canonical_name": "Analog Devices", "aliases": ["Analog Devices"], "ticker": "ADI", "category": "stocks"},
    {"canonical_name": "NXP Semiconductors", "aliases": ["NXP Semiconductors", "NXP"], "ticker": "NXPI", "category": "stocks"},
    {"canonical_name": "Marvell Technology", "aliases": ["Marvell Technology", "Marvell"], "ticker": "MRVL", "category": "stocks"},
    {"canonical_name": "ON Semiconductor", "aliases": ["ON Semiconductor"], "ticker": "ON", "category": "stocks"},
    {"canonical_name": "Skyworks Solutions", "aliases": ["Skyworks Solutions", "Skyworks"], "ticker": "SWKS", "category": "stocks"},
    {"canonical_name": "Qorvo", "aliases": ["Qorvo"], "ticker": "QRVO", "category": "stocks"},
    {"canonical_name": "Microchip Technology", "aliases": ["Microchip Technology"], "ticker": "MCHP", "category": "stocks"},
    {"canonical_name": "Teradyne", "aliases": ["Teradyne"], "ticker": "TER", "category": "stocks"},
    {"canonical_name": "Entegris", "aliases": ["Entegris"], "ticker": "ENTG", "category": "stocks"},
    {"canonical_name": "MKS Instruments", "aliases": ["MKS Instruments"], "ticker": "MKSI", "category": "stocks"},
    {"canonical_name": "GlobalFoundries", "aliases": ["GlobalFoundries"], "ticker": "GFS", "category": "stocks"},

    # --- International: Automotive ---
    {"canonical_name": "Tesla", "aliases": ["Tesla"], "ticker": "TSLA", "category": "stocks"},
    {"canonical_name": "Ford Motor Company", "aliases": ["Ford"], "ticker": "F", "category": "stocks"},
    {"canonical_name": "General Motors", "aliases": ["General Motors"], "ticker": "GM", "category": "stocks"},
    {"canonical_name": "Toyota", "aliases": ["Toyota"], "ticker": "TM", "category": "stocks"},
    {"canonical_name": "Rivian Automotive", "aliases": ["Rivian"], "ticker": "RIVN", "category": "stocks"},
    {"canonical_name": "Lucid Group", "aliases": ["Lucid Motors", "Lucid Group"], "ticker": "LCID", "category": "stocks"},
    # --- Automotive additions (v1.3) ---
    {"canonical_name": "Honda Motor", "aliases": ["Honda Motor", "Honda"], "ticker": "HMC", "category": "stocks"},
    {"canonical_name": "Volkswagen", "aliases": ["Volkswagen"], "ticker": "VWAGY", "category": "stocks"},
    {"canonical_name": "BMW", "aliases": ["BMW"], "ticker": "BMWYY", "category": "stocks"},
    {"canonical_name": "Mercedes-Benz Group", "aliases": ["Mercedes-Benz Group", "Mercedes-Benz"], "ticker": "MBGYY", "category": "stocks"},
    {"canonical_name": "Stellantis", "aliases": ["Stellantis"], "ticker": "STLA", "category": "stocks"},
    {"canonical_name": "Ferrari", "aliases": ["Ferrari"], "ticker": "RACE", "category": "stocks"},
    {"canonical_name": "Porsche", "aliases": ["Porsche"], "ticker": "POAHY", "category": "stocks"},
    {"canonical_name": "Nio", "aliases": ["Nio"], "ticker": "NIO", "category": "stocks"},
    {"canonical_name": "XPeng", "aliases": ["XPeng"], "ticker": "XPEV", "category": "stocks"},
    {"canonical_name": "BYD Company", "aliases": ["BYD Company", "BYD"], "ticker": "BYDDY", "category": "stocks"},
    {"canonical_name": "Harley-Davidson", "aliases": ["Harley-Davidson"], "ticker": "HOG", "category": "stocks"},
    {"canonical_name": "Aptiv", "aliases": ["Aptiv"], "ticker": "APTV", "category": "stocks"},
    {"canonical_name": "BorgWarner", "aliases": ["BorgWarner"], "ticker": "BWA", "category": "stocks"},

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
    # --- Healthcare / Biotech additions (v1.3) ---
    {"canonical_name": "Amgen", "aliases": ["Amgen"], "ticker": "AMGN", "category": "stocks"},
    {"canonical_name": "Gilead Sciences", "aliases": ["Gilead Sciences", "Gilead"], "ticker": "GILD", "category": "stocks"},
    {"canonical_name": "Biogen", "aliases": ["Biogen"], "ticker": "BIIB", "category": "stocks"},
    {"canonical_name": "Regeneron Pharmaceuticals", "aliases": ["Regeneron Pharmaceuticals", "Regeneron"], "ticker": "REGN", "category": "stocks"},
    {"canonical_name": "Vertex Pharmaceuticals", "aliases": ["Vertex Pharmaceuticals"], "ticker": "VRTX", "category": "stocks"},
    {"canonical_name": "Zoetis", "aliases": ["Zoetis"], "ticker": "ZTS", "category": "stocks"},
    {"canonical_name": "Stryker", "aliases": ["Stryker"], "ticker": "SYK", "category": "stocks"},
    {"canonical_name": "Medtronic", "aliases": ["Medtronic"], "ticker": "MDT", "category": "stocks"},
    {"canonical_name": "Boston Scientific", "aliases": ["Boston Scientific"], "ticker": "BSX", "category": "stocks"},
    {"canonical_name": "Becton Dickinson", "aliases": ["Becton Dickinson"], "ticker": "BDX", "category": "stocks"},
    {"canonical_name": "Danaher", "aliases": ["Danaher"], "ticker": "DHR", "category": "stocks"},
    {"canonical_name": "Thermo Fisher Scientific", "aliases": ["Thermo Fisher Scientific", "Thermo Fisher"], "ticker": "TMO", "category": "stocks"},
    {"canonical_name": "IQVIA", "aliases": ["IQVIA"], "ticker": "IQV", "category": "stocks"},
    {"canonical_name": "Illumina", "aliases": ["Illumina"], "ticker": "ILMN", "category": "stocks"},
    {"canonical_name": "Humana", "aliases": ["Humana"], "ticker": "HUM", "category": "stocks"},
    {"canonical_name": "Cigna", "aliases": ["Cigna"], "ticker": "CI", "category": "stocks"},
    {"canonical_name": "Elevance Health", "aliases": ["Elevance Health"], "ticker": "ELV", "category": "stocks"},
    {"canonical_name": "Centene Corporation", "aliases": ["Centene Corporation", "Centene"], "ticker": "CNC", "category": "stocks"},
    {"canonical_name": "HCA Healthcare", "aliases": ["HCA Healthcare"], "ticker": "HCA", "category": "stocks"},
    {"canonical_name": "DaVita", "aliases": ["DaVita"], "ticker": "DVA", "category": "stocks"},
    {"canonical_name": "Baxter International", "aliases": ["Baxter International", "Baxter"], "ticker": "BAX", "category": "stocks"},
    {"canonical_name": "Cardinal Health", "aliases": ["Cardinal Health"], "ticker": "CAH", "category": "stocks"},
    {"canonical_name": "McKesson", "aliases": ["McKesson"], "ticker": "MCK", "category": "stocks"},
    {"canonical_name": "Cencora", "aliases": ["Cencora"], "ticker": "COR", "category": "stocks"},
    {"canonical_name": "GSK", "aliases": ["GSK"], "ticker": "GSK", "category": "stocks"},
    {"canonical_name": "AstraZeneca", "aliases": ["AstraZeneca"], "ticker": "AZN", "category": "stocks"},
    {"canonical_name": "Novartis", "aliases": ["Novartis"], "ticker": "NVS", "category": "stocks"},
    {"canonical_name": "Roche", "aliases": ["Roche"], "ticker": "RHHBY", "category": "stocks"},
    {"canonical_name": "Sanofi", "aliases": ["Sanofi"], "ticker": "SNY", "category": "stocks"},
    {"canonical_name": "Novo Nordisk", "aliases": ["Novo Nordisk"], "ticker": "NVO", "category": "stocks"},

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
    # --- Financial Services additions (v1.3) ---
    {"canonical_name": "Citigroup", "aliases": ["Citigroup", "Citi"], "ticker": "C", "category": "stocks"},
    {"canonical_name": "US Bancorp", "aliases": ["US Bancorp"], "ticker": "USB", "category": "stocks"},
    {"canonical_name": "PNC Financial Services", "aliases": ["PNC Financial Services", "PNC"], "ticker": "PNC", "category": "stocks"},
    {"canonical_name": "Truist Financial", "aliases": ["Truist Financial", "Truist"], "ticker": "TFC", "category": "stocks"},
    {"canonical_name": "Capital One Financial", "aliases": ["Capital One"], "ticker": "COF", "category": "stocks"},
    {"canonical_name": "Fifth Third Bancorp", "aliases": ["Fifth Third Bancorp", "Fifth Third"], "ticker": "FITB", "category": "stocks"},
    {"canonical_name": "State Street Corporation", "aliases": ["State Street Corporation"], "ticker": "STT", "category": "stocks"},
    {"canonical_name": "Bank of New York Mellon", "aliases": ["Bank of New York Mellon", "BNY Mellon"], "ticker": "BK", "category": "stocks"},
    {"canonical_name": "Ally Financial", "aliases": ["Ally Financial"], "ticker": "ALLY", "category": "stocks"},
    {"canonical_name": "Discover Financial Services", "aliases": ["Discover Financial Services"], "ticker": "DFS", "category": "stocks"},
    {"canonical_name": "Synchrony Financial", "aliases": ["Synchrony Financial", "Synchrony"], "ticker": "SYF", "category": "stocks"},
    {"canonical_name": "Prudential Financial", "aliases": ["Prudential Financial"], "ticker": "PRU", "category": "stocks"},
    {"canonical_name": "MetLife", "aliases": ["MetLife"], "ticker": "MET", "category": "stocks"},
    {"canonical_name": "Aflac", "aliases": ["Aflac"], "ticker": "AFL", "category": "stocks"},
    {"canonical_name": "Progressive Corporation", "aliases": ["Progressive Corporation"], "ticker": "PGR", "category": "stocks"},
    {"canonical_name": "Allstate", "aliases": ["Allstate"], "ticker": "ALL", "category": "stocks"},
    {"canonical_name": "Travelers Companies", "aliases": ["Travelers Companies"], "ticker": "TRV", "category": "stocks"},
    {"canonical_name": "Chubb Limited", "aliases": ["Chubb Limited", "Chubb"], "ticker": "CB", "category": "stocks"},
    {"canonical_name": "Marsh McLennan", "aliases": ["Marsh McLennan"], "ticker": "MMC", "category": "stocks"},
    {"canonical_name": "Aon", "aliases": ["Aon"], "ticker": "AON", "category": "stocks"},
    {"canonical_name": "Arthur J Gallagher", "aliases": ["Arthur J. Gallagher", "Arthur J Gallagher"], "ticker": "AJG", "category": "stocks"},
    {"canonical_name": "Moody's Corporation", "aliases": ["Moody's Corporation", "Moody's"], "ticker": "MCO", "category": "stocks"},
    {"canonical_name": "S&P Global", "aliases": ["S&P Global"], "ticker": "SPGI", "category": "stocks"},
    {"canonical_name": "Intercontinental Exchange", "aliases": ["Intercontinental Exchange"], "ticker": "ICE", "category": "stocks"},
    {"canonical_name": "Nasdaq Inc", "aliases": ["Nasdaq Inc", "Nasdaq, Inc."], "ticker": "NDAQ", "category": "stocks"},
    {"canonical_name": "CME Group", "aliases": ["CME Group"], "ticker": "CME", "category": "stocks"},
    {"canonical_name": "Ameriprise Financial", "aliases": ["Ameriprise Financial", "Ameriprise"], "ticker": "AMP", "category": "stocks"},
    {"canonical_name": "Raymond James Financial", "aliases": ["Raymond James Financial", "Raymond James"], "ticker": "RJF", "category": "stocks"},
    {"canonical_name": "T Rowe Price", "aliases": ["T. Rowe Price", "T Rowe Price"], "ticker": "TROW", "category": "stocks"},
    {"canonical_name": "Invesco", "aliases": ["Invesco"], "ticker": "IVZ", "category": "stocks"},
    {"canonical_name": "Berkshire Hathaway", "aliases": ["Berkshire Hathaway"], "ticker": "BRK.B", "category": "stocks"},

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
    # --- Retail / Consumer additions (v1.3) ---
    {"canonical_name": "Kroger", "aliases": ["Kroger"], "ticker": "KR", "category": "stocks"},
    {"canonical_name": "TJX Companies", "aliases": ["TJX Companies", "TJX"], "ticker": "TJX", "category": "stocks"},
    {"canonical_name": "Ross Stores", "aliases": ["Ross Stores"], "ticker": "ROST", "category": "stocks"},
    {"canonical_name": "Dollar General", "aliases": ["Dollar General"], "ticker": "DG", "category": "stocks"},
    {"canonical_name": "Dollar Tree", "aliases": ["Dollar Tree"], "ticker": "DLTR", "category": "stocks"},
    {"canonical_name": "Best Buy", "aliases": ["Best Buy"], "ticker": "BBY", "category": "stocks"},
    {"canonical_name": "AutoZone", "aliases": ["AutoZone"], "ticker": "AZO", "category": "stocks"},
    {"canonical_name": "O'Reilly Automotive", "aliases": ["O'Reilly Automotive"], "ticker": "ORLY", "category": "stocks"},
    {"canonical_name": "Kimberly-Clark", "aliases": ["Kimberly-Clark"], "ticker": "KMB", "category": "stocks"},
    {"canonical_name": "Church & Dwight", "aliases": ["Church & Dwight"], "ticker": "CHD", "category": "stocks"},
    {"canonical_name": "Kraft Heinz", "aliases": ["Kraft Heinz"], "ticker": "KHC", "category": "stocks"},
    {"canonical_name": "General Mills", "aliases": ["General Mills"], "ticker": "GIS", "category": "stocks"},
    {"canonical_name": "Kellanova", "aliases": ["Kellanova"], "ticker": "K", "category": "stocks"},
    {"canonical_name": "Mondelez International", "aliases": ["Mondelez International", "Mondelez"], "ticker": "MDLZ", "category": "stocks"},
    {"canonical_name": "Hershey", "aliases": ["Hershey"], "ticker": "HSY", "category": "stocks"},
    {"canonical_name": "Constellation Brands", "aliases": ["Constellation Brands"], "ticker": "STZ", "category": "stocks"},
    {"canonical_name": "Molson Coors", "aliases": ["Molson Coors"], "ticker": "TAP", "category": "stocks"},
    {"canonical_name": "Monster Beverage", "aliases": ["Monster Beverage"], "ticker": "MNST", "category": "stocks"},
    {"canonical_name": "Estee Lauder", "aliases": ["Estee Lauder"], "ticker": "EL", "category": "stocks"},
    {"canonical_name": "Clorox", "aliases": ["Clorox"], "ticker": "CLX", "category": "stocks"},
    {"canonical_name": "Yum! Brands", "aliases": ["Yum! Brands", "Yum Brands"], "ticker": "YUM", "category": "stocks"},
    {"canonical_name": "Chipotle Mexican Grill", "aliases": ["Chipotle Mexican Grill", "Chipotle"], "ticker": "CMG", "category": "stocks"},
    {"canonical_name": "Darden Restaurants", "aliases": ["Darden Restaurants"], "ticker": "DRI", "category": "stocks"},
    {"canonical_name": "Domino's Pizza", "aliases": ["Domino's Pizza", "Dominos Pizza"], "ticker": "DPZ", "category": "stocks"},

    # --- International: Energy ---
    {"canonical_name": "ExxonMobil", "aliases": ["ExxonMobil", "Exxon Mobil", "Exxon"], "ticker": "XOM", "category": "stocks"},
    {"canonical_name": "Chevron", "aliases": ["Chevron"], "ticker": "CVX", "category": "stocks"},
    {"canonical_name": "Shell", "aliases": ["Shell"], "ticker": "SHEL", "category": "stocks"},
    {"canonical_name": "ConocoPhillips", "aliases": ["ConocoPhillips"], "ticker": "COP", "category": "stocks"},
    {"canonical_name": "Occidental Petroleum", "aliases": ["Occidental Petroleum", "Occidental"], "ticker": "OXY", "category": "stocks"},
    # --- Energy additions (v1.3) ---
    {"canonical_name": "Marathon Petroleum", "aliases": ["Marathon Petroleum"], "ticker": "MPC", "category": "stocks"},
    {"canonical_name": "Phillips 66", "aliases": ["Phillips 66"], "ticker": "PSX", "category": "stocks"},
    {"canonical_name": "Valero Energy", "aliases": ["Valero Energy", "Valero"], "ticker": "VLO", "category": "stocks"},
    {"canonical_name": "Williams Companies", "aliases": ["Williams Companies"], "ticker": "WMB", "category": "stocks"},
    {"canonical_name": "Kinder Morgan", "aliases": ["Kinder Morgan"], "ticker": "KMI", "category": "stocks"},
    {"canonical_name": "ONEOK", "aliases": ["ONEOK"], "ticker": "OKE", "category": "stocks"},
    {"canonical_name": "Baker Hughes", "aliases": ["Baker Hughes"], "ticker": "BKR", "category": "stocks"},
    {"canonical_name": "Halliburton", "aliases": ["Halliburton"], "ticker": "HAL", "category": "stocks"},
    {"canonical_name": "Schlumberger", "aliases": ["Schlumberger", "SLB"], "ticker": "SLB", "category": "stocks"},
    {"canonical_name": "EOG Resources", "aliases": ["EOG Resources"], "ticker": "EOG", "category": "stocks"},
    {"canonical_name": "Devon Energy", "aliases": ["Devon Energy"], "ticker": "DVN", "category": "stocks"},
    {"canonical_name": "Coterra Energy", "aliases": ["Coterra Energy"], "ticker": "CTRA", "category": "stocks"},
    {"canonical_name": "Diamondback Energy", "aliases": ["Diamondback Energy"], "ticker": "FANG", "category": "stocks"},
    {"canonical_name": "NextEra Energy", "aliases": ["NextEra Energy"], "ticker": "NEE", "category": "stocks"},
    {"canonical_name": "Duke Energy", "aliases": ["Duke Energy"], "ticker": "DUK", "category": "stocks"},
    {"canonical_name": "Southern Company", "aliases": ["Southern Company"], "ticker": "SO", "category": "stocks"},
    {"canonical_name": "Dominion Energy", "aliases": ["Dominion Energy"], "ticker": "D", "category": "stocks"},
    {"canonical_name": "American Electric Power", "aliases": ["American Electric Power", "AEP"], "ticker": "AEP", "category": "stocks"},
    {"canonical_name": "Exelon", "aliases": ["Exelon"], "ticker": "EXC", "category": "stocks"},
    {"canonical_name": "Sempra", "aliases": ["Sempra"], "ticker": "SRE", "category": "stocks"},
    {"canonical_name": "PG&E Corporation", "aliases": ["PG&E Corporation", "PG&E"], "ticker": "PCG", "category": "stocks"},
    {"canonical_name": "Edison International", "aliases": ["Edison International"], "ticker": "EIX", "category": "stocks"},
    {"canonical_name": "Xcel Energy", "aliases": ["Xcel Energy"], "ticker": "XEL", "category": "stocks"},
    {"canonical_name": "WEC Energy Group", "aliases": ["WEC Energy Group"], "ticker": "WEC", "category": "stocks"},
    {"canonical_name": "Entergy", "aliases": ["Entergy"], "ticker": "ETR", "category": "stocks"},
    {"canonical_name": "Public Service Enterprise Group", "aliases": ["Public Service Enterprise Group", "PSEG"], "ticker": "PEG", "category": "stocks"},

    # --- International: Media & Entertainment ---
    {"canonical_name": "Netflix", "aliases": ["Netflix"], "ticker": "NFLX", "category": "stocks"},
    {"canonical_name": "Walt Disney", "aliases": ["Disney"], "ticker": "DIS", "category": "stocks"},
    {"canonical_name": "Warner Bros Discovery", "aliases": ["Warner Bros Discovery", "Warner Bros"], "ticker": "WBD", "category": "stocks"},
    {"canonical_name": "Comcast", "aliases": ["Comcast"], "ticker": "CMCSA", "category": "stocks"},
    {"canonical_name": "Spotify", "aliases": ["Spotify"], "ticker": "SPOT", "category": "stocks"},
    # --- Media additions (v1.3) ---
    {"canonical_name": "Paramount Global", "aliases": ["Paramount Global", "Paramount"], "ticker": "PARA", "category": "stocks"},
    {"canonical_name": "Fox Corporation", "aliases": ["Fox Corporation"], "ticker": "FOXA", "category": "stocks"},
    {"canonical_name": "News Corp", "aliases": ["News Corp"], "ticker": "NWSA", "category": "stocks"},
    {"canonical_name": "Live Nation Entertainment", "aliases": ["Live Nation Entertainment", "Live Nation"], "ticker": "LYV", "category": "stocks"},
    {"canonical_name": "Electronic Arts", "aliases": ["Electronic Arts", "EA"], "ticker": "EA", "category": "stocks"},
    {"canonical_name": "Take-Two Interactive", "aliases": ["Take-Two Interactive"], "ticker": "TTWO", "category": "stocks"},
    {"canonical_name": "Roku", "aliases": ["Roku"], "ticker": "ROKU", "category": "stocks"},

    # --- International: Industrials ---
    {"canonical_name": "Boeing", "aliases": ["Boeing"], "ticker": "BA", "category": "stocks"},
    {"canonical_name": "Caterpillar", "aliases": ["Caterpillar"], "ticker": "CAT", "category": "stocks"},
    {"canonical_name": "General Electric", "aliases": ["General Electric"], "ticker": "GE", "category": "stocks"},
    {"canonical_name": "Honeywell", "aliases": ["Honeywell"], "ticker": "HON", "category": "stocks"},
    {"canonical_name": "3M", "aliases": ["3M"], "ticker": "MMM", "category": "stocks"},
    {"canonical_name": "Lockheed Martin", "aliases": ["Lockheed Martin"], "ticker": "LMT", "category": "stocks"},
    {"canonical_name": "RTX Corporation", "aliases": ["Raytheon", "RTX Corporation"], "ticker": "RTX", "category": "stocks"},
    # --- Industrials additions (v1.3) ---
    {"canonical_name": "Union Pacific Corporation", "aliases": ["Union Pacific Corporation", "Union Pacific"], "ticker": "UNP", "category": "stocks"},
    {"canonical_name": "Norfolk Southern", "aliases": ["Norfolk Southern"], "ticker": "NSC", "category": "stocks"},
    {"canonical_name": "CSX Corporation", "aliases": ["CSX Corporation", "CSX"], "ticker": "CSX", "category": "stocks"},
    {"canonical_name": "FedEx", "aliases": ["FedEx"], "ticker": "FDX", "category": "stocks"},
    {"canonical_name": "United Parcel Service", "aliases": ["United Parcel Service", "UPS"], "ticker": "UPS", "category": "stocks"},
    {"canonical_name": "Deere & Company", "aliases": ["Deere & Company", "John Deere"], "ticker": "DE", "category": "stocks"},
    {"canonical_name": "Parker Hannifin", "aliases": ["Parker Hannifin"], "ticker": "PH", "category": "stocks"},
    {"canonical_name": "Illinois Tool Works", "aliases": ["Illinois Tool Works", "ITW"], "ticker": "ITW", "category": "stocks"},
    {"canonical_name": "Emerson Electric", "aliases": ["Emerson Electric"], "ticker": "EMR", "category": "stocks"},
    {"canonical_name": "Eaton Corporation", "aliases": ["Eaton Corporation"], "ticker": "ETN", "category": "stocks"},
    {"canonical_name": "Cummins", "aliases": ["Cummins"], "ticker": "CMI", "category": "stocks"},
    {"canonical_name": "PACCAR", "aliases": ["PACCAR"], "ticker": "PCAR", "category": "stocks"},
    {"canonical_name": "Northrop Grumman", "aliases": ["Northrop Grumman"], "ticker": "NOC", "category": "stocks"},
    {"canonical_name": "General Dynamics", "aliases": ["General Dynamics"], "ticker": "GD", "category": "stocks"},
    {"canonical_name": "L3Harris Technologies", "aliases": ["L3Harris Technologies", "L3Harris"], "ticker": "LHX", "category": "stocks"},
    {"canonical_name": "TransDigm Group", "aliases": ["TransDigm Group", "TransDigm"], "ticker": "TDG", "category": "stocks"},
    {"canonical_name": "Waste Management", "aliases": ["Waste Management"], "ticker": "WM", "category": "stocks"},
    {"canonical_name": "Republic Services", "aliases": ["Republic Services"], "ticker": "RSG", "category": "stocks"},
    {"canonical_name": "Xylem", "aliases": ["Xylem"], "ticker": "XYL", "category": "stocks"},

    # --- International: Airlines ---
    {"canonical_name": "Delta Air Lines", "aliases": ["Delta Air Lines", "Delta Airlines"], "ticker": "DAL", "category": "stocks"},
    {"canonical_name": "United Airlines", "aliases": ["United Airlines"], "ticker": "UAL", "category": "stocks"},
    {"canonical_name": "Southwest Airlines", "aliases": ["Southwest Airlines"], "ticker": "LUV", "category": "stocks"},
    {"canonical_name": "American Airlines", "aliases": ["American Airlines"], "ticker": "AAL", "category": "stocks"},
    # --- Airlines additions (v1.3) ---
    {"canonical_name": "Alaska Air Group", "aliases": ["Alaska Air Group", "Alaska Airlines"], "ticker": "ALK", "category": "stocks"},
    {"canonical_name": "JetBlue Airways", "aliases": ["JetBlue Airways", "JetBlue"], "ticker": "JBLU", "category": "stocks"},

    # --- International: Telecommunications ---
    {"canonical_name": "AT&T", "aliases": ["AT&T"], "ticker": "T", "category": "stocks"},
    {"canonical_name": "Verizon", "aliases": ["Verizon"], "ticker": "VZ", "category": "stocks"},
    {"canonical_name": "T-Mobile US", "aliases": ["T-Mobile"], "ticker": "TMUS", "category": "stocks"},
    # --- Telecommunications additions (v1.3) ---
    {"canonical_name": "Nokia", "aliases": ["Nokia"], "ticker": "NOK", "category": "stocks"},
    {"canonical_name": "Ericsson", "aliases": ["Ericsson"], "ticker": "ERIC", "category": "stocks"},

    # --- International: Technology (additional) ---
    {"canonical_name": "Uber Technologies", "aliases": ["Uber"], "ticker": "UBER", "category": "stocks"},
    {"canonical_name": "ServiceNow", "aliases": ["ServiceNow"], "ticker": "NOW", "category": "stocks"},
    {"canonical_name": "Palantir Technologies", "aliases": ["Palantir"], "ticker": "PLTR", "category": "stocks"},
    {"canonical_name": "Shopify", "aliases": ["Shopify"], "ticker": "SHOP", "category": "stocks"},
    {"canonical_name": "Zoom Communications", "aliases": ["Zoom"], "ticker": "ZM", "category": "stocks"},
    {"canonical_name": "Snowflake", "aliases": ["Snowflake Inc"], "ticker": "SNOW", "category": "stocks"},

    # --- International: Real Estate (NEW sector, v1.3) ---
    {"canonical_name": "Prologis", "aliases": ["Prologis"], "ticker": "PLD", "category": "stocks"},
    {"canonical_name": "American Tower", "aliases": ["American Tower"], "ticker": "AMT", "category": "stocks"},
    {"canonical_name": "Equinix", "aliases": ["Equinix"], "ticker": "EQIX", "category": "stocks"},
    {"canonical_name": "Public Storage", "aliases": ["Public Storage"], "ticker": "PSA", "category": "stocks"},
    {"canonical_name": "Simon Property Group", "aliases": ["Simon Property Group"], "ticker": "SPG", "category": "stocks"},
    {"canonical_name": "Realty Income", "aliases": ["Realty Income"], "ticker": "O", "category": "stocks"},
    {"canonical_name": "Digital Realty Trust", "aliases": ["Digital Realty Trust", "Digital Realty"], "ticker": "DLR", "category": "stocks"},
    {"canonical_name": "Welltower", "aliases": ["Welltower"], "ticker": "WELL", "category": "stocks"},
    {"canonical_name": "AvalonBay Communities", "aliases": ["AvalonBay Communities", "AvalonBay"], "ticker": "AVB", "category": "stocks"},
    {"canonical_name": "Equity Residential", "aliases": ["Equity Residential"], "ticker": "EQR", "category": "stocks"},
    {"canonical_name": "Crown Castle", "aliases": ["Crown Castle"], "ticker": "CCI", "category": "stocks"},

    # --- International: Materials & Mining (NEW sector, v1.3) ---
    {"canonical_name": "Linde plc", "aliases": ["Linde plc", "Linde"], "ticker": "LIN", "category": "stocks"},
    {"canonical_name": "Air Products and Chemicals", "aliases": ["Air Products and Chemicals", "Air Products"], "ticker": "APD", "category": "stocks"},
    {"canonical_name": "Sherwin-Williams", "aliases": ["Sherwin-Williams"], "ticker": "SHW", "category": "stocks"},
    {"canonical_name": "Ecolab", "aliases": ["Ecolab"], "ticker": "ECL", "category": "stocks"},
    {"canonical_name": "Dow Inc", "aliases": ["Dow Inc"], "ticker": "DOW", "category": "stocks"},
    {"canonical_name": "DuPont de Nemours", "aliases": ["DuPont de Nemours", "DuPont"], "ticker": "DD", "category": "stocks"},
    {"canonical_name": "LyondellBasell Industries", "aliases": ["LyondellBasell Industries", "LyondellBasell"], "ticker": "LYB", "category": "stocks"},
    {"canonical_name": "Freeport-McMoRan", "aliases": ["Freeport-McMoRan"], "ticker": "FCX", "category": "stocks"},
    {"canonical_name": "Newmont Corporation", "aliases": ["Newmont Corporation", "Newmont"], "ticker": "NEM", "category": "stocks"},
    {"canonical_name": "Nucor Corporation", "aliases": ["Nucor Corporation", "Nucor"], "ticker": "NUE", "category": "stocks"},
    {"canonical_name": "Vale", "aliases": ["Vale"], "ticker": "VALE", "category": "stocks"},
    {"canonical_name": "Rio Tinto", "aliases": ["Rio Tinto"], "ticker": "RIO", "category": "stocks"},
    {"canonical_name": "BHP Group", "aliases": ["BHP Group", "BHP"], "ticker": "BHP", "category": "stocks"},
    {"canonical_name": "Glencore", "aliases": ["Glencore"], "ticker": "GLNCY", "category": "stocks"},
    {"canonical_name": "Albemarle Corporation", "aliases": ["Albemarle Corporation", "Albemarle"], "ticker": "ALB", "category": "stocks"},

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
    # --- Crypto additions (v1.3) ---
    {"canonical_name": "Shiba Inu", "aliases": ["Shiba Inu"], "ticker": "SHIB", "category": "crypto"},
    {"canonical_name": "Uniswap", "aliases": ["Uniswap"], "ticker": "UNI", "category": "crypto"},
    {"canonical_name": "Toncoin", "aliases": ["Toncoin"], "ticker": "TON", "category": "crypto"},
    {"canonical_name": "Tron", "aliases": ["Tron"], "ticker": "TRX", "category": "crypto"},
    {"canonical_name": "Cosmos", "aliases": ["Cosmos"], "ticker": "ATOM", "category": "crypto"},
    {"canonical_name": "Aave", "aliases": ["Aave"], "ticker": "AAVE", "category": "crypto"},
    {"canonical_name": "Near Protocol", "aliases": ["Near Protocol"], "ticker": "NEAR", "category": "crypto"},
    {"canonical_name": "Internet Computer", "aliases": ["Internet Computer"], "ticker": "ICP", "category": "crypto"},
    {"canonical_name": "Stellar", "aliases": ["Stellar Lumens", "Stellar"], "ticker": "XLM", "category": "crypto"},
    {"canonical_name": "Monero", "aliases": ["Monero"], "ticker": "XMR", "category": "crypto"},
    {"canonical_name": "Bitcoin Cash", "aliases": ["Bitcoin Cash"], "ticker": "BCH", "category": "crypto"},
    {"canonical_name": "Algorand", "aliases": ["Algorand"], "ticker": "ALGO", "category": "crypto"},
    {"canonical_name": "VeChain", "aliases": ["VeChain"], "ticker": "VET", "category": "crypto"},
    {"canonical_name": "Filecoin", "aliases": ["Filecoin"], "ticker": "FIL", "category": "crypto"},
    {"canonical_name": "The Graph", "aliases": ["The Graph"], "ticker": "GRT", "category": "crypto"},
    {"canonical_name": "Injective", "aliases": ["Injective"], "ticker": "INJ", "category": "crypto"},
    {"canonical_name": "Sui", "aliases": ["Sui"], "ticker": "SUI", "category": "crypto"},
    {"canonical_name": "Aptos", "aliases": ["Aptos"], "ticker": "APT", "category": "crypto"},
    {"canonical_name": "Arbitrum", "aliases": ["Arbitrum"], "ticker": "ARB", "category": "crypto"},
    {"canonical_name": "Optimism", "aliases": ["Optimism"], "ticker": "OP", "category": "crypto"},
    {"canonical_name": "Sei", "aliases": ["Sei Network", "Sei"], "ticker": "SEI", "category": "crypto"},
    {"canonical_name": "Celestia", "aliases": ["Celestia"], "ticker": "TIA", "category": "crypto"},
    {"canonical_name": "Pepe", "aliases": ["Pepe Coin"], "ticker": "PEPE", "category": "crypto"},
    {"canonical_name": "dYdX", "aliases": ["dYdX"], "ticker": "DYDX", "category": "crypto"},
    {"canonical_name": "MakerDAO", "aliases": ["MakerDAO"], "ticker": "MKR", "category": "crypto"},
    {"canonical_name": "Curve DAO", "aliases": ["Curve DAO", "Curve Finance"], "ticker": "CRV", "category": "crypto"},
    {"canonical_name": "Fantom", "aliases": ["Fantom"], "ticker": "FTM", "category": "crypto"},
    {"canonical_name": "Hedera", "aliases": ["Hedera", "Hedera Hashgraph"], "ticker": "HBAR", "category": "crypto"},
]
