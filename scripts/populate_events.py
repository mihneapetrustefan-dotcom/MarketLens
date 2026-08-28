"""
Populates `events` (and related Phase 4 tables) from articles that
already have Phase 3 entity links.

PIPELINE THIS SCRIPT CONNECTS
------------------------------
    articles + article_entities (Phase 3, populated by
    backfill_article_entities.py)
        -> EventExtractor.extract_from_article  (Phase 4, deterministic)
        -> dedup against existing events via find_candidate_events /
           find_matching_event (Phase 4's own corroboration hook)
        -> EventRepository.save / add_evidence + link_article

SCOPE, STATED PLAINLY
----------------------
This is Phase 4 only: per-article deterministic extraction. It reuses
Phase 4's own duplicate-detection functions (fingerprint + bounded
candidate window) so that three articles about one earnings report
become one event with three pieces of evidence, not three events —
but it does NOT do Phase 5's fuller fusion (source corroboration
scoring, contradiction handling, the event graph). That distinction
already exists in the code; this script does not blur it.

ONLY ARTICLES WITH AT LEAST ONE PHASE 3 ENTITY ARE CONSIDERED, by
construction: EventExtractor.extract_from_article returns nothing for
an article with no entity_ids (see its docstring — "an event with no
participants is not a financial event"). So this script implicitly
processes the intersection of `articles` and `article_entities`,
currently 23,504 of 51,794 articles.

SAFETY
------
- All schema creation is `CREATE TABLE IF NOT EXISTS`.
- `articles` and `article_entities` are read-only inputs, never
  written.
- Writes go through EventRepository.save, which itself refuses
  (raises) an invalid event rather than storing a broken one.
- --dry-run (the default) resolves and classifies everything and
  reports the outcome without opening a write transaction.
- Re-running is safe: matching candidates get an added evidence
  record + article_event_link rather than a duplicate event.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.news_schema import initialize_news_schema
from src.data_access.event_repository import initialize_event_schema, EventRepository
from src.events.extractor import EventExtractor
from src.events.fingerprint import find_matching_event
from src.domain.event_models import ArticleEventLink, ArticleEventRelation, EventEvidence

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

# Measured on the full eligible corpus (521 new events + participants +
# evidence + links, plus 325 corroborating evidence rows against
# existing events): 1.21 MB total, ~2320 bytes per NEW event including
# its share of shared corroboration overhead. Rounded up for margin.
ESTIMATED_BYTES_PER_EVENT = 2400


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """articles.published_at is stored as an ISO string; fingerprinting
    and candidate-window queries need real datetimes."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def load_articles_with_entities(conn: sqlite3.Connection,
                                limit: Optional[int],
                                since: Optional[str]) -> List[Dict]:
    where = ["a.article_id IN (SELECT DISTINCT article_id FROM article_entities)"]
    params: List[object] = []
    if since:
        where.append("a.published_at >= ?")
        params.append(since)

    sql = f"""
        SELECT a.article_id, a.title, a.summary, a.source, a.published_at
        FROM articles a
        WHERE {' AND '.join(where)}
        ORDER BY a.published_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    out = []
    for article_id, title, summary, source, published_at in rows:
        entity_rows = conn.execute(
            "SELECT entity_id FROM article_entities WHERE article_id = ? AND entity_type = 'company'",
            (article_id,),
        ).fetchall()
        entity_ids = [r[0] for r in entity_rows]
        if not entity_ids:
            continue
        out.append({
            "article_id": article_id,
            "title": title or "",
            "summary": summary or "",
            "source_name": source,
            "published_at": parse_ts(published_at),
            "ingested_at": None,
            "_entity_ids": entity_ids,
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max articles to process, newest first.")
    parser.add_argument("--since", default=None,
                        help="Only articles published on/after this ISO date.")
    parser.add_argument("--min-confidence", type=float, default=0.3,
                        help="Passed to EventExtractor. Events scoring below this are dropped.")
    parser.add_argument("--max-db-mb", type=float, default=96.0,
                        help="Refuse to write if the projected size exceeds this.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    size_before = os.path.getsize(args.db)
    print(f"Baza          : {args.db}")
    print(f"Marime curenta: {size_before / 1024 / 1024:.2f} MB")

    conn = sqlite3.connect(args.db)
    initialize_news_schema(conn)
    initialize_event_schema(conn)
    conn.commit()

    articles = load_articles_with_entities(conn, args.limit, args.since)
    print(f"Articole cu entitati Faza 3: {len(articles):,}")

    extractor = EventExtractor(min_confidence=args.min_confidence)
    repo = EventRepository(conn)

    new_events = []          # StructuredEvent objects to save() fresh
    corroborations = []      # (existing_event_id, evidence, link) to attach
    filtered_irrelevant = 0
    no_classification = 0
    below_confidence = 0
    type_counts: Counter = Counter()

    # Track candidates created THIS run so articles processed in the
    # same pass can corroborate each other, not just events already
    # on disk from a previous run.
    session_events = []

    for article in articles:
        if not extractor.is_potentially_relevant(article):
            filtered_irrelevant += 1
            continue

        candidates = extractor.extract_from_article(
            article,
            entity_ids=article["_entity_ids"],
        )
        if not candidates:
            no_classification += 1
            continue

        for candidate in candidates:
            existing_on_disk = repo.find_candidate_events(candidate) if args.apply else []
            match, reason = find_matching_event(candidate, existing_on_disk + session_events)

            evidence = candidate.evidence[0]
            if match:
                corroborations.append((match.event_id, evidence, reason))
            else:
                new_events.append(candidate)
                session_events.append(candidate)
                type_counts[candidate.event_type.value] += 1

    total_candidates = len(new_events) + len(corroborations)
    print()
    print(f"Articole irelevante (filtru Tier 1): {filtered_irrelevant:,}")
    print(f"Fara clasificare deterministica    : {no_classification:,}")
    print(f"Evenimente noi (candidati)         : {len(new_events):,}")
    print(f"Dovezi corroborante (evenim. existent): {len(corroborations):,}")
    if type_counts:
        print("Pe tip:")
        for t, n in type_counts.most_common():
            print(f"  {t:28s} {n:>6,}")

    projected = size_before + len(new_events) * ESTIMATED_BYTES_PER_EVENT
    print()
    print(f"Proiectie marime : {projected / 1024 / 1024:.2f} MB (prag {args.max_db_mb:.1f} MB)")

    if projected / 1024 / 1024 > args.max_db_mb:
        print("REFUZ: proiectia depaseste pragul. Nimic nu a fost scris.")
        conn.close()
        return 2

    if not args.apply:
        print("DRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    for event in new_events:
        repo.save(event)
        repo.link_article(ArticleEventLink(
            article_id=event.evidence[0].article_id,
            event_id=event.event_id,
            relation=ArticleEventRelation.CREATES,
        ))

    for event_id, evidence, reason in corroborations:
        repo.add_evidence(event_id, evidence)
        repo.link_article(ArticleEventLink(
            article_id=evidence.article_id,
            event_id=event_id,
            relation=ArticleEventRelation.CONFIRMS,
        ))

    conn.close()
    size_after = os.path.getsize(args.db)
    print(f"SCRIS: {len(new_events):,} evenimente noi, {len(corroborations):,} dovezi corroborante")
    print(f"Marime dupa: {size_after / 1024 / 1024:.2f} MB "
          f"(+{(size_after - size_before) / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
