#!/usr/bin/env python3
"""
scripts/archive_old_articles.py
------------------------------------
Shrinks the live `articles` table by ARCHIVING old rows to compressed
files, rather than deleting them outright.

WHY ARCHIVE INSTEAD OF DELETE: this project's Phase 7 (Historical
Event Studies & Research Dataset) needs exactly the old articles this
script would otherwise remove. Silently deleting them to satisfy
GitHub's 100 MB limit would quietly sabotage that later goal. Archiving
keeps every row, in a form that:
  - is 5-10x smaller than the live SQLite table (gzip-compressed
    newline-delimited JSON, no indexes, no page overhead)
  - is chunked by month, so no single archive file risks the same
    100 MB problem
  - remains directly loadable for research later (one JSON object per
    line — trivial to read back with a for-loop and json.loads)

SCHEMA-AGNOSTIC BY DESIGN: this script does not assume the exact
column layout of the pre-existing `articles` table (created by
news_database.py, which this environment does not have direct access
to for inspection). It discovers the actual columns at runtime and
looks for a plausible timestamp column from a candidate list, so it
adapts to the real schema rather than guessing at it blindly.

SAFETY: if no recognizable timestamp column is found, the script
reports the schema and STOPS without touching any data — the same
"never guess, never silently corrupt" policy as reduce_database_size.py.

USAGE:
    python scripts/archive_old_articles.py [path/to/marketlens.db] [--keep-days N]

    Defaults to data/marketlens.db and a 180-day retention window
    (roughly 6 months of articles stay live and instantly queryable;
    everything older moves to the compressed archive).
"""

import sys
import os
import sqlite3
import json
import gzip
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "archives")

#: Column names checked, in order of preference, to find the article's
#: publication timestamp. The first one actually present in the real
#: table is used — this is what makes the script schema-agnostic
#: rather than hardcoded to one exact layout.
TIMESTAMP_CANDIDATES = ["published_at", "collected_at", "created_at", "ingested_at", "date", "timestamp"]


def human_mb(byte_count: float) -> str:
    return f"{byte_count / (1024 * 1024):.2f} MB"


def find_timestamp_column(conn: sqlite3.Connection, table: str):
    """Return the first candidate column name actually present in `table`, or None."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for candidate in TIMESTAMP_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def parse_timestamp(value):
    """Best-effort ISO parse. Returns None (never raises) for anything unparseable — an unparseable row is simply not archived."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def archive_old_articles(conn: sqlite3.Connection, keep_days: int) -> int:
    """
    Move `articles` rows older than `keep_days` into monthly
    gzip-compressed JSONL files under data/archives/, then delete them
    from the live table.

    Returns the number of rows archived. Returns 0 (untouched) if the
    `articles` table or a usable timestamp column cannot be found —
    this must never guess its way into deleting the wrong thing.
    """
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='articles'").fetchone()
    if not exists:
        print("Tabelul 'articles' nu exista in aceasta baza de date — nimic de arhivat.")
        return 0

    timestamp_column = find_timestamp_column(conn, "articles")
    if timestamp_column is None:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(articles)")]
        print("NU am gasit o coloana de timp recunoscuta in 'articles'.")
        print(f"Coloanele reale sunt: {columns}")
        print("Nu arhivez nimic — spune-mi exact numele coloanei de data, ca sa ajustez scriptul corect.")
        return 0

    print(f"Folosesc coloana de timp: '{timestamp_column}'")

    conn.row_factory = sqlite3.Row
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    rows = conn.execute("SELECT * FROM articles").fetchall()

    by_month = defaultdict(list)
    ids_to_delete = []
    id_column = "id" if "id" in rows[0].keys() else None if rows else None

    for row in rows:
        moment = parse_timestamp(row[timestamp_column])
        if moment is None or moment >= cutoff:
            continue   # too recent, or unparseable — never archive/delete on an unparseable date
        record = dict(row)
        record[timestamp_column] = row[timestamp_column]  # keep original string form in the archive
        by_month[month_key(moment)].append(record)
        if id_column:
            ids_to_delete.append(row[id_column])

    if not by_month:
        print(f"Niciun articol mai vechi de {keep_days} zile — nimic de arhivat.")
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    total_archived = 0
    for month, records in sorted(by_month.items()):
        archive_path = os.path.join(ARCHIVE_DIR, f"articles_{month}.jsonl.gz")
        # Append mode: if this month's archive already exists from a
        # previous run, add to it rather than overwriting — repeated
        # runs must never lose a prior archive.
        with gzip.open(archive_path, "at", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  arhivate {len(records)} articole din {month} -> {os.path.relpath(archive_path, REPO_ROOT)} "
              f"({human_mb(os.path.getsize(archive_path))})")
        total_archived += len(records)

    if id_column and ids_to_delete:
        placeholders = ",".join("?" * len(ids_to_delete))
        conn.execute(f"DELETE FROM articles WHERE {id_column} IN ({placeholders})", ids_to_delete)
        conn.commit()

    print(f"\nTotal arhivate si sterse din tabelul live: {total_archived} articole (pastrate ultimele {keep_days} zile)")
    return total_archived


def vacuum(conn: sqlite3.Connection) -> None:
    print("\nRulez VACUUM (poate dura cateva secunde)...")
    conn.execute("VACUUM")
    print("VACUUM complet.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive old MarketLens articles to compressed files, shrinking the live database.")
    parser.add_argument("db_path", nargs="?", default=os.path.join(REPO_ROOT, "data", "marketlens.db"))
    parser.add_argument("--keep-days", type=int, default=180,
                         help="Days of articles to keep live/queryable (default: 180). Older articles are archived, not deleted.")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"EROARE: nu gasesc baza de date la {args.db_path}")
        return 1

    size_before = os.path.getsize(args.db_path)
    print(f"Dimensiune fisier INAINTE: {human_mb(size_before)}")

    conn = sqlite3.connect(args.db_path)
    archived = archive_old_articles(conn, args.keep_days)
    if archived:
        vacuum(conn)
    conn.close()

    size_after = os.path.getsize(args.db_path)
    print(f"\nDimensiune fisier DUPA: {human_mb(size_after)}")
    print(f"Redus cu: {human_mb(size_before - size_after)}")

    if size_after > 95 * 1024 * 1024:
        print("\nATENTIE: fisierul e in continuare aproape de limita GitHub de 100 MB.")
        return 2

    print("\nOK — istoricul e pastrat integral, in arhive comprimate sub data/archives/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
