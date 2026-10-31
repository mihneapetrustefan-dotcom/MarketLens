#!/usr/bin/env python3
"""
scripts/build_parent_universe.py
-----------------------------------------------------------
Phase 25.9I-B — write the versioned PARENT universe from repository data.

    python scripts/build_parent_universe.py --db data/marketlens.db \
        --version v1 --as-of 2026-08-27

WHAT A PARENT UNIVERSE IS, AND WHAT IT IS NOT
-------------------------------------------------
It is the set of instruments the system is ALLOWED to evaluate. It is
not a watchlist, not a portfolio, and not a claim that any member is
worth trading today. Session selection happens later and separately;
most members will be absent from most sessions.

THE RULE, STATED SO IT CAN BE CHECKED
-----------------------------------------
As of a stated date, a US-listed stock (`asset_class = 'stock'`,
exchange `US_AND_INTL`) is a member when:

  * it has at least `--min-candles` daily candles at or before `as_of`
    in the research cache, and
  * volume is present on at least 90% of them,

ranked within its sector by MEDIAN DAILY DOLLAR VOLUME over the trailing
`--liquidity-window` candles, keeping at most `--per-sector` names.

WHAT THE RULE DELIBERATELY IGNORES
--------------------------------------
Returns, signals, model output, labels, and every outcome-derived
quantity -- the same exclusions `build_capture_universe.py` states, and
for the same reason (§64): a universe chosen by what performed well is
a universe that makes any later model look good. Daily-candle coverage
and dollar volume are availability and liquidity properties. They say
which instruments the project can actually observe and trade, and
nothing about how they will behave.

AS-OF SAFETY
----------------
Every query is bounded by `as_of`. A universe built as of 2026-06-30
cannot see July's candles, so a historical replay selects from the
membership that was defensible then rather than the one that is
convenient now.

BENCHMARKS ARE NOT CANDIDATES
---------------------------------
`benchmark-spy` is absent from this project's instrument master
entirely (`portfolio/exposure.py` says so; `capture/universe.py` skips
`benchmark-*` by name; `features/intraday.py` computes market context
"on the benchmark, never on self"). It is carried in a separate
`benchmarks` list, and it never consumes session capacity.

READ-ONLY. The database is opened `mode=ro`. The output is a JSON file:
a new rule or a new membership is a NEW version, never an edit of an
old one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")

DEFAULT_MIN_CANDLES = 150
DEFAULT_PER_SECTOR = 10
DEFAULT_LIQUIDITY_WINDOW = 60
MIN_VOLUME_COVERAGE = 0.90

#: Reference instruments: available for market-context features, never
#: candidates for a session slot.
BENCHMARKS = [{"instrument_id": "benchmark-spy", "ticker": "SPY",
               "asset_class": "etf", "sec_type": "STK", "currency": "USD",
               "role": "benchmark",
               "note": "market context only; absent from the instrument master"}]

RULE_TEMPLATE = (
    "US-listed stocks (asset_class='stock', exchange='US_AND_INTL') with at "
    "least %d daily candles at or before as_of and volume present on at least "
    "%d%% of them, ranked within sector by median daily dollar volume over the "
    "trailing %d candles, at most %d per sector")


def candidates(conn: sqlite3.Connection, as_of: str, min_candles: int,
               liquidity_window: int):
    """Every member that qualifies, with the numbers behind the decision."""
    rows = conn.execute("""
        SELECT i.instrument_id, i.ticker, co.sector_id, se.currency
          FROM instruments i
          JOIN securities se ON se.security_id = i.security_id
          JOIN companies co  ON co.company_id = se.company_id
         WHERE i.asset_class = 'stock' AND i.exchange_id = 'US_AND_INTL'
      ORDER BY i.instrument_id
    """).fetchall()

    members, excluded = [], []
    for instrument_id, ticker, sector_id, currency in rows:
        candles = conn.execute("""
            SELECT close, volume FROM price_candle_cache
             WHERE instrument_id = ? AND interval = '1d' AND timestamp <= ?
          ORDER BY timestamp DESC LIMIT ?
        """, (instrument_id, as_of, liquidity_window)).fetchall()
        total = conn.execute("""
            SELECT COUNT(*), SUM(volume IS NOT NULL) FROM price_candle_cache
             WHERE instrument_id = ? AND interval = '1d' AND timestamp <= ?
        """, (instrument_id, as_of)).fetchone()
        count, with_volume = total[0] or 0, total[1] or 0

        if count < min_candles:
            excluded.append((instrument_id, ticker,
                             "only %d daily candles at as_of (need %d)"
                             % (count, min_candles)))
            continue
        if count and with_volume / count < MIN_VOLUME_COVERAGE:
            excluded.append((instrument_id, ticker,
                             "volume present on %.0f%% of candles (need %.0f%%)"
                             % (100 * with_volume / count,
                                100 * MIN_VOLUME_COVERAGE)))
            continue
        if not sector_id:
            excluded.append((instrument_id, ticker, "no sector metadata"))
            continue
        dollar_volumes = [c * v for c, v in candles
                          if c is not None and v is not None]
        if not dollar_volumes:
            excluded.append((instrument_id, ticker,
                             "no priced volume in the liquidity window"))
            continue

        members.append({
            "instrument_id": instrument_id, "ticker": ticker,
            "sector_id": sector_id, "asset_class": "stock", "sec_type": "STK",
            "currency": currency or "USD", "role": "candidate",
            "daily_candles_at_as_of": count,
            "median_dollar_volume": round(statistics.median(dollar_volumes), 2),
            "liquidity_window_candles": len(dollar_volumes),
            "inclusion_reason": "coverage and liquidity at as_of",
        })
    return members, excluded


def apply_sector_cap(members, per_sector: int):
    """At most `per_sector` per sector, by liquidity, ties broken by ticker."""
    by_sector = {}
    for member in members:
        by_sector.setdefault(member["sector_id"], []).append(member)
    kept, dropped = [], []
    for sector_id in sorted(by_sector):
        ranked = sorted(by_sector[sector_id],
                        key=lambda m: (-m["median_dollar_volume"], m["ticker"]))
        for rank, member in enumerate(ranked, start=1):
            member["sector_rank"] = rank
            if rank <= per_sector:
                kept.append(member)
            else:
                dropped.append((member["instrument_id"], member["ticker"],
                                "rank %d in %s, above the per-sector cap of %d"
                                % (rank, sector_id, per_sector)))
    #: Deterministic order: sector, then liquidity rank, then ticker.
    kept.sort(key=lambda m: (m["sector_id"], m["sector_rank"], m["ticker"]))
    return kept, dropped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--as-of", required=True,
                        help="ISO date; only candles at or before this are read")
    parser.add_argument("--min-candles", type=int, default=DEFAULT_MIN_CANDLES)
    parser.add_argument("--per-sector", type=int, default=DEFAULT_PER_SECTOR)
    parser.add_argument("--liquidity-window", type=int,
                        default=DEFAULT_LIQUIDITY_WINDOW)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    out = args.out or os.path.join(CONFIG_DIR,
                                   "parent_universe_%s.json" % args.version)
    if os.path.exists(out):
        print("REFUSED: %s exists. A universe version is immutable; write a "
              "new version instead." % out)
        return 2

    as_of = args.as_of if "T" in args.as_of else args.as_of + "T23:59:59+00:00"
    uri = "file:%s?mode=ro" % os.path.abspath(args.db).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        qualified, excluded = candidates(conn, as_of, args.min_candles,
                                         args.liquidity_window)
        members, capped = apply_sector_cap(qualified, args.per_sector)
        newest = conn.execute(
            "SELECT MAX(timestamp) FROM price_candle_cache WHERE interval = '1d' "
            "AND timestamp <= ?", (as_of,)).fetchone()[0]
    finally:
        conn.close()

    payload = json.dumps(members, sort_keys=True).encode("utf-8")
    definition = {
        "version": args.version,
        "purpose": "Phase 25.9I-B parent universe: instruments the adaptive "
                   "layer is permitted to evaluate",
        "rule": RULE_TEMPLATE % (args.min_candles, 100 * MIN_VOLUME_COVERAGE,
                                 args.liquidity_window, args.per_sector),
        "excludes": "returns, signals, model output, labels and every "
                    "outcome-derived quantity",
        "as_of": as_of,
        "newest_candle_at_as_of": newest,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": "price_candle_cache (interval='1d') and the canonical "
                  "instrument master",
        "members_sha256": hashlib.sha256(payload).hexdigest(),
        "member_count": len(members),
        "sector_count": len({m["sector_id"] for m in members}),
        "benchmarks": BENCHMARKS,
        "members": members,
        "excluded_count": len(excluded) + len(capped),
        "excluded": [{"instrument_id": i, "ticker": t, "reason": r}
                     for i, t, r in sorted(excluded + capped)],
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(definition, handle, indent=2, sort_keys=False)
        handle.write("\n")
    print("wrote %s" % out)
    print("  members   : %d across %d sectors"
          % (len(members), definition["sector_count"]))
    print("  excluded  : %d (%d on coverage/metadata, %d on the per-sector cap)"
          % (definition["excluded_count"], len(excluded), len(capped)))
    print("  as_of     : %s (newest candle %s)" % (as_of, newest))
    print("  sha256    : %s" % definition["members_sha256"][:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
