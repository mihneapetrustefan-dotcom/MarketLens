"""
company_detector.py
----------------------
Company Detector module for MarketLens.

RESPONSIBILITY:
Given a cleaned article (or a batch of them), identify which KNOWN
companies are mentioned in its title/summary text, using a curated
registry of company names and aliases (company_registry.py).

This module does NOT infer companies it has never seen before — that
would require full Named Entity Recognition, which is out of scope for
v1 (see the "Improvements" notes when this module is reviewed). It
performs precise, registry-based matching instead: fully deterministic,
fast, and trivially extensible by adding one entry to the registry —
no model, no retraining.
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

from company_registry import COMPANY_REGISTRY

logger = logging.getLogger("marketlens.company_detector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class CompanyDetector:
    """
    Detects mentions of known companies in article text using a
    registry of canonical names + aliases.
    """

    # Aliases at or below this length are matched CASE-SENSITIVELY.
    # WHY: short aliases/acronyms ("BT", "GS", "Meta") are far more
    # likely to collide with ordinary words or unrelated fragments when
    # matched case-insensitively — e.g. lowercase "meta" inside "meta
    # description", or "gs" as a stray abbreviation unrelated to Goldman
    # Sachs. Requiring exact capitalization for short aliases
    # meaningfully cuts false positives, while longer, more distinctive
    # names ("Hidroelectrica", "Tesla") are safe to match regardless of
    # case, since collisions with unrelated text are far less likely.
    _CASE_SENSITIVE_MAX_LENGTH = 4

    def __init__(self, registry: Optional[List[Dict[str, Any]]] = None):
        """
        Args:
            registry: list of company entries (see company_registry.py
                for the exact shape). Defaults to the built-in
                COMPANY_REGISTRY. Injectable so unit tests can use a
                small, controlled registry, fully independent of the
                real production list.
        """
        self.registry = registry if registry is not None else COMPANY_REGISTRY

        # Precompile one regex per (company, alias) pair up front, ONCE,
        # rather than re-compiling patterns on every call to
        # detect_in_text — this module is meant to run over thousands
        # of articles, so avoiding repeated regex compilation matters
        # for throughput at that scale.
        self._compiled: List[Tuple[Dict[str, Any], str, "re.Pattern"]] = []
        for company in self.registry:
            for alias in company["aliases"]:
                pattern = self._build_pattern(alias)
                self._compiled.append((company, alias, pattern))

    def _build_pattern(self, alias: str) -> "re.Pattern":
        """
        Build a word-boundary regex for one alias, choosing case
        sensitivity based on alias length (see class docstring).

        Word boundaries (\\b) ensure "Tesla" matches the standalone word
        "Tesla" but never matches as a fragment inside an unrelated
        longer word (e.g. it would NOT match inside "Teslas" — there's
        no boundary between "a" and "s").
        """
        escaped = re.escape(alias)
        flags = 0 if len(alias) <= self._CASE_SENSITIVE_MAX_LENGTH else re.IGNORECASE
        return re.compile(rf"\b{escaped}\b", flags)

    def detect_in_text(self, text: str) -> List[Dict[str, Any]]:
        """
        Scan a block of text and return every KNOWN company found.

        Returns:
            A list of dicts: {"company", "ticker", "category",
            "matched_alias"} — one entry per DISTINCT company found. If
            a company matches via two different aliases in the same
            text (e.g. both "Meta" and "Facebook" appear), it is still
            reported only once, under its canonical name.
        """
        if not text:
            return []

        found: Dict[str, Dict[str, Any]] = {}  # canonical_name -> match record
        for company, alias, pattern in self._compiled:
            canonical = company["canonical_name"]
            if canonical in found:
                continue  # already matched this company via an earlier alias
            if pattern.search(text):
                found[canonical] = {
                    "company": canonical,
                    "ticker": company.get("ticker"),
                    "category": company.get("category"),
                    "matched_alias": alias,
                }
        return list(found.values())

    def detect_in_article(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detect companies mentioned in one article's title + summary and
        return a NEW article dict tagged with `companies_mentioned`.

        DESIGN DECISION: title and summary are combined into a single
        search text rather than searched separately, because we only
        care WHETHER a company is mentioned, not which specific field
        it appeared in.

        Follows the same copy-don't-mutate discipline as News Cleaner
        and Duplicate Detector: a NEW dict is returned, the caller's
        original article is left untouched.
        """
        combined_text = f"{article.get('title', '')} {article.get('summary', '')}"
        companies = self.detect_in_text(combined_text)

        tagged = dict(article)
        tagged["companies_mentioned"] = companies
        return tagged

    def detect_batch(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Run company detection over an entire batch of articles.

        Returns:
            A new list, same order, every article tagged with
            `companies_mentioned` (an empty list for articles that
            mention no known company).
        """
        tagged_articles = [self.detect_in_article(article) for article in articles]

        with_companies = sum(1 for a in tagged_articles if a["companies_mentioned"])
        logger.info(
            "Company Detector: %d of %d articles mention at least one known company",
            with_companies, len(tagged_articles),
        )
        return tagged_articles
