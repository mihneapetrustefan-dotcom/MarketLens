"""
scripts/build_dashboard.py
------------------------------
Manually-triggered rebuild of docs/index.html directly from the
database, independent of run_daily.py's own run.

WHY THIS SCRIPT EXISTS SEPARATELY FROM run_daily.py
-----------------------------------------------------
run_daily.py calls the exact same DashboardGenerator (src/dashboard.py)
with the CURRENT run's in-memory extras attached (live prices, the
daily narrative). This script has none of those — it only has the
database — so it renders the terminal DB-only: every phase that is
actually persisted still appears (recommendations, events, impact,
research, models, signals), and the few things that are only ever
computed in-memory during a live run (live prices, the daily summary)
show their honest "unavailable" state instead.

Used by .github/workflows/rebuild_dashboard.yml to refresh the
published dashboard on demand, without waiting for the next scheduled
run_daily.py pass.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from dashboard import DashboardGenerator  # noqa: E402

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")
DEFAULT_PAGE = os.path.join(REPO_ROOT, "docs", "index.html")
WATCHLIST_PATH = os.path.join(REPO_ROOT, "watchlist.txt")


def _load_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return None
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return names or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_PAGE)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    generator = DashboardGenerator()
    page = generator.generate_report(conn=conn, watchlist=_load_watchlist())
    conn.close()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    generator.save_report(page, args.out)

    print(f"Dashboard generat: {args.out} ({len(page):,} caractere)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
