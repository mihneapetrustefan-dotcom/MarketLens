"""
event_lexicon.py
--------------------
Event type lexicon for MarketLens's Event Detector.

RESPONSIBILITY:
Map each EVENT TYPE (from the project's event taxonomy — earnings,
M&A, leadership changes, legal/regulatory actions, etc.) to a list of
trigger PHRASES that reliably indicate that event type in a headline
or summary. Deliberately rule/dictionary-based (same philosophy as
sentiment_lexicon.py and company_registry.py) — transparent,
testable, and requires no ML model or hosting cost, at the cost of
recall on phrasing not anticipated here.

DESIGN NOTE — multi-word phrases preferred over single words:
Nearly every trigger here is 2+ words ("stock split", "guidance cut",
"files for bankruptcy") rather than a single generic word. This is
the same false-positive discipline already established for Company
Detector (short/ambiguous aliases are the main source of collisions)
— a single word like "cut" or "launch" would trigger constantly on
unrelated text; a specific phrase is far less likely to.

THIS IS v1 — a starting set covering the most common, highest-impact
event types from the project's taxonomy, not the full exhaustive list
(e.g. GDP/inflation/interest-rate macro events are intentionally
deferred — they need broader, less company-specific phrasing that
risks far more false positives, and deserve their own dedicated pass
rather than being bolted on here).
"""

from typing import Dict, List

EVENT_LEXICON: Dict[str, List[str]] = {
    "EARNINGS": [
        "quarterly earnings", "quarterly results", "quarterly revenue",
        "earnings report", "earnings call", "beat estimates", "missed estimates",
        "fiscal quarter", "q1 earnings", "q2 earnings", "q3 earnings", "q4 earnings",
    ],
    "DIVIDEND": [
        "declares dividend", "dividend increase", "dividend cut", "quarterly dividend",
        "special dividend", "raises its dividend",
    ],
    "STOCK_SPLIT": [
        "stock split", "share split", "2-for-1 split", "3-for-1 split",
    ],
    "BUYBACK": [
        "share buyback", "stock buyback", "repurchase program", "buyback program",
    ],
    "ACQUISITION": [
        "to acquire", "acquires", "acquisition of", "agreed to buy", "definitive agreement to acquire",
    ],
    "MERGER": [
        "merger with", "to merge with", "merger agreement", "completes merger",
    ],
    "INVESTMENT": [
        "to invest", "announces investment", "billion investment", "million investment",
        "capital investment", "invests in",
    ],
    "PRODUCT_LAUNCH": [
        "unveils new", "launches new", "announces new product", "product launch",
        "new product line",
    ],
    "CEO_CHANGE": [
        "ceo resigns", "ceo steps down", "names new ceo", "appoints new ceo",
        "ceo to retire", "new chief executive",
    ],
    "EXECUTIVE_CHANGE": [
        "cfo resigns", "chief financial officer", "names new cfo", "executive departure",
        "appoints new cfo", "chief operating officer",
    ],
    "GUIDANCE_UPGRADE": [
        "raises guidance", "raises forecast", "guidance increase", "boosts outlook",
        "upgrades full-year outlook",
    ],
    "GUIDANCE_DOWNGRADE": [
        "cuts guidance", "lowers guidance", "guidance cut", "lowers forecast",
        "cuts full-year outlook", "reduces outlook",
    ],
    "LAYOFFS": [
        "lays off", "layoffs", "job cuts", "workforce reduction", "cutting jobs",
        "to cut jobs",
    ],
    "LAWSUIT": [
        "files lawsuit", "sues", "class action lawsuit", "settles lawsuit",
        "legal action against",
    ],
    "REGULATION": [
        "regulatory approval", "antitrust investigation", "fined by regulators",
        "regulatory scrutiny", "under investigation",
    ],
    "CYBERATTACK": [
        "data breach", "cyberattack", "hacked", "security breach", "ransomware attack",
    ],
    "BANKRUPTCY": [
        "files for bankruptcy", "chapter 11", "bankruptcy protection", "insolvency",
    ],
    "CAPITAL_RAISE": [
        "raises capital", "secondary offering", "capital raise", "equity offering",
        "public offering",
    ],
}
