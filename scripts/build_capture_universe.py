#!/usr/bin/env python3
"""
scripts/build_capture_universe.py
-----------------------------------------------------------
Phase 25.9G — write a versioned capture universe from repository data.

    python scripts/build_capture_universe.py --db <snapshot.db> --version v1

THE RULE, STATED SO IT CAN BE CHECKED
-----------------------------------------
For every sector: the two US-listed stocks (`asset_class = 'stock'`,
exchange `US_AND_INTL`) with the MOST daily candles in the research
cache, ties broken alphabetically by ticker. Plus the SPY benchmark.

WHAT THE RULE DELIBERATELY IGNORES
--------------------------------------
Returns, signals, model output, label values -- anything that could make
the universe a choice about which instruments make a model look good
(§64). Daily-candle coverage is a data-availability property: it favours
names the project already follows, which is what a capture pilot should
start from, and it says nothing about how they will perform.

Sector breadth is what makes cross-sectional minutes measurable: two
names per sector across fifteen sectors puts roughly thirty instruments
on every minute boundary, where the event-window corpus managed three
only by coincidence.

READ-ONLY. The database is opened `mode=ro`. The output is a JSON file,
which is committed; a new rule or a new membership is a NEW version file,
never an edit of an old one (§18).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")

PER_SECTOR = 2
BENCHMARK = {"instrument_id": "benchmark-spy", "ticker": "SPY",
             "sector_id": "benchmark", "asset_class": "etf",
             "sec_type": "STK", "currency": "USD", "role": "benchmark"}

RULE = ("top %d US-listed stocks per sector by daily-candle count in the "
        "research cache, ties broken by ticker; plus the SPY benchmark"
        % PER_SECTOR)


def select(conn: sqlite3.Connection, per_sector: int = PER_SECTOR):
    rows = conn.execute("""
        SELECT i.instrument_id, i.ticker, co.sector_id,
               COUNT(p.timestamp) AS daily_candles
          FROM instruments i
          JOIN securities s  ON s.security_id = i.security_id
          JOIN companies co  ON co.company_id = s.company_id
          LEFT JOIN price_candle_cache p
                 ON p.instrument_id = i.instrument_id AND p.interval = '1d'
         WHERE i.asset_class = 'stock' AND i.exchange_id = 'US_AND_INTL'
         GROUP BY i.instrument_id
    """).fetchall()
    by_sector = {}
    for instrument_id, ticker, sector_id, candles in rows:
        by_sector.setdefault(sector_id or "unassigned", []).append(
            (-(candles or 0), ticker, instrument_id))
    members = []
    for sector_id in sorted(by_sector):
        for neg, ticker, instrument_id in sorted(by_sector[sector_id])[:per_sector]:
            members.append({"instrument_id": instrument_id, "ticker": ticker,
                            "sector_id": sector_id, "asset_class": "stock",
                            "sec_type": "STK", "currency": "USD",
                            "role": "member", "daily_candles_at_build": -neg})
    return members


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    out = args.out or os.path.join(CONFIG_DIR, "capture_universe_%s.json" % args.version)
    if os.path.exists(out):
        print("REFUSED: %s exists. A universe version is immutable; write a "
              "new version instead." % out)
        return 2

    uri = "file:%s?mode=ro" % os.path.abspath(args.db).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    members = [BENCHMARK] + select(conn)
    source_hash = hashlib.sha256(
        json.dumps(members, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    definition = {
        "version": args.version,
        "purpose": "Phase 25.9G continuous intraday capture pilot",
        "rule": RULE,
        "excludes": "returns, signals, model output and labels",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "built_from_db_sha256_prefix": source_hash,
        "instruments": members,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(definition, handle, indent=2, sort_keys=False)
        handle.write("\n")
    print("wrote %s: %d instruments (%d sectors + benchmark)"
          % (out, len(members), len({m["sector_id"] for m in members}) - 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
