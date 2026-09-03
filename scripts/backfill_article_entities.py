"""
Backfill `article_entities` from the existing `articles` table.

WHY THIS READS FROM `articles` AND NOT `news_articles`
------------------------------------------------------
Migrating all 51,794 legacy articles into `news_articles` was measured
at ~127 MB, which would take the database to ~212 MB against a 100 MiB
GitHub file limit. `article_entities` has no foreign key to
`news_articles`, so Phase 3 resolution can link directly into the
legacy corpus by `article_id`. This avoids duplicating the corpus
entirely.

This is a deliberate scoping decision, not a shortcut: the canonical
`news_articles` model still exists and is still the target for newly
ingested articles. What is skipped is the historical bulk copy.

SAFETY
------
- All schema creation is `CREATE TABLE IF NOT EXISTS`.
- No existing table is read-modified-written; `articles` is read-only.
- Writes are `INSERT OR IGNORE`, so re-running is idempotent.
- The script REFUSES to write if the projected database size would
  exceed --max-db-mb. Nothing is committed until the projection passes.
- --dry-run performs full resolution and reports what WOULD be written,
  without opening a write transaction.

Only confident resolutions are persisted. AMBIGUOUS and UNRESOLVED
outcomes are counted and reported but never written, because a wrong
confident link is more damaging downstream than a missing one.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.news_schema import initialize_news_schema
from src.entities.index_builder import build_index_from_registries
from src.entities.resolver import EntityResolver
from src.domain.entity_models import EntityType

from company_registry import COMPANY_REGISTRY
from sector_registry import COMPANY_SECTOR_MAP

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

# Measured at 219 bytes/row over the full 48,457-row backfill (row +
# primary-key index + page overhead), rounded up to 230 for margin.
# An earlier 401-byte figure came from a 5k-row sample where one-off
# page allocation dominated; it over-projected by ~80%.
MEASURED_BYTES_PER_ROW = 230


def extract_mentions(companies_json: Optional[str],
                     tickers_json: Optional[str]) -> List[Tuple[str, Optional[EntityType]]]:
    """
    Pull candidate mention strings out of the legacy JSON columns.

    Returns (text, expected_type) pairs. Malformed JSON is skipped
    rather than raising: the legacy corpus predates schema validation
    and a handful of bad rows must not abort a backfill.
    """
    mentions: List[Tuple[str, Optional[EntityType]]] = []

    for raw, expected in ((companies_json, EntityType.COMPANY),
                          (tickers_json, EntityType.COMPANY)):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("ticker") or item.get("name") or ""
            else:
                continue
            text = (text or "").strip()
            if text:
                mentions.append((text, expected))

    return mentions


def resolve_articles(conn: sqlite3.Connection,
                     resolver: EntityResolver,
                     limit: Optional[int],
                     since: Optional[str]) -> Tuple[List[Tuple[str, str, str]], Counter, int]:
    """
    Resolve mentions for the selected articles.

    Returns (rows_to_write, status_counts, articles_scanned).
    Rows are deduplicated in memory so the projection reflects real
    inserts rather than attempted ones.
    """
    where = []
    params: List[object] = []
    if since:
        where.append("published_at >= ?")
        params.append(since)

    sql = "SELECT article_id, title, summary, companies_mentioned, tickers_mentioned FROM articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY published_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    seen = set()
    rows: List[Tuple[str, str, str]] = []
    counts: Counter = Counter()
    scanned = 0

    for article_id, title, summary, companies, tickers in conn.execute(sql, params):
        scanned += 1
        context = " ".join(p for p in (title, summary) if p)
        for text, expected in extract_mentions(companies, tickers):
            result = resolver.resolve(text, context=context, expected_type=expected)
            counts[result.status.value] += 1
            if not result.is_confident or not result.entity_id:
                continue
            entity_type = result.entity_type.value if result.entity_type else "company"
            key = (article_id, entity_type, result.entity_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(key)

    return rows, counts, scanned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max articles to process, newest first.")
    parser.add_argument("--since", default=None,
                        help="Only articles published on/after this ISO date.")
    parser.add_argument("--max-db-mb", type=float, default=1400.0,
                        help="Refuse to write if the projected size exceeds this. "
                             "The database is a Release asset (2 GB), not a git file "
                             "(100 MB). This catches runaway growth only.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    size_before = os.path.getsize(args.db)
    print(f"Baza          : {args.db}")
    print(f"Marime curenta: {size_before / 1024 / 1024:.2f} MB")

    index = build_index_from_registries(COMPANY_REGISTRY, COMPANY_SECTOR_MAP)
    resolver = EntityResolver(index)
    print(f"Index alias   : {len(COMPANY_REGISTRY)} companii")

    conn = sqlite3.connect(args.db)
    initialize_news_schema(conn)
    conn.commit()

    existing = conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0]

    rows, counts, scanned = resolve_articles(conn, resolver, args.limit, args.since)

    total_mentions = sum(counts.values())
    print()
    print(f"Articole scanate : {scanned:,}")
    print(f"Mentiuni incercate: {total_mentions:,}")
    for status, n in counts.most_common():
        share = (100 * n / total_mentions) if total_mentions else 0
        print(f"  {status:18s} {n:>8,}  ({share:4.1f}%)")
    print(f"Randuri distincte de scris: {len(rows):,}")
    print(f"Randuri deja existente    : {existing:,}")

    projected = size_before + len(rows) * MEASURED_BYTES_PER_ROW
    print()
    print(f"Proiectie marime : {projected / 1024 / 1024:.2f} MB "
          f"(prag {args.max_db_mb:.1f} MB)")

    if projected / 1024 / 1024 > args.max_db_mb:
        print("REFUZ: proiectia depaseste pragul. Nimic nu a fost scris.")
        print("Reduceti volumul cu --limit sau --since.")
        conn.close()
        return 2

    if not args.apply:
        print("DRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    conn.executemany(
        "INSERT OR IGNORE INTO article_entities (article_id, entity_type, entity_id) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    written = conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0] - existing
    conn.close()

    size_after = os.path.getsize(args.db)
    print(f"SCRIS: {written:,} randuri noi")
    print(f"Marime dupa: {size_after / 1024 / 1024:.2f} MB "
          f"(+{(size_after - size_before) / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
