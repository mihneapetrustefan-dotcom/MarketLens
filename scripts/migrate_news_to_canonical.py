#!/usr/bin/env python3
"""
scripts/migrate_news_to_canonical.py
------------------------------------------
Backfill `news_articles` from `articles` (Phase 17, TD-02).

WHY THIS EXISTS
-------------------
The Phase 17 audit found two competing definitions of "an article":

    articles        16 columns, 48,392 rows, ticker strings, populated
    news_articles   27 columns, 0 rows, provider + provenance + dedup
                    levels + processing state, EMPTY

The better schema was the empty one. Worse, the Phase 2 domain model
was written *expecting* this migration — `NormalizedArticle` carries
`sentiment_label`, `sentiment_score` and `impact_score` with a comment
saying they exist so migrated articles keep the values the existing
engines already produced. The design anticipated the migration; nobody
ran it.

WHAT THIS DOES
------------------
Reads every row of `articles`, maps it onto `NormalizedArticle`, and
writes it to `news_articles` through the canonical repository — the
same INSERT the ingestion path uses, so there is one definition of an
article row rather than two.

WHAT IT DELIBERATELY DOES NOT DO
------------------------------------
It does not touch `articles`. Not one UPDATE, not one DELETE, no DROP.
`articles` holds the only copy of 48,392 rows and seven modules still
read it. Retiring it is a separate decision, taken after its readers
are repointed, and it must not be smuggled into a backfill.

It does not invent data. Where the legacy row has no equivalent —
language, country, author, provider article id — the canonical column
is left NULL rather than guessed. A migration that fills gaps with
plausible values produces a table that looks complete and is not.

IDEMPOTENT
--------------
`INSERT OR REPLACE` on a stable `article_id`, which is carried across
unchanged. Running it twice writes the same rows twice and changes
nothing — and `article_entities` (26,400 rows, keyed on `article_id`)
stays valid precisely because the id does not change.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data_access.news_repository import NewsRepository
from src.data_access.news_schema import initialize_news_schema
from src.domain.news_models import (
    DuplicateMatchLevel, NormalizedArticle, ProcessingStatus,
)

DEFAULT_DB = os.path.join("data", "marketlens.db")
BATCH = 2000

#: The legacy pipeline collected from these three, and recorded none of
#: them per row. The provider is therefore unknown for migrated rows,
#: and is recorded as exactly that rather than guessed at from the URL.
LEGACY_PROVIDER = "legacy"


def line(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 68 - len(title)))


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json_field(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def source_index(conn: sqlite3.Connection) -> Dict[str, str]:
    """
    Map a source NAME to a canonical `news_sources.source_id`.

    Only 13 of ~398 distinct legacy source names have a canonical row.
    The rest resolve to None, and `source_name` carries the string —
    which is what the domain model says to do, in a comment explaining
    that existing code joins on the name.
    """
    return {name: sid for sid, name
            in conn.execute("SELECT source_id, name FROM news_sources")}


def canonical_of_group(conn: sqlite3.Connection) -> Dict[str, str]:
    """
    Choose one canonical article per legacy duplicate group.

    The legacy model records a `duplicate_group_id` and a size; the
    canonical model records `duplicate_of` pointing at the original.
    Converting between them needs a rule for which member is the
    original, and the rule is the earliest publication time, falling
    back to the earliest time we stored it.

    Groups of one — 41,452 of 48,392 — have no duplicate at all, and
    their members are left with `duplicate_of = NULL`.
    """
    winners: Dict[str, str] = {}
    for group_id, article_id in conn.execute("""
        SELECT duplicate_group_id, article_id
        FROM articles
        WHERE duplicate_group_id IS NOT NULL AND duplicate_group_size > 1
        ORDER BY duplicate_group_id,
                 COALESCE(published_at, stored_at) ASC,
                 article_id ASC
    """):
        winners.setdefault(group_id, article_id)
    return winners


def to_normalized(row: sqlite3.Row, sources: Dict[str, str],
                  group_winner: Dict[str, str]) -> NormalizedArticle:
    """One legacy row → one canonical article. No field is invented."""
    sentiment = _json_field(row["sentiment"], {}) or {}
    impact = _json_field(row["impact"], {}) or {}

    group_id = row["duplicate_group_id"]
    size = row["duplicate_group_size"] or 1
    duplicate_of = None
    match_level = DuplicateMatchLevel.NONE
    if group_id and size > 1:
        winner = group_winner.get(group_id)
        if winner and winner != row["article_id"]:
            duplicate_of = winner
            # The legacy deduplicator matched on normalised title and
            # source within a time window. That is Level 3, and saying
            # so keeps the decision auditable rather than a black box.
            match_level = DuplicateMatchLevel.TITLE_SOURCE_TIME

    category = row["category"]
    published = _parse_iso(row["published_at"])
    ingested = _parse_iso(row["collected_at"]) or _parse_iso(row["stored_at"])

    return NormalizedArticle(
        article_id=row["article_id"],
        provider=LEGACY_PROVIDER,
        provider_article_id=None,
        raw_id=None,
        source_id=sources.get(row["source"] or ""),
        source_name=row["source"],
        source_url=row["url"],
        canonical_url=row["url"],
        title=row["title"] or "",
        summary=row["summary"] or None,
        language=None,
        country=None,
        author=None,
        categories=[category] if category else [],
        published_at=published,
        ingested_at=ingested,
        updated_at=None,
        fingerprint=None,
        content_fingerprint=None,
        duplicate_of=duplicate_of,
        duplicate_match_level=match_level,
        sentiment_label=sentiment.get("label"),
        sentiment_score=_decimal(sentiment.get("score")),
        impact_score=_decimal(impact.get("score")),
        # These rows went through cleaning, deduplication, entity
        # detection and scoring in the legacy pipeline. READY is the
        # honest status: they are not freshly ingested.
        processing_status=ProcessingStatus.READY,
        rejection_reason=None,
        version=1,
    )


def migrate(conn: sqlite3.Connection, limit: Optional[int],
            dry_run: bool, quiet: bool = False) -> Dict[str, int]:
    say = (lambda *a, **k: None) if quiet else print
    repository = NewsRepository(conn)
    sources = source_index(conn)
    winners = canonical_of_group(conn)

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    say(f"  legacy articles       {total:,}")
    say(f"  canonical sources     {len(sources)}")
    say(f"  duplicate groups >1   {len(winners):,}")

    sql = "SELECT * FROM articles ORDER BY stored_at ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    stats = {"read": 0, "written": 0, "skipped_no_title": 0, "duplicates": 0}
    batch: List[NormalizedArticle] = []

    for row in conn.execute(sql):
        stats["read"] += 1
        if not (row["title"] or "").strip():
            # `news_articles.title` is NOT NULL. A titleless legacy row
            # is skipped and counted rather than written as an empty
            # string, which would look like a real article.
            stats["skipped_no_title"] += 1
            continue

        article = to_normalized(row, sources, winners)
        if article.duplicate_of:
            stats["duplicates"] += 1
        batch.append(article)

        if len(batch) >= BATCH:
            if not dry_run:
                stats["written"] += repository.bulk_write(batch)
            else:
                stats["written"] += len(batch)
            batch = []
            print(f"    {stats['written']:>7,} / {total:,}", end="\r")

    if batch:
        if not dry_run:
            stats["written"] += repository.bulk_write(batch)
        else:
            stats["written"] += len(batch)

    print(" " * 40, end="\r")
    return stats


def verify(conn: sqlite3.Connection, quiet: bool = False) -> bool:
    """
    Check the migration against the source, and report rather than
    assume. Every number here is recomputed from both tables.
    """
    legacy = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE TRIM(COALESCE(title,'')) != ''"
    ).fetchone()[0]
    canonical = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]

    missing = conn.execute("""
        SELECT COUNT(*) FROM articles a
        LEFT JOIN news_articles n ON a.article_id = n.article_id
        WHERE n.article_id IS NULL AND TRIM(COALESCE(a.title,'')) != ''
    """).fetchone()[0]

    orphan_dupes = conn.execute("""
        SELECT COUNT(*) FROM news_articles n
        WHERE n.duplicate_of IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM news_articles o
                          WHERE o.article_id = n.duplicate_of)
    """).fetchone()[0]

    entity_links = conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0]
    linked_ok = conn.execute("""
        SELECT COUNT(*) FROM article_entities e
        JOIN news_articles n ON e.article_id = n.article_id
    """).fetchone()[0]

    with_sentiment = conn.execute(
        "SELECT COUNT(*) FROM news_articles WHERE sentiment_label IS NOT NULL"
    ).fetchone()[0]

    say = (lambda *a, **k: None) if quiet else print
    if not quiet:
        line("VERIFICATION")
    say(f"  legacy rows with a title      {legacy:,}")
    say(f"  canonical rows                {canonical:,}")
    say(f"  legacy rows NOT migrated      {missing:,}")
    say(f"  duplicate_of pointing nowhere {orphan_dupes:,}")
    say(f"  sentiment carried across      {with_sentiment:,}")
    say(f"  entity links still resolvable {linked_ok:,} / {entity_links:,}")

    ok = (missing == 0 and orphan_dupes == 0 and linked_ok == entity_links)
    say()
    if ok:
        say("  VERIFIED. Every legacy article with a title has a canonical")
        say("  row, every duplicate points at a row that exists, and every")
        say("  Phase 5 entity link still resolves.")
    else:
        say("  FAILED. See the non-zero counts above.")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill news_articles from articles (TD-02). "
                    "Never modifies the articles table.")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int,
                        help="migrate only the first N rows (for a trial run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="map every row and write nothing")
    parser.add_argument("--verify-only", action="store_true",
                        help="check a previous migration without writing")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No database at {args.db}.")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print("MarketLens - migrate news to the canonical schema (TD-02)")
    print("SOURCE: articles        (read only, never modified)")
    print("TARGET: news_articles   (INSERT OR REPLACE, idempotent)")
    if args.dry_run:
        print("MODE:   DRY RUN - nothing is written")
    print("=" * 72)

    initialize_news_schema(conn)

    if not args.verify_only:
        line("MIGRATION")
        stats = migrate(conn, args.limit, args.dry_run)
        print(f"  read                  {stats['read']:,}")
        print(f"  written               {stats['written']:,}")
        print(f"  skipped (no title)    {stats['skipped_no_title']:,}")
        print(f"  marked duplicate      {stats['duplicates']:,}")

    if args.dry_run:
        print("\n  Dry run: no verification, because nothing was written.")
        return 0

    ok = verify(conn)

    line("WHAT THIS DID NOT DO")
    print("  The articles table was not modified. No UPDATE, no DELETE,")
    print("  no DROP. It remains the only copy of the legacy rows and")
    print("  seven modules still read it; retiring it is a separate")
    print("  decision, taken after those readers are repointed.")

    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
