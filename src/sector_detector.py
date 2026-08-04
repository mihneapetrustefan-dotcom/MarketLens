"""
sector_detector.py
---------------------
Sector Detector module for MarketLens.

RESPONSIBILITY:
Classify each article into economic sector(s) — Technology, Energy,
Financial Services, Healthcare, ... — using two complementary signals:

1. COMPANY-BASED (high confidence): if the article already has
   `companies_mentioned` (populated by Company Detector), each known
   company maps directly to a sector via COMPANY_SECTOR_MAP. This is
   the preferred path whenever available — precise and deterministic,
   no guessing involved.

2. KEYWORD-BASED (fallback, lower confidence): used only to catch
   sectors NOT already found via companies. This handles genuinely
   sector-wide stories that name no specific company (e.g. "Oil prices
   surge on supply concerns"), by scanning text for sector-indicative
   keywords/phrases from SECTOR_KEYWORDS.

An article can belong to MULTIPLE sectors (e.g. it mentions both a bank
and an energy company) — this module reports every sector found, each
tagged with its confidence source, rather than forcing a single label.
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

from sector_registry import COMPANY_SECTOR_MAP, SECTOR_KEYWORDS

logger = logging.getLogger("marketlens.sector_detector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class SectorDetector:
    """
    Classifies articles into economic sectors using company mentions
    (preferred) and sector keywords (fallback).
    """

    def __init__(
        self,
        company_sector_map: Optional[Dict[str, str]] = None,
        sector_keywords: Optional[Dict[str, List[str]]] = None,
    ):
        """
        Args:
            company_sector_map: canonical company name -> sector.
                Defaults to the built-in COMPANY_SECTOR_MAP. Injectable
                for isolated unit testing.
            sector_keywords: sector -> list of fallback keywords/phrases.
                Defaults to the built-in SECTOR_KEYWORDS.
        """
        self.company_sector_map = company_sector_map if company_sector_map is not None else COMPANY_SECTOR_MAP
        self.sector_keywords = sector_keywords if sector_keywords is not None else SECTOR_KEYWORDS

        # Precompile one word-boundary regex per (sector, keyword) pair,
        # once, at construction time — this module runs over thousands
        # of articles, so repeated compilation on every call would be
        # wasteful. Case-INSENSITIVE here: unlike Ticker Detector's
        # short acronyms, these are ordinary words/phrases with no
        # meaningful acronym-collision risk, so requiring exact case
        # would only lose real matches for no real benefit.
        self._keyword_patterns: List[Tuple[str, str, "re.Pattern"]] = []
        for sector, keywords in self.sector_keywords.items():
            for keyword in keywords:
                pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
                self._keyword_patterns.append((sector, keyword, pattern))

    def _sectors_from_companies(self, companies_mentioned: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Map each already-detected company to its sector via
        COMPANY_SECTOR_MAP.

        Returns:
            A list of dicts: {"sector", "source": "company", "via": [...]}
            — one per DISTINCT sector found. `via` lists EVERY
            contributing company, not just the first — e.g. if both
            Alphabet and Microsoft (both Technology) are mentioned in
            one article, `via` reports both, so the classification is
            fully transparent and downstream modules (Impact Score)
            can see the full corroborating set, not an arbitrary sample
            of one.
        """
        found: Dict[str, Dict[str, Any]] = {}
        for company in companies_mentioned:
            name = company.get("company")
            sector = self.company_sector_map.get(name)
            if not sector:
                continue
            if sector not in found:
                found[sector] = {"sector": sector, "source": "company", "via": [name]}
            elif name not in found[sector]["via"]:
                found[sector]["via"].append(name)
        return list(found.values())

    def _sectors_from_keywords(self, text: str) -> List[Dict[str, Any]]:
        """
        Scan text for sector-indicative keywords/phrases (the fallback
        path — see class docstring).

        Returns:
            A list of dicts: {"sector", "source": "keyword", "via": [...]}
            — one per DISTINCT sector found. `via` lists every matched
            keyword/phrase for that sector (same list-based shape as
            the company-based path, for a consistent schema regardless
            of which source classified the sector).
        """
        if not text:
            return []
        found: Dict[str, Dict[str, Any]] = {}
        for sector, keyword, pattern in self._keyword_patterns:
            if not pattern.search(text):
                continue
            if sector not in found:
                found[sector] = {"sector": sector, "source": "keyword", "via": [keyword]}
            elif keyword not in found[sector]["via"]:
                found[sector]["via"].append(keyword)
        return list(found.values())

    def detect_in_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classify one article into sector(s) and return a NEW article
        dict tagged with `sectors`.

        DESIGN DECISION: the company-based path always runs first and
        is preferred; the keyword-based path only contributes sectors
        NOT already found via companies, so a sector is never reported
        twice with two different confidence sources for one article.

        Gracefully handles an article with no `companies_mentioned` key
        at all (e.g. if this module is ever run before Company Detector
        in some future pipeline variant) by treating it as an empty list.
        """
        companies_mentioned = article.get("companies_mentioned", [])
        company_sectors = self._sectors_from_companies(companies_mentioned)
        found_sector_names = {s["sector"] for s in company_sectors}

        combined_text = f"{article.get('title', '')} {article.get('summary', '')}"
        keyword_sectors = [
            s for s in self._sectors_from_keywords(combined_text)
            if s["sector"] not in found_sector_names
        ]

        tagged = dict(article)  # shallow copy — never mutate the caller's dict
        tagged["sectors"] = company_sectors + keyword_sectors
        return tagged

    def detect_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run sector detection over an entire batch of articles.

        Returns:
            A new list, same order, every article tagged with
            `sectors` (an empty list where no sector could be
            determined either way).
        """
        tagged_articles = [self.detect_in_article(article) for article in articles]

        with_sectors = sum(1 for a in tagged_articles if a["sectors"])
        logger.info(
            "Sector Detector: %d of %d articles classified into at least one sector",
            with_sectors, len(tagged_articles),
        )
        return tagged_articles
