#!/usr/bin/env python3
"""
scripts/audit_intraday_data.py
-----------------------------------------------------------
Phase 25.9F — how much intraday research data actually exists, and
what could be built from it?

    python scripts/audit_intraday_data.py --db data/marketlens.db
    python scripts/audit_intraday_data.py --db ... --instrument us_and_intl-nvda

READ-ONLY. The database is opened `mode=ro` and copied into memory.
Nothing here writes, trains, promotes or trades.

WHY ROW COUNT IS NOT THE ANSWER
-----------------------------------
One-minute rows are not automatically information. A rolling window is
only defined inside an unbroken run of minutes, and a forward label
needs the minutes after it as well. So this reports CONTIGUITY and
USABLE POINTS, not just totals -- on this project's corpus the two
numbers differ by an order of magnitude, and the difference is the
whole story.

EFFECTIVE SAMPLE, NOT ROW COUNT
-----------------------------------
Consecutive decision minutes overlap: a 30-minute forward return at
10:00 and at 10:01 share 29 of their 30 minutes. Counting both as
independent evidence is how a dataset of 30,000 rows becomes a
confidence interval it has not earned. The effective figure reported
here divides by the horizon and then counts the independent episodes
those blocks actually came from.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.marketdata.intraday import (
    MINUTE, RESEARCH_INTERVAL, contiguous_runs, instruments_with_bars,
    load_research_bars, session_governed,
)

DEFAULT_DB = os.path.join(ROOT, "data", "marketlens.db")

#: Horizons reported. Each needs its own lookback AND its own forward
#: window, so the usable count falls as the horizon grows.
HORIZONS = (5, 15, 30, 60)


def open_copy(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    source = sqlite3.connect(uri, uri=True)
    try:
        copy = sqlite3.connect(":memory:")
        source.backup(copy)
    finally:
        source.close()
    return copy


def operational_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """What the live Phase 25.7 layer has captured, as opposed to the cache."""
    out: Dict[str, Any] = {}
    for table in ("market_data_bars", "market_data_state", "market_data_cycles"):
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None
        out[table] = ("ABSENT" if not present else
                      f"{conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]} row(s)")
    return out


def coverage(conn: sqlite3.Connection,
             instruments: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Per-instrument contiguity and usability, from the research cache."""
    names = list(instruments or instruments_with_bars(conn))
    per_instrument: List[Dict[str, Any]] = []
    all_runs: List[int] = []
    grand_total = 0

    for instrument_id in names:
        bars = load_research_bars(conn, instrument_id)
        if not bars:
            continue
        runs = [len(r) for r in contiguous_runs(bars)]
        all_runs.extend(runs)
        grand_total += len(bars)
        sessions = {b.session_date for b in bars}
        usable = {h: sum(max(0, r - h - h) for r in runs) for h in HORIZONS}
        per_instrument.append({
            "instrument_id": instrument_id,
            "bars": len(bars),
            "first_bar": bars[0].bar_start.isoformat(),
            "last_bar": bars[-1].bar_end.isoformat(),
            "sessions": len(sessions),
            "runs": len(runs),
            "longest_run": max(runs) if runs else 0,
            "median_run": statistics.median(runs) if runs else 0,
            "usable": usable,
            "session_governed": session_governed(conn, instrument_id),
        })

    per_instrument.sort(key=lambda r: -r["bars"])
    return {"instruments": per_instrument, "total_bars": grand_total,
            "total_runs": len(all_runs), "runs": all_runs}


