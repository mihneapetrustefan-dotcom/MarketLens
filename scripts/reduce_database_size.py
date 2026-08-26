#!/usr/bin/env python3
"""
scripts/reduce_database_size.py
------------------------------------
Emergency fix for: "data/marketlens.db is 102.01 MB; this exceeds
GitHub's file size limit of 100.00 MB".

WHAT THIS DOES, IN ORDER:
  1. REPORTS the size of every table in the database — so we know
     exactly what is large BEFORE touching anything.
  2. PRUNES only `raw_articles` (Phase 2's raw provider-payload store)
     down to a retention window. This is safe to prune because:
       - it is a MarketLens Phase 2 addition, not part of the original
         app's schema
       - normalized articles (news_articles) are extracted from it and
         are NOT affected — nothing the UI/pipeline reads is touched
       - its whole purpose (spec §3: "preserve the original provider
         response... for reproducibility") is best served by a rolling
         window, not indefinite retention — recent raw payloads remain
         inspectable, old ones are superseded by their already-extracted
         normalized rows
  3. Runs VACUUM to actually reclaim the freed disk space — SQLite does
     NOT shrink a file automatically after DELETE; without VACUUM the
     file stays exactly as large as before.
  4. REPORTS the size again, so the fix is verifiable, not assumed.

WHAT THIS DELIBERATELY DOES NOT TOUCH: the pre-existing `articles`,
`recommendations`, and `portfolio_snapshots` tables, or any Phase 1/3/4/5
table. If the diagnostic step shows one of THOSE is actually the large
one, this script will say so explicitly rather than guessing at a fix
for the wrong table.

USAGE:
    python scripts/reduce_database_size.py [path/to/marketlens.db] [--keep-days N]

    Defaults to data/marketlens.db and a 14-day raw_articles retention
    window if not specified.
"""

import sys
import os
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def human_mb(byte_count: float) -> str:
    return f"{byte_count / (1024 * 1024):.2f} MB"


def report_table_sizes(conn: sqlite3.Connection) -> dict:
    """
    Report an approximate size per table, using SQLite's own page
    accounting (dbstat) when available, falling back to a rough
    row-count-based estimate otherwise. Printed BEFORE any change is
    made, so the user can see exactly what is large.
    """
    sizes = {}
    try:
        rows = conn.execute("""
            SELECT name, SUM(pgsize) as bytes
            FROM dbstat
            WHERE name NOT LIKE 'sqlite_%'
            GROUP BY name
            ORDER BY bytes DESC
        """).fetchall()
        for name, byte_count in rows:
            sizes[name] = byte_count or 0
    except sqlite3.OperationalError:
        # dbstat virtual table not compiled in on this SQLite build —
        # fall back to row counts, which are still informative even
        # without exact byte sizes.
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            sizes[name] = count   # row count, not bytes, in this fallback path

    print("\nDimensiune pe tabel (aproximativ):")
    for name, value in sorted(sizes.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {name:30s} {human_mb(value) if value > 1000 else f'{value} randuri (dbstat indisponibil)'}")
    return sizes


def prune_raw_articles(conn: sqlite3.Connection, keep_days: int) -> int:
    """
    Delete raw_articles rows older than `keep_days`, keyed on
    fetched_at. Returns the number of rows deleted. A no-op (returns 0)
    if the table does not exist — this script must not fail on a
    database that never had Phase 2 installed.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='raw_articles'"
    ).fetchone()
    if not exists:
        print("Tabelul 'raw_articles' nu exista in aceasta baza de date — nimic de curatat aici.")
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    before = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()[0]
    conn.execute("DELETE FROM raw_articles WHERE fetched_at < ?", (cutoff,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()[0]
    deleted = before - after
    print(f"raw_articles: {before} randuri -> {after} randuri (sterse {deleted}, pastrate ultimele {keep_days} zile)")
    return deleted


def vacuum(conn: sqlite3.Connection) -> None:
    """
    Physically reclaim the space freed by DELETE. SQLite marks deleted
    rows' pages as free but does NOT shrink the file until VACUUM runs
    — skipping this step would leave the file exactly as large as
    before despite the deletions above.
    """
    print("\nRulez VACUUM (poate dura cateva secunde)...")
    conn.execute("VACUUM")
    print("VACUUM complet.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose and reduce MarketLens database file size.")
    parser.add_argument("db_path", nargs="?", default=os.path.join(REPO_ROOT, "data", "marketlens.db"))
    parser.add_argument("--keep-days", type=int, default=14,
                         help="Days of raw_articles payloads to retain (default: 14).")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"EROARE: nu gasesc baza de date la {args.db_path}")
        return 1

    size_before = os.path.getsize(args.db_path)
    print(f"Dimensiune fisier INAINTE: {human_mb(size_before)}")

    conn = sqlite3.connect(args.db_path)
    report_table_sizes(conn)

    prune_raw_articles(conn, args.keep_days)
    vacuum(conn)
    conn.close()

    size_after = os.path.getsize(args.db_path)
    print(f"\nDimensiune fisier DUPA: {human_mb(size_after)}")
    print(f"Redus cu: {human_mb(size_before - size_after)}")

    if size_after > 95 * 1024 * 1024:
        print("\nATENTIE: fisierul e in continuare aproape de limita GitHub de 100 MB.")
        print("Verifica raportul de mai sus — daca alt tabel (nu raw_articles) e mare,")
        print("acela trebuie tratat separat, cu grija (nu e curatat automat de acest script).")
        return 2

    print("\nOK — fisierul e acum sub limita GitHub. Poti relua rularile normale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
