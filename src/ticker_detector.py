"""
ticker_detector.py
---------------------
Ticker Detector module for MarketLens.

RESPONSIBILITY:
Identify stock/ETF/forex/crypto ticker SYMBOLS written directly in
article text (e.g. "$AAPL", "TLV", "BTC", "EURUSD"), independent of
whether the full company/asset name also appears. This complements
Company Detector, which matches full names/aliases — some headlines
reference an asset only by its ticker (e.g. "TLV +3% after earnings"
mentions no company name at all).

DETECTION STRATEGY (two distinct signals, handled differently):

1. CASHTAGS ("$AAPL" style) — the "$" prefix is a strong, unambiguous
   signal that the following letters ARE a ticker; this convention is
   used across financial media and social platforms specifically for
   this purpose. Every cashtag found is reported. If it matches our
   known registry, full metadata (name, category) is attached;
   otherwise it's still reported, with name/category set to None —
   we never silently drop a signal the text clearly intended as a
   ticker just because we don't recognize the specific symbol yet.

2. BARE tickers (no "$" prefix, e.g. "TLV", "BTC") — these are ONLY
   matched against ticker_registry.py's WHITELIST of known symbols,
   never guessed freely. A bare 2-4 letter uppercase token could
   easily be an unrelated abbreviation ("CEO", "GDP", "IPO"), so
   restricting to a curated whitelist — matched case-SENSITIVELY, with
   word boundaries — keeps false positives low.
"""

import re
import logging
from typing import List, Dict, Any, Optional

from ticker_registry import TICKER_REGISTRY

logger = logging.getLogger("marketlens.ticker_detector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class TickerDetector:
    """
    Detects stock/ETF/forex/crypto ticker symbols in article text,
    both cashtag-style ($AAPL) and bare (TLV, BTC).
    """

    # Matches "$" followed by 1-6 letters — the standard cashtag shape.
    _CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})\b")

    # Bare (no "$") tickers shorter than this are excluded from bare
    # matching entirely. WHY: single-character tickers (e.g. "M" for
    # MedLife) collide constantly with ordinary text — a middle initial
    # like "Kathleen M. Hutchinson", a stray list marker, a single
    # capital letter — none of which are ticker mentions. A "$" prefix
    # removes that ambiguity (a real observed case: "$M" is unambiguous,
    # bare "M" is not), so 1-character tickers are still detected via
    # cashtag, just never via bare matching.
    _MIN_BARE_TICKER_LENGTH = 2

    def __init__(self, registry: Optional[List[Dict[str, Any]]] = None):
        """
        Args:
            registry: list of ticker entries (see ticker_registry.py
                for the exact shape). Defaults to the built-in
                TICKER_REGISTRY. Injectable so unit tests can use a
                small, controlled registry, independent of the real one.
        """
        self.registry = registry if registry is not None else TICKER_REGISTRY

        # Index by uppercase ticker string for O(1) metadata lookup —
        # a plain dict lookup is the right structure here (unlike
        # Company Detector's alias matching), since tickers are exact
        # symbols with no alternate spellings to account for. This
        # index covers ALL tickers, including 1-character ones — it's
        # used for cashtag resolution too, where the "$" already
        # disambiguates short symbols.
        self._by_ticker: Dict[str, Dict[str, Any]] = {
            entry["ticker"].upper(): entry for entry in self.registry
        }

        # ONE combined regex covering every known bare ticker AT OR
        # ABOVE the minimum length, built once at construction time
        # rather than looping over the whole registry per article — far
        # cheaper at the scale this platform targets (thousands of
        # articles per run). Longer symbols are placed first in the
        # alternation so a longer match always wins if one ticker
        # string happens to be a prefix of another (defensive; not
        # currently the case in our registry, but free to guarantee).
        known_tickers = sorted(
            (t for t in self._by_ticker if len(t) >= self._MIN_BARE_TICKER_LENGTH),
            key=len, reverse=True,
        )
        if known_tickers:
            alternation = "|".join(re.escape(t) for t in known_tickers)
            # Case-SENSITIVE, uppercase-only by convention: real ticker
            # symbols are written in capitals, and requiring an exact-
            # case match is the single biggest lever against false
            # positives from ordinary capitalized words or acronyms.
            self._bare_ticker_re: Optional["re.Pattern"] = re.compile(rf"\b(?:{alternation})\b")
        else:
            self._bare_ticker_re = None

    def _detect_cashtags(self, text: str) -> List[Dict[str, Any]]:
        """
        Find every "$SYMBOL" cashtag in the text.

        Returns one entry per DISTINCT symbol, tagged with
        match_type="cashtag". Symbols not in our registry are still
        included, with name/category set to None.
        """
        found: Dict[str, Dict[str, Any]] = {}
        for match in self._CASHTAG_RE.finditer(text):
            symbol = match.group(1).upper()
            if symbol in found:
                continue
            entry = self._by_ticker.get(symbol)
            found[symbol] = {
                "ticker": symbol,
                "name": entry["name"] if entry else None,
                "category": entry["category"] if entry else None,
                "match_type": "cashtag",
            }
        return list(found.values())

    def _detect_bare_tickers(self, text: str) -> List[Dict[str, Any]]:
        """
        Find bare (no "$") occurrences of KNOWN tickers only — never a
        freeform guess, always checked against the registry whitelist.
        """
        if self._bare_ticker_re is None:
            return []

        found: Dict[str, Dict[str, Any]] = {}
        for match in self._bare_ticker_re.finditer(text):
            symbol = match.group(0)  # already exact-case (uppercase) by construction
            if symbol in found:
                continue
            entry = self._by_ticker[symbol]
            found[symbol] = {
                "ticker": symbol,
                "name": entry["name"],
                "category": entry["category"],
                "match_type": "bare",
            }
        return list(found.values())

    def detect_in_text(self, text: str) -> List[Dict[str, Any]]:
        """
        Detect all ticker mentions (cashtag + bare) in a block of text.

        Returns:
            A list of dicts, one per distinct ticker symbol found. If a
            symbol appears BOTH as a cashtag and bare in the same text,
            the cashtag version wins — it's the higher-confidence,
            explicit financial reference of the two.
        """
        if not text:
            return []

        bare_matches = self._detect_bare_tickers(text)
        cashtag_matches = self._detect_cashtags(text)

        merged: Dict[str, Dict[str, Any]] = {}
        for entry in bare_matches:
            merged[entry["ticker"]] = entry
        for entry in cashtag_matches:
            merged[entry["ticker"]] = entry  # cashtag overrides a bare match of the same symbol

        return list(merged.values())

    def detect_in_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detect tickers mentioned in one article's title + summary and
        return a NEW article dict tagged with `tickers_mentioned`.

        Follows the same copy-don't-mutate discipline as every other
        module in this pipeline: a new dict is returned, the caller's
        original article is left untouched.
        """
        combined_text = f"{article.get('title', '')} {article.get('summary', '')}"
        tickers = self.detect_in_text(combined_text)

        tagged = dict(article)
        tagged["tickers_mentioned"] = tickers
        return tagged

    def detect_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run ticker detection over an entire batch of articles.

        Returns:
            A new list, same order, every article tagged with
            `tickers_mentioned` (an empty list where none were found).
        """
        tagged_articles = [self.detect_in_article(article) for article in articles]

        with_tickers = sum(1 for a in tagged_articles if a["tickers_mentioned"])
        logger.info(
            "Ticker Detector: %d of %d articles mention at least one ticker",
            with_tickers, len(tagged_articles),
        )
        return tagged_articles