def effective_sample(runs: Sequence[int], horizon: int) -> Dict[str, Any]:
    """
    Overlapping points, non-overlapping blocks, and the episodes behind
    them.

    Three numbers because they answer three different questions, and
    only the last one is close to "how much independent evidence is
    there".
    """
    lookback = horizon
    overlapping = sum(max(0, r - lookback - horizon) for r in runs)
    blocks = sum(max(0, (r - lookback) // horizon) for r in runs)
    episodes = sum(1 for r in runs if r >= lookback + horizon)
    return {"overlapping_points": overlapping,
            "non_overlapping_blocks": blocks,
            "independent_runs": episodes}


#: A defensible walk-forward intraday study wants several folds over
#: genuinely different periods. One month is about the smallest fold
#: worth calling one, and five folds is about the fewest worth
#: believing -- so roughly six months, or 120 sessions.
SESSIONS_FOR_WALK_FORWARD = 120
MONTHS_FOR_REGIME_DIVERSITY = 6

#: Below this there is not enough even to explore honestly.
SESSIONS_FOR_EXPLORATION = 40
MONTHS_FOR_EXPLORATION = 2

VERDICT_ORDER = ["NOT IMPLEMENTED", "INSUFFICIENT", "MARGINAL", "READY"]


def calendar_ceiling(sessions: int, months: int) -> Tuple[str, str]:
    """
    The best verdict CALENDAR COVERAGE alone permits, and why.

    This is a ceiling, not a score. More one-minute rows inside the
    same eight weeks cannot buy a second market regime, so no amount of
    horizon evidence may raise a verdict above this line. That is the
    §29 question -- regime diversity and fold count -- answered before
    any row count is consulted.
    """
    if sessions >= SESSIONS_FOR_WALK_FORWARD and months >= MONTHS_FOR_REGIME_DIVERSITY:
        return "READY", "%d sessions across %d months" % (sessions, months)
    if sessions >= SESSIONS_FOR_EXPLORATION and months >= MONTHS_FOR_EXPLORATION:
        return "MARGINAL", ("%d sessions across %d months: enough to explore, "
                            "too few folds to qualify a model" % (sessions, months))
    if sessions > 0:
        return "INSUFFICIENT", "only %d sessions across %d months" % (sessions, months)
    return "NOT IMPLEMENTED", "no intraday sessions"


def readiness(effective: Dict[str, Any], instruments: int,
              ceiling: str = "READY") -> str:
    """
    One word per horizon: the evidence verdict, capped by the calendar.

    The thresholds are deliberately crude and stated rather than tuned.
    A verdict can only ever be as good as the calendar allows -- which
    on this corpus is the binding constraint at every horizon.
    """
    blocks = effective["non_overlapping_blocks"]
    episodes = effective["independent_runs"]
    if episodes >= 200 and blocks >= 1000 and instruments >= 20:
        verdict = "READY"
    elif episodes >= 50 and blocks >= 200:
        verdict = "MARGINAL"
    elif blocks > 0:
        verdict = "INSUFFICIENT"
    else:
        verdict = "NOT IMPLEMENTED"
    return min(verdict, ceiling, key=VERDICT_ORDER.index)


def calendar_span(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Distinct session dates and calendar months the corpus touches."""
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(timestamp,1,10) FROM price_candle_cache "
        "WHERE interval = ? ORDER BY 1", (RESEARCH_INTERVAL,))]
    months = sorted({d[:7] for d in rows})
    return {"sessions": len(rows), "months": len(months),
            "first": rows[0] if rows else None,
            "last": rows[-1] if rows else None,
            "month_list": months}


def report(conn: sqlite3.Connection, instruments: Optional[Sequence[str]],
           top: int) -> Dict[str, Any]:
    cover = coverage(conn, instruments)
    runs = cover["runs"]
    feature_ready = [r for r in cover["instruments"] if r["longest_run"] >= 31]

    span = calendar_span(conn)
    ceiling, ceiling_reason = calendar_ceiling(span["sessions"], span["months"])

    horizons: Dict[str, Any] = {}
    for horizon in HORIZONS:
        effective = effective_sample(runs, horizon)
        instruments_supporting = sum(
            1 for r in cover["instruments"]
            if r["longest_run"] >= horizon * 2 + 1)
        horizons["%dm" % horizon] = {
            **effective,
            "instruments_supporting": instruments_supporting,
            "evidence_only": readiness(effective, instruments_supporting),
            "readiness": readiness(effective, instruments_supporting, ceiling),
        }

    return {
        "operational_layer": operational_state(conn),
        "calendar": {**span, "ceiling": ceiling, "ceiling_reason": ceiling_reason},
        "research_cache": {
            "interval": RESEARCH_INTERVAL,
            "instruments": len(cover["instruments"]),
            "bars": cover["total_bars"],
            "contiguous_runs": cover["total_runs"],
            "median_run_minutes": statistics.median(runs) if runs else 0,
            "mean_run_minutes": round(statistics.fmean(runs), 1) if runs else 0,
            "longest_run_minutes": max(runs) if runs else 0,
            "instruments_with_a_30m_window": len(feature_ready),
        },
        "horizons": horizons,
        "top_instruments": cover["instruments"][:top],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--instrument", action="append", dest="instruments")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    data = report(open_copy(args.db), args.instruments, args.top)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    print("=== OPERATIONAL LAYER (Phase 25.7 live capture) ===")
    for table, state in data["operational_layer"].items():
        print(f"  {table:24s} {state}")

    cache = data["research_cache"]
    print("\n=== RESEARCH 1-MINUTE CACHE ===")
    for key in ("instruments", "bars", "contiguous_runs", "median_run_minutes",
                "mean_run_minutes", "longest_run_minutes",
                "instruments_with_a_30m_window"):
        print(f"  {key:32s} {cache[key]}")

    cal = data["calendar"]
    print("\n=== CALENDAR COVERAGE (the binding constraint) ===")
    print("  sessions                         %s" % cal["sessions"])
    print("  calendar months                  %s %s" % (cal["months"], cal["month_list"]))
    print("  span                             %s -> %s" % (cal["first"], cal["last"]))
    print("  ceiling on any verdict           %s  (%s)" % (cal["ceiling"], cal["ceiling_reason"]))

    print("\n=== HORIZON READINESS (effective sample, not row count) ===")
    print("  %8s %12s %8s %9s %6s  %12s  capped" % ("horizon", "overlapping",
          "blocks", "episodes", "instr", "evidence"))
    for name, h in data["horizons"].items():
        print("  %8s %12d %8d %9d %6d  %12s  %s" % (
            name, h["overlapping_points"], h["non_overlapping_blocks"],
            h["independent_runs"], h["instruments_supporting"],
            h["evidence_only"], h["readiness"]))

    print("\n=== TOP INSTRUMENTS BY BARS ===")
    print(f"  {'instrument':28s} {'bars':>7s} {'sess':>5s} {'runs':>5s} "
          f"{'longest':>8s} {'median':>7s}  feature-ready")
    for row in data["top_instruments"]:
        ready = "yes" if row["longest_run"] >= 31 else "no"
        print(f"  {row['instrument_id']:28s} {row['bars']:7d} {row['sessions']:5d} "
              f"{row['runs']:5d} {row['longest_run']:8d} {row['median_run']:7.0f}  {ready}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
