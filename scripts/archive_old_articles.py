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

#: Default only. The real directory is DERIVED FROM THE DATABASE PATH
#: (see `archive_dir_for`), because this script deletes rows and the
#: standard way to test something that deletes is to run it against a
#: copy first.
#:
#: TD-15: it used to be this constant unconditionally. A rehearsal
#: against a throwaway copy during the TD-02 migration appended to the
#: live articles_2026-07.jsonl.gz -- the script accepted a --db
#: argument and then ignored it when writing. Nothing was lost, but a
#: destructive script that cannot be rehearsed safely is one that will
#: eventually be run unrehearsed.
ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "archives")


def archive_dir_for(db_path: str) -> str:
    """
    Where the archives for THIS database belong.

    Sibling of the database file: data/marketlens.db -> data/archives,
    which is exactly where production already writes, so this changes
    nothing for the scheduled run. A copy at /tmp/x/marketlens.db
    archives to /tmp/x/archives instead of into the repository.
    """
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "archives")

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


def archive_old_articles(conn: sqlite3.Connection, keep_days: int,
                         archive_dir: str = ARCHIVE_DIR) -> int:
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
    # BUG REPARAT: aceasta linie cauta o coloana numita exact "id".
    # Tabelul `articles` are cheia primara `article_id`, deci
    # id_column ramanea None si DELETE-ul de mai jos nu rula NICIODATA
    # — desi scriptul tiparea "arhivate si sterse". Efectul masurat:
    # articolele ramaneau in tabelul live si erau re-arhivate la
    # fiecare rulare (arhiva se deschide in mod append), ajungand la
    # 7 copii ale aceluiasi articol; 18.986 de linii in arhiva iunie
    # 2026 pentru doar 3.105 articole reale.
    #
    # Se detecteaza acum cheia reala, in ordinea probabilitatii, in
    # loc sa se presupuna un singur nume.
    available = set(rows[0].keys()) if rows else set()
    id_column = next((name for name in ("article_id", "id", "rowid") if name in available), None)
    if rows and id_column is None:
        print("NU am gasit o coloana de identificare in 'articles'.")
        print(f"Coloanele reale sunt: {sorted(available)}")
        print("Nu sterg nimic — arhivarea fara stergere ar duplica arhivele la fiecare rulare.")
        return 0

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

    os.makedirs(archive_dir, exist_ok=True)
    total_archived = 0
    for month, records in sorted(by_month.items()):
        archive_path = os.path.join(archive_dir, f"articles_{month}.jsonl.gz")
        # Append mode: if this month's archive already exists from a
        # previous run, add to it rather than overwriting — repeated
        # runs must never lose a prior archive.
        with gzip.open(archive_path, "at", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  arhivate {len(records)} articole din {month} -> {archive_path} "
              f"({human_mb(os.path.getsize(archive_path))})")
        total_archived += len(records)

    deleted = 0
    if id_column and ids_to_delete:
        placeholders = ",".join("?" * len(ids_to_delete))
        conn.execute(f"DELETE FROM articles WHERE {id_column} IN ({placeholders})", ids_to_delete)
        conn.commit()
        deleted = len(ids_to_delete)

    # Raporteaza ce s-a intamplat REAL. Versiunea anterioara spunea
    # mereu "arhivate si sterse", chiar si cand nu se stergea nimic.
    print(f"\nTotal arhivate: {total_archived} articole "
          f"(pastrate ultimele {keep_days} zile)")
    print(f"Total sterse din tabelul live: {deleted}")
    if total_archived and not deleted:
        print("ATENTIE: s-a arhivat fara sa se stearga nimic — la urmatoarea rulare "
              "aceleasi articole vor fi re-arhivate. Verifica coloana de identificare.")
    return total_archived


def vacuum(conn: sqlite3.Connection) -> None:
    print("\nRulez VACUUM (poate dura cateva secunde)...")
    conn.execute("VACUUM")
    print("VACUUM complet.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive old MarketLens articles to compressed files, shrinking the live database.")
    parser.add_argument("db_path", nargs="?", default=os.path.join(REPO_ROOT, "data", "marketlens.db"))
    parser.add_argument("--archive-dir", default=None,
                        help="Where to write the .jsonl.gz archives. "
                             "Default: an 'archives' directory beside the "
                             "database, so running against a copy cannot "
                             "touch the real archive (TD-15).")
    parser.add_argument("--keep-days", type=int, default=180,
                         help="Days of articles to keep live/queryable (default: 180). Older articles are archived, not deleted.")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"EROARE: nu gasesc baza de date la {args.db_path}")
        return 1

    size_before = os.path.getsize(args.db_path)
    print(f"Dimensiune fisier INAINTE: {human_mb(size_before)}")

    conn = sqlite3.connect(args.db_path)
    archive_dir = args.archive_dir or archive_dir_for(args.db_path)
    print(f"Arhive        : {archive_dir}")
    archived = archive_old_articles(conn, args.keep_days, archive_dir)
    if archived:
        vacuum(conn)
    conn.close()

    size_after = os.path.getsize(args.db_path)
    print(f"\nDimensiune fisier DUPA: {human_mb(size_after)}")
    print(f"Redus cu: {human_mb(size_before - size_after)}")

    # Pragul de 95 MB (limita git de 100 MB) a devenit invalid din
    # momentul migrarii bazei intr-un GitHub Release asset (plafon
    # 2 GB), pe 28 august 2026. Pastrat neactualizat, acest prag oprea
    # pipeline-ul zilnic cu exit code 2 in fiecare zi, exact contrariul
    # motivului migrarii. Prag nou: 1800 MB, cu marja sub 2 GB. Peste
    # el, un AVERTISMENT — nu o eroare care opreste pipeline-ul; doar
    # pragul dur de 2 GB al Release-ului justifica o oprire reala.
    if size_after > 1800 * 1024 * 1024:
        print("\nATENTIE: fisierul se apropie de limita de 2 GB a GitHub Release asset-ului.")
        print("Ramane totusi doar un avertisment — pipeline-ul continua normal.")

    print(f"\nOK — istoricul e pastrat integral, in arhive comprimate sub {archive_dir}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
