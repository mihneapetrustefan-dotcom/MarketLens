#!/usr/bin/env python3
"""
run_weekly_backfill.py
--------------------------
MarketLens weekly historical backfill automation entry point.

RESPONSIBILITY:
Search Google News for every tracked company's last ~60 days of
coverage (via Google News Historical Backfill), merge whatever is new
into the accumulated database, then re-run the full daily pipeline so
Confidence Score / Recommendation Engine benefit from the deeper
history immediately, in the same run.

WHY SEPARATE FROM run_daily.py: scanning the entire company registry
against Google News — one request per company, with a polite delay
between each — takes several minutes. Worth doing periodically
(weekly), not on every single daily run, which should stay fast.
"""

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from google_news_sources import build_entity_search_sources
from company_registry import COMPANY_REGISTRY
from rss_collector import RSSCollector
from pipeline_core import process_articles
from news_database import NewsDatabase

import run_daily  # reuses the exact same scoring/recommendation/Dashboard pipeline

DB_PATH = str(REPO_ROOT / "data" / "marketlens.db")


def run_backfill() -> None:
    """
    Search Google News for every company in the registry and merge any
    new articles found into the persistent database. Never raises on
    an individual company's search failing (RSSCollector already
    isolates per-source failures) — a slow or unreachable search for
    one company must never block the rest.
    """
    category_lookup = {c["canonical_name"]: c["category"] for c in COMPANY_REGISTRY}
    all_companies = [c["canonical_name"] for c in COMPANY_REGISTRY]

    sources = build_entity_search_sources(all_companies, days_back=60, category_lookup=category_lookup)
    print(f"=== Weekly backfill: searching Google News for {len(sources)} tracked companies ===")

    collector = RSSCollector(feeds=sources)
    historical_raw = []
    for i, source in enumerate(sources):
        articles = collector.collect_from_source(source)
        historical_raw.extend(a.to_dict() for a in articles)
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(sources)} companies searched")
        time.sleep(1)  # polite delay between requests, same as the notebook version

    print(f"Historical backfill found {len(historical_raw)} article(s) total")

    processed = process_articles(historical_raw)

    db = NewsDatabase(DB_PATH)
    newly_stored = db.save_articles(processed)
    print(f"Archived {newly_stored} new historical article(s) into the database")
    db.close()


if __name__ == "__main__":
    run_backfill()
    # Re-run the standard daily pipeline so the freshly-backfilled
    # history is immediately reflected in Confidence Score,
    # Recommendation Engine, and the Dashboard — not just sitting in
    # the database until tomorrow's daily run picks it up.
    sys.exit(run_daily.main())
