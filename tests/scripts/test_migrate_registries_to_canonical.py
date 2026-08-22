"""
scripts/migrate_registries_to_canonical.py
-----------------------------------------------
One-off (but safely re-runnable) migration: populates the Phase 1
canonical tables from the EXISTING, unmodified registries.

RESPONSIBILITY:
Read company_registry.COMPANY_REGISTRY, sector_registry.COMPANY_SECTOR_MAP,
sources.RSS_FEEDS, and source_credibility.SOURCE_TIERS — exactly as
they are today, unmodified — and write the equivalent canonical rows
into exchanges / sectors / companies / securities / instruments /
news_sources.

SAFE TO RUN REPEATEDLY: every write goes through a repository's
INSERT OR REPLACE save() method; running this script twice against the
same database produces the same end state, not duplicates.

DOES NOT TOUCH: data/marketlens.db's EXISTING tables (articles,
recommendations, portfolio_snapshots) — this script only calls
initialize_schema() (additive) and the NEW repositories.

USAGE:
    python scripts/migrate_registries_to_canonical.py [path/to/marketlens.db]

    If no path is given, defaults to data/marketlens.db relative to
    the repository root (the same file run_daily.py already uses) —
    running this against the real database is intentional and safe,
    per the additive-schema guarantee above.
"""

import sys
import os
import sqlite3

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from src.data_access.schema import initialize_schema
from src.data_access.repositories import (
    ExchangeRepository, SectorRepository, CompanyRepository,
    SecurityRepository, InstrumentRepository, NewsSourceRepository,
)
from src.providers.registry_adapter import (
    SectorRegistryAdapter, CompanyRegistryAdapter, NewsSourceRegistryAdapter,
    _CATEGORY_TO_EXCHANGE,
)

from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP
from sources import RSS_FEEDS
from source_credibility import SOURCE_TIERS


def run_migration(db_path: str) -> dict:
    """
    Execute the migration against the given SQLite database path.

    Returns:
        A summary dict {"exchanges", "sectors", "companies",
        "securities", "instruments", "news_sources"} with the count of
        rows written for each — used both for the CLI printout and by
        the test suite to assert on real outcomes.
    """
    conn = sqlite3.connect(db_path)
    initialize_schema(conn)

    exchange_repo = ExchangeRepository(conn)
    sector_repo = SectorRepository(conn)
    company_repo = CompanyRepository(conn)
    security_repo = SecurityRepository(conn)
    instrument_repo = InstrumentRepository(conn)
    news_source_repo = NewsSourceRepository(conn)

    # --- Exchanges (the 3 placeholders every company migrates onto) ---
    for exchange in _CATEGORY_TO_EXCHANGE.values():
        exchange_repo.save(exchange)

    # --- Sectors ---
    sector_names = list(set(COMPANY_SECTOR_MAP.values()))
    sectors = SectorRegistryAdapter().normalize(sector_names)
    for sector in sectors:
        sector_repo.save(sector)

    # --- Companies + Securities + Instruments ---
    company_adapter = CompanyRegistryAdapter(sector_map=COMPANY_SECTOR_MAP)
    company_tuples = company_adapter.normalize(COMPANY_REGISTRY)
    for company, security, instrument, _exchange in company_tuples:
        company_repo.save(company)
        security_repo.save(security)
        instrument_repo.save(instrument)

    # --- News Sources ---
    news_source_adapter = NewsSourceRegistryAdapter(tier_map=SOURCE_TIERS)
    news_sources = news_source_adapter.normalize(RSS_FEEDS)
    for source in news_sources:
        news_source_repo.save(source)

    conn.close()

    return {
        "exchanges": len(_CATEGORY_TO_EXCHANGE),
        "sectors": len(sectors),
        "companies": len(company_tuples),
        "securities": len(company_tuples),
        "instruments": len(company_tuples),
        "news_sources": len(news_sources),
    }


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "data", "marketlens.db")
    print(f"Migrating existing registries into canonical tables at: {db_path}")
    summary = run_migration(db_path)
    for key, count in summary.items():
        print(f"  {key}: {count}")
    print("Migration complete. Existing tables (articles, recommendations, portfolio_s
