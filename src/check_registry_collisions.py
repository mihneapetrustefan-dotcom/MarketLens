"""
check_registry_collisions.py
--------------------------------
Automated consistency checker for company_registry.py / sector_registry.py.

RESPONSIBILITY:
With 389+ companies (and growing), a future edit adding a new company
could silently introduce an alias collision, a duplicate canonical
name, or a company with no sector mapped — exactly the class of bug
that already happened once with "Oracle" and "NOW" during manual
review. This module makes that check AUTOMATIC and repeatable, instead
of relying on someone noticing a wrong-looking Dashboard result.

DESIGNED TO RUN IN CI: exits with a non-zero status if any BLOCKING
issue is found (duplicate names, alias collisions, missing sectors),
so it can be added as a step in the Tests workflow and catch a bad
registry edit before it's ever committed. Ticker collisions are
reported as a WARNING only — they can be legitimate (e.g. the SAME
ticker "EL" genuinely belongs to Electrica on BVB and Estee Lauder on
NYSE — different exchanges, not a real conflict).
"""

import sys
from typing import Dict, List, Set

from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP

# Mirrors Company Detector's own rule: aliases at least this long are
# matched CASE-INSENSITIVELY, so a case-insensitive collision at this
# length or above is a real risk — shorter aliases are matched
# case-sensitively and are checked for EXACT collisions only.
_CASE_INSENSITIVE_MIN_LENGTH = 5


def find_duplicate_canonical_names() -> List[str]:
    """Return canonical_names that appear more than once in the registry."""
    names = [c["canonical_name"] for c in COMPANY_REGISTRY]
    return sorted(set(n for n in names if names.count(n) > 1))


def find_alias_collisions() -> Dict[str, Set[str]]:
    """
    Return every EXACT-match alias shared by 2+ DIFFERENT companies —
    e.g. two companies both listing "Global Corp" as an alias would
    make Company Detector unable to tell them apart.
    """
    alias_map: Dict[str, Set[str]] = {}
    for company in COMPANY_REGISTRY:
        for alias in company["aliases"]:
            alias_map.setdefault(alias, set()).add(company["canonical_name"])
    return {alias: names for alias, names in alias_map.items() if len(names) > 1}


def find_case_insensitive_alias_collisions(min_length: int = _CASE_INSENSITIVE_MIN_LENGTH) -> Dict[str, Set[str]]:
    """
    Same as find_alias_collisions(), but folding case — only for
    aliases long enough to actually be matched case-insensitively by
    Company Detector (mirrors its own length-based rule).
    """
    alias_map: Dict[str, Set[str]] = {}
    for company in COMPANY_REGISTRY:
        for alias in company["aliases"]:
            if len(alias) >= min_length:
                key = alias.lower()
                alias_map.setdefault(key, set()).add(company["canonical_name"])
    return {alias: names for alias, names in alias_map.items() if len(names) > 1}


def find_ticker_collisions() -> Dict[str, Set[str]]:
    """
    Return every ticker shared by 2+ DIFFERENT companies. INFORMATIONAL
    ONLY — not treated as blocking, since the same ticker string can
    legitimately belong to different companies on different exchanges
    (e.g. "EL" = Electrica on BVB, Estee Lauder on NYSE).
    """
    ticker_map: Dict[str, Set[str]] = {}
    for company in COMPANY_REGISTRY:
        ticker_map.setdefault(company["ticker"], set()).add(company["canonical_name"])
    return {ticker: names for ticker, names in ticker_map.items() if len(names) > 1}


def find_companies_missing_sector() -> List[str]:
    """Return canonical_names present in COMPANY_REGISTRY but absent from COMPANY_SECTOR_MAP."""
    company_names = {c["canonical_name"] for c in COMPANY_REGISTRY}
    sector_names = set(COMPANY_SECTOR_MAP.keys())
    return sorted(company_names - sector_names)


def run_all_checks(verbose: bool = True) -> bool:
    """
    Run every check and report the results.

    Returns:
        True if the registry has no BLOCKING issues (duplicate names,
        alias collisions of either kind, or a company missing its
        sector). Ticker collisions are reported but never cause this
        to return False.
    """
    ok = True

    duplicate_names = find_duplicate_canonical_names()
    if duplicate_names:
        ok = False
        if verbose:
            print(f"EROARE: nume canonice duplicate: {duplicate_names}")

    alias_collisions = find_alias_collisions()
    if alias_collisions:
        ok = False
        if verbose:
            print(f"EROARE: coliziuni de alias exacte: {alias_collisions}")

    case_insensitive_collisions = find_case_insensitive_alias_collisions()
    if case_insensitive_collisions:
        ok = False
        if verbose:
            print(f"EROARE: coliziuni de alias case-insensitive: {case_insensitive_collisions}")

    missing_sector = find_companies_missing_sector()
    if missing_sector:
        ok = False
        if verbose:
            print(f"EROARE: companii fără sector mapat: {missing_sector}")

    ticker_collisions = find_ticker_collisions()
    if ticker_collisions and verbose:
        print(f"AVERTISMENT (nu blochează): tichere partajate de mai multe companii: {ticker_collisions}")

    if ok and verbose:
        print(f"OK — {len(COMPANY_REGISTRY)} companii verificate, fără coliziuni blocante.")

    return ok


if __name__ == "__main__":
    success = run_all_checks(verbose=True)
    sys.exit(0 if success else 1)
