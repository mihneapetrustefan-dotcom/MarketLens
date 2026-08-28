"""
Computes Phase 6 event studies for every canonical event with a
resolvable instrument, reading prices exclusively from
price_candle_cache — never live from Polygon.

WHY THIS SCRIPT NEVER TOUCHES THE NETWORK
--------------------------------------------
cache_price_candles.py is the only piece of Phase 6 allowed to call
Polygon. This script is downstream of it by design: an event study is
a derived calculation that must be exactly reproducible from recorded
inputs (spec rules 11, 12). If this script fetched live prices, two
runs on different days could silently disagree about the same
historical event.

WINDOWS MIX TWO GRANULARITIES — HANDLED EXPLICITLY, NOT BY ACCIDENT
----------------------------------------------------------------------
DEFAULT_WINDOWS mixes MINUTES windows (wall-clock arithmetic) and
TRADING_DAYS windows (walked over a real session calendar). The
engine derives that calendar from `session_timestamps`, or — if the
caller omits it — from the candle series itself. Passing one merged
daily+minute candle series WITHOUT an explicit session_timestamps
would corrupt the day-window calendar: every minute bar would be
miscounted as its own "trading day".

This script avoids that by passing session_timestamps EXPLICITLY as
the daily-only timestamps, while still handing the engine a merged
daily+minute candle series for price lookups — so minute windows get
wall-clock precision and day windows get an honest session calendar,
at the same time, from the same call.

ONLY THE PRIMARY PARTICIPANT'S INSTRUMENT IS STUDIED
--------------------------------------------------------
Same scoping as cache_price_candles.py: a canonical event centers on
its primary participant. Secondary participants are not studied here.

SCOPE: build_study() OUTPUT ONLY
------------------------------------
This persists returns, volume, volatility, gap, and the quality
verdict — what build_study() itself computes. EventStudy's
cross-event fields (sector/peer/macro context, market regime,
confounders) come from separate engine methods that operate across
many studies at once; they are not part of this script. See
impact_schema.py's docstring for the same note.

SAFETY
------
- Schema creation is CREATE TABLE IF NOT EXISTS.
- price_candle_cache, events, and canonical_events are read-only
  inputs.
- --dry-run computes every study in memory and reports the outcome
  without writing.
- Re-running is safe: study ids are derived deterministically from
  (event_id, instrument_id), and every write is INSERT OR REPLACE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.impact_schema import initialize_impact_schema
from src.domain.impact_models import BenchmarkModel
from src.impact.engine import Candle, EventStudyEngine

# Reused from cache_price_candles.py rather than duplicated — same
# resolution logic, same anchor rule, same reserved benchmark key.
# Importing keeps the two scripts from silently drifting apart on
# what "the primary instrument" or "the anchor" means.
from scripts.cache_price_candles import (
    resolve_event_instruments, anchor_for_event, BENCHMARK_INSTRUMENT_ID,
)

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")


def study_id_for(event_id: str, instrument_id: str) -> str:
    """Stable study id, so re-running rewrites rather than duplicates."""
    digest = hashlib.sha1(f"{event_id}|{instrument_id}".encode("utf-8")).hexdigest()
    return f"es-{digest[:16]}"


def load_candles(conn: sqlite3.Connection, instrument_id: str) -> Tuple[List[Candle], List[datetime]]:
    """
    Load one instrument's cached candles.

    Returns (merged, daily_timestamps):
      merged           — daily + minute candles combined, sorted by
                          time, for the engine's price lookups.
      daily_timestamps — daily-only timestamps, for session_timestamps
                          (see module docstring for why this must stay
                          separate from `merged`).
    """
    rows = conn.execute("""
        SELECT interval, timestamp, open, high, low, close, adjusted_close, volume
        FROM price_candle_cache WHERE instrument_id = ? ORDER BY timestamp
    """, (instrument_id,)).fetchall()

    merged: List[Candle] = []
    daily_timestamps: List[datetime] = []
    for interval, ts, o, h, l, c, adj, vol in rows:
        moment = datetime.fromisoformat(ts)
        candle = Candle(timestamp=moment, open_=o, high=h, low=l, close=c,
                        volume=vol, adjusted_close=adj)
        merged.append(candle)
        if interval == "1d":
            daily_timestamps.append(moment)
    return merged, daily_timestamps


def persist(conn: sqlite3.Connection, study) -> None:
    sid = study_id_for(study.event_id, study.instrument_id)
    gap = study.gap
    conn.execute("""
        INSERT OR REPLACE INTO event_studies (
            study_id, event_id, instrument_id, benchmark_id,
            event_time, publication_time, ingestion_time,
            market_visibility_earliest, market_visibility_latest, visibility_basis,
            is_direct, quality_level, quality_issues_json,
            observations_available, observations_expected,
            gap_occurred_during_session, gap_previous_close, gap_next_open,
            gap_return, gap_intraday_followthrough, computed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sid, study.event_id, study.instrument_id, study.benchmark_id,
        _iso(study.event_time), _iso(study.publication_time), _iso(study.ingestion_time),
        _iso(study.market_visibility_earliest), _iso(study.market_visibility_latest), study.visibility_basis,
        int(study.is_direct), study.quality.level.value, json.dumps([i.value for i in study.quality.issues]),
        study.quality.observations_available, study.quality.observations_expected,
        int(gap.occurred_during_session) if gap else None,
        str(gap.previous_close) if gap and gap.previous_close is not None else None,
        str(gap.next_open) if gap and gap.next_open is not None else None,
        gap.gap_return if gap else None,
        gap.intraday_followthrough if gap else None,
        _iso(study.computed_at),
    ))

    for window_name, r in study.returns.items():
        conn.execute("""
            INSERT OR REPLACE INTO event_study_returns (
                study_id, window_name, method, price_before, price_after, raw_return,
                benchmark_id, benchmark_return, expected_return, abnormal_return,
                benchmark_model, quality_level, quality_issues_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sid, window_name, r.method.value,
            str(r.price_before) if r.price_before is not None else None,
            str(r.price_after) if r.price_after is not None else None,
            r.raw_return, r.benchmark_id, r.benchmark_return, r.expected_return, r.abnormal_return,
            r.benchmark_model.value if r.benchmark_model else None,
            r.quality.level.value, json.dumps([i.value for i in r.quality.issues]),
        ))

    for window_name, v in study.volume.items():
        conn.execute("""
            INSERT OR REPLACE INTO event_study_volume (
                study_id, window_name, event_volume, baseline_mean_volume, baseline_std_volume,
                relative_volume, volume_zscore, quality_level, quality_issues_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (sid, window_name, v.event_volume, v.baseline_mean_volume, v.baseline_std_volume,
              v.relative_volume, v.volume_zscore, v.quality.level.value,
              json.dumps([i.value for i in v.quality.issues])))

    for window_name, vo in study.volatility.items():
        conn.execute("""
            INSERT OR REPLACE INTO event_study_volatility (
                study_id, window_name, pre_volatility, post_volatility, volatility_change_pct,
                quality_level, quality_issues_json
            ) VALUES (?,?,?,?,?,?,?)
        """, (sid, window_name, vo.pre_volatility, vo.post_volatility, vo.volatility_change_pct,
              vo.quality.level.value, json.dumps([i.value for i in vo.quality.issues])))


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max events to study, newest first.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without this the script is a dry run.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_impact_schema(conn)

    event_instruments = resolve_event_instruments(conn)
    if args.limit:
        event_instruments = event_instruments[:args.limit]
    print(f"Evenimente de studiat: {len(event_instruments):,}")

    benchmark_candles, _ = load_candles(conn, BENCHMARK_INSTRUMENT_ID)
    if not benchmark_candles:
        print("ATENTIE: niciun candle de benchmark (SPY) in cache. "
              "Randamentele ajustate la piata vor lipsi din toate studiile.")

    engine = EventStudyEngine()
    candle_cache: Dict[str, Tuple[List[Candle], List[datetime]]] = {}
    studies = []
    quality_counts: Counter = Counter()

    for canonical_event_id, instrument_id, _ticker, _asset_class in event_instruments:
        anchor_publication = None
        row = conn.execute(
            "SELECT first_reported_at FROM canonical_events WHERE canonical_event_id = ?",
            (canonical_event_id,)).fetchone()
        if row and row[0]:
            anchor_publication = datetime.fromisoformat(row[0])

        if instrument_id not in candle_cache:
            candle_cache[instrument_id] = load_candles(conn, instrument_id)
        candles, daily_timestamps = candle_cache[instrument_id]

        study = engine.build_study(
            event_id=canonical_event_id,
            instrument_id=instrument_id,
            candles=candles,
            event_time=None,
            publication_time=anchor_publication,
            benchmark_id=BENCHMARK_INSTRUMENT_ID if benchmark_candles else None,
            benchmark_candles=benchmark_candles or None,
            benchmark_model=BenchmarkModel.MARKET_ADJUSTED,
            is_direct=True,
            session_timestamps=daily_timestamps,
        )
        studies.append(study)
        quality_counts[study.quality.level.value] += 1

    print("Calitate studii:")
    for level, n in quality_counts.most_common():
        print(f"  {level:16s} {n:>6,}")

    usable = sum(1 for s in studies if s.quality.level.value != "unusable")
    print(f"Studii utilizabile: {usable:,} / {len(studies):,}")

    if not args.apply:
        print("DRY RUN — nimic nu a fost scris. Adaugati --apply pentru a scrie.")
        conn.close()
        return 0

    for study in studies:
        persist(conn, study)
    conn.commit()
    conn.close()
    print(f"SCRIS: {len(studies):,} studii de impact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
