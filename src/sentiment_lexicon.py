"""
sentiment_lexicon.py
-----------------------
Curated financial sentiment lexicon for MarketLens.

Two word/phrase lists (English + Romanian), used by sentiment_engine.py
to score article tone. Words are chosen for FINANCIAL/MARKET context
specifically (e.g. "beats estimates", "profit warning") rather than
generic sentiment words, since generic sentiment lexicons (built for
product reviews, social media, etc.) perform poorly on financial text —
a word like "aggressive" is negative in a review but often neutral-to-
positive in "aggressive expansion plans".

NOTE (v1 limitation, documented rather than hidden): this lexicon-based
approach does NOT handle negation ("shares did NOT rise" would still
score as positive, due to "rise"). Adding negation handling is a
reasonable v2 improvement — flagged here rather than attempted now, to
keep this module's first version simple, correct, and fully tested for
what it does claim to do.
"""

from typing import List

POSITIVE_WORDS: List[str] = [
    # English
    "surge", "surges", "surged", "soar", "soars", "soared", "rally",
    "rallies", "rallied", "gain", "gains", "gained", "jump", "jumps",
    "jumped", "rise", "rises", "rose", "beat", "beats", "outperform",
    "outperforms", "upgrade", "upgrades", "upgraded", "bullish",
    "growth", "expand", "expands", "expansion", "profit", "profits",
    "profitable", "strong", "recovery", "recovers", "boost", "boosts",
    "record high", "record profit", "wins", "winning", "optimistic",
    "exceeds expectations", "beats estimates",
    # Romanian
    "creste", "creșterea", "creștere", "profit", "castig", "câștig",
    "avans", "urca", "urcă", "record", "optimist", "redresare",
]

NEGATIVE_WORDS: List[str] = [
    # English
    "plunge", "plunges", "plunged", "crash", "crashes", "crashed",
    "drop", "drops", "dropped", "fall", "falls", "fell", "decline",
    "declines", "declined", "loss", "losses", "downgrade",
    "downgrades", "downgraded", "bearish", "recession", "layoffs",
    "lawsuit", "fraud", "scandal", "bankruptcy", "warns", "warning",
    "profit warning", "cut", "cuts", "miss", "misses", "missed",
    "weak", "slump", "slumps", "slumped", "sell-off", "selloff",
    "misses estimates", "below expectations",
    # Romanian
    "scadere", "scădere", "pierdere", "pierderi", "scade", "criza",
    "criză", "faliment", "avertisment", "concedieri", "fraudă",
    "scandal",
]
