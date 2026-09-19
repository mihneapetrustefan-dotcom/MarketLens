"""
scripts/build_anchor_v2_labels.py
-------------------------------------------
Build post-event labels under anchor-v2, BESIDE the v1 labels.

WHY DISTINCT NAMES
----------------------
`research_labels` keys on (observation_id, name) and
`event_study_returns` on (study_id, window_name). Neither key includes
a version. Rebuilding under the same names would therefore OVERWRITE
the v1 rows through INSERT OR REPLACE and destroy v1's
reproducibility.

So v2 labels are written as

    {window}.raw_return.anchor-v2
    {window}.abnormal_return.anchor-v2

with label_version 'v2' and calculation 'anchor-v2'. Every v1 row is
left byte-identical. Nothing in `event_study_returns` is touched.

EVERY OBSERVATION IS ACCOUNTED FOR
--------------------------------------
A label that cannot be resolved is COUNTED WITH ITS REASON, never
filled with zero and never silently dropped. The report prints
resolved / unresolved per window and region, with reasons.

Refuses to write to a path named like the production database unless
--i-understand-this-is-not-production is omitted... it simply refuses
data/marketlens.db outright.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.impact_models import DEFAULT_WINDOWS, WindowKind
from src.impact.anchoring import (
    ANCHOR_METHOD_V2, raw_and_abnormal, resolve_v2,
)
from src.impact.engine import Candle

PROTECTED_START = "2026-08-15T01:23:31"
PROTECTED_END = "2026-08-27T17:09:32"
LABEL_VERSION = "v2"
POST_WINDOWS = [w for w in DEFAULT_WINDOWS if w.kind == WindowKind.POST_EVENT]


def load_split(conn, instrument_id) -> Tuple[List[Candle], List[Candle], List[datetime]]:
    """Minute and daily candles kept APART -- the whole point of v2."""
    minute, daily, sessions = [], [], []
    for iv, ts, o, h, l, c, adj, vol in conn.execute("""
        SELECT interval, timestamp, open, high, low, close, adjusted_close, volume
        FROM price_candle_cache WHERE instrument_id = ? ORDER BY timestamp
    """, (instrument_id,)):
        candle = Candle(timestamp=datetime.fromisoformat(ts), open_=o, high=h,
                        low=l, close=c, volume=vol, adjusted_close=adj)
        if iv == "1d":
            daily.append(candle)
            sessions.append(candle.timestamp)
        else:
            minute.append(candle)
    return minute, daily, sessions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if os.path.normpath(args.db).endswith(os.path.join("data", "marketlens.db")):
        print("REFUSED: this writes research labels; run it on a working copy, "
              "never the production database.")
        return 2

    conn = sqlite3.connect(args.db)
    rows = conn.execute("""
        SELECT o.observation_id, o.instrument_id, s.benchmark_id,
               s.market_visibility_latest, o.information_cutoff
        FROM research_observations o
        JOIN event_studies s
          ON s.event_id = o.event_id AND s.instrument_id = o.instrument_id
        WHERE s.market_visibility_latest IS NOT NULL
          AND o.quality_level != 'invalid'
    """).fetchall()

    cache: Dict[str, tuple] = {}

    def candles(instrument_id):
        if instrument_id not in cache:
            cache[instrument_id] = load_split(conn, instrument_id)
        return cache[instrument_id]

    generated_at = datetime.now(timezone.utc).isoformat()
    newest = conn.execute(
        "SELECT MAX(timestamp) FROM price_candle_cache WHERE interval='1d'").fetchone()[0]

    counts = defaultdict(Counter)      # (window, region) -> {resolved, reason...}
    written = 0
    for obs_id, instrument_id, benchmark_id, anchor_text, cutoff in rows:
        # Bounded on both sides. Observations after the protected END are
        # not part of the protected test and must not be reported as such.
        region = ("protected" if PROTECTED_START <= (cutoff or "") <= PROTECTED_END
                  else "research" if (cutoff or "") < PROTECTED_START else "after_protected")
        anchor = datetime.fromisoformat(anchor_text)
        minute, daily, sessions = candles(instrument_id)
        b_minute, b_daily, _b_sessions = (candles(benchmark_id) if benchmark_id
                                          else ([], [], []))

        for window in POST_WINDOWS:
            resolution = resolve_v2(anchor, window, minute, daily, sessions)
            bench = (resolve_v2(anchor, window, b_minute, b_daily, sessions)
                     if benchmark_id else None)
            raw, abnormal, reason = raw_and_abnormal(resolution, bench)

            key = (window.name, region)
            if abnormal is not None:
                counts[key]["resolved"] += 1
            else:
                counts[key][reason or "unresolved"] += 1

            if args.dry_run:
                continue
            for metric, value in (("raw_return", raw), ("abnormal_return", abnormal)):
                if value is None:
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO research_labels
                      (observation_id, name, value_json, measured_at,
                       window_name, label_version, calculation)
                    VALUES (?,?,?,?,?,?,?)
                """, (obs_id, f"{window.name}.{metric}.{ANCHOR_METHOD_V2}",
                      json.dumps(value),
                      resolution.after.timestamp.isoformat() if resolution.after else None,
                      window.name, LABEL_VERSION, ANCHOR_METHOD_V2))
                written += 1
    if not args.dry_run:
        conn.commit()

    print("=" * 74)
    print("anchor-v2 label build")
    print("=" * 74)
    print(f"observations          {len(rows)}")
    print(f"method / label ver    {ANCHOR_METHOD_V2} / {LABEL_VERSION}")
    print(f"generated at          {generated_at}")
    print(f"source daily cutoff   {newest}")
    print(f"label rows written    {written}{'  (dry run)' if args.dry_run else ''}")
    for region in ("research", "protected", "after_protected"):
        print(f"\n--- {region.upper()} ---")
        for window in POST_WINDOWS:
            c = counts[(window.name, region)]
            total = sum(c.values())
            print(f"  {window.name:14s} resolved {c['resolved']:>4}/{total:<4}")
            for reason, n in c.most_common():
                if reason != "resolved":
                    print(f"      {n:>4}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
