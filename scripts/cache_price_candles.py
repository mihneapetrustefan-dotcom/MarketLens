"""
Caches Polygon daily and minute candles for the primary instrument of
every canonical event, so EventStudyEngine never depends on a live API
call.

WHY CACHING AT ALL, GIVEN POLYGON'S FREE TIER HAS ~2 YEARS OF MINUTE
HISTORY (not yfinance's fragile 60 days)
----------------------------------------------------------------------
Reproducibility (spec rules 11, 12) does not stop mattering just
because the expiry window is longer. Even with two years of headroom,
a study computed today and recomputed in six months should read the
same recorded prices, not whatever the provider happens to return on
a second live call — a provider outage, a data revision, or eventual
expiry of that older window would otherwise change history. Caching
once and computing from the cache is what makes an event study exactly
as reproducible as any other derived table in this database.

RATE LIMIT SHAPES THIS SCRIPT'S STRUCTURE
--------------------------------------------
Polygon's free Basic tier allows 5 requests/minute (enforced by
PolygonConnector's built-in 12.5s pacing). With ~244 instruments this
script needs, a full run makes on the order of a few hundred requests
and can take one to several hours. That is why fetches are grouped:
ONE daily request per instrument covers baseline + all post-event
windows for every event on that instrument; minute requests are
grouped per (instrument, calendar day) so multiple events on the same
instrument the same day cost one request, not several.

price_cache_requests records which ranges have already been fetched —
including ranges that came back empty — so a re-run after adding new
events only fetches what is actually new.

ONLY THE PRIMARY PARTICIPANT DRIVES INSTRUMENT RESOLUTION
------------------------------------------------------------
A canonical event can have secondary participants; only the primary
one (spec-defined as the entity the event centers on) is used to pick
the instrument to study. This mirrors the same PRIMARY-vs-SECONDARY
distinction the extractor already establishes — not a new judgment
call invented here.

WHAT IS SKIPPED, AND WHY
----------------------------
BVB (Bucharest) instruments are skipped, honestly: Polygon's coverage
is US/global-listed equities, and this project's own yfinance
integration already documents the same gap. No fetch is attempted for
them; they are recorded as skipped so this is visible, not silent.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src.data_access.price_cache_schema import initialize_price_cache_schema
from src.impact.polygon_connector import PolygonConnector, normalize_ticker_for_polygon
from src.pointintime.view import market_visibility_time

DEFAULT_DB = os.path.join(REPO_ROOT, "data", "marketlens.db")

#: Trading-day baseline the event study engine requires, converted to
#: a calendar-day fetch window with margin for weekends/holidays.
#: 150 trading days ~ 220 calendar days; +30 days of headroom on top.
BASELINE_CALENDAR_DAYS = 250
#: Forward margin past the latest anchor, for the d1..d20 post-event
#: windows plus weekend/holiday slack.
FORWARD_CALENDAR_DAYS = 35
#: Minute-window margin around each anchor, wider than the largest
#: DEFAULT_WINDOWS minute offset (60) so edge candles are covered.
MINUTE_MARGIN = timedelta(minutes=65)


#: Reserved instrument_id for the market benchmark, used to key its
#: candles in the SAME price_candle_cache table as real instruments.
#: SPY is not in `instruments` — that table comes from the company
#: registry, and an index ETF is not a company — so it needs this
#: separate, explicit key rather than a fabricated company/security/
#: instrument chain (spec rule 3: no parallel data model without a
#: strong reason, and an ETF is not the same kind of thing as a
#: security tied to a company).
BENCHMARK_INSTRUMENT_ID = "benchmark-spy"
BENCHMARK_TICKER = "SPY"


def cache_benchmark(conn: sqlite3.Connection, connector: PolygonConnector,
                    all_anchors: List[datetime], dry_run: bool) -> Tuple[int, int, int, int]:
    """
    Cache SPY as the market benchmark, covering every event's window.

    Uses the SAME daily-per-range and minute-per-day logic as
    per-instrument caching, just keyed under BENCHMARK_INSTRUMENT_ID
    and spanning ALL events at once rather than one instrument's
    events — there is only one benchmark series, shared by everything.

    Returns (daily_calls, daily_rows, minute_calls, minute_rows).
    """
    if not all_anchors:
        return (0, 0, 0, 0)

    daily_calls = daily_rows = minute_calls = minute_rows = 0

    d_start = min(all_anchors) - timedelta(days=BASELINE_CALENDAR_DAYS)
    d_end = max(all_anchors) + timedelta(days=FORWARD_CALENDAR_DAYS)
    if not is_range_cached(conn, BENCHMARK_INSTRUMENT_ID, "1d", d_start, d_end):
        daily_calls += 1
        if not dry_run:
            candles = connector.get_daily_candles(BENCHMARK_TICKER, d_start.date(), d_end.date())
            n = store_candles(conn, BENCHMARK_INSTRUMENT_ID, "1d", candles)
            record_request(conn, BENCHMARK_INSTRUMENT_ID, "1d", d_start, d_end, n)
            daily_rows += n
            conn.commit()

    by_day: Dict[str, List[datetime]] = defaultdict(list)
    for a in all_anchors:
        by_day[a.date().isoformat()].append(a)

    for _, day_anchors in by_day.items():
        m_start = min(day_anchors) - MINUTE_MARGIN
        m_end = max(day_anchors) + MINUTE_MARGIN
        if is_range_cached(conn, BENCHMARK_INSTRUMENT_ID, "1m", m_start, m_end):
            continue
        minute_calls += 1
        if not dry_run:
            candles = connector.get_minute_candles(BENCHMARK_TICKER, m_start, m_end)
            n = store_candles(conn, BENCHMARK_INSTRUMENT_ID, "1m", candles)
            record_request(conn, BENCHMARK_INSTRUMENT_ID, "1m", m_start, m_end, n)
            minute_rows += n
            conn.commit()

    return (daily_calls, daily_rows, minute_calls, minute_rows)


def resolve_event_instruments(conn: sqlite3.Connection) -> List[Tuple[str, str, str, str]]:
    """
    One row per canonical event with a resolvable primary instrument:
    (canonical_event_id, instrument_id, ticker, asset_class).

    Events whose primary participant has no company/security/
    instrument chain are silently excluded — there is nothing to
    fetch a price for.
    """
    sql = """
        SELECT ce.canonical_event_id, i.instrument_id, i.ticker, i.asset_class
        FROM canonical_events ce
        JOIN canonical_event_participants cep
            ON cep.canonical_event_id = ce.canonical_event_id AND cep.role = 'primary'
        JOIN companies co ON co.company_id = cep.entity_id
        JOIN securities s ON s.company_id = co.company_id
        JOIN instruments i ON i.security_id = s.security_id
    """
    return conn.execute(sql).fetchall()


def anchor_for_event(conn: sqlite3.Connection, canonical_event_id: str) -> Optional[datetime]:
    """
    The market-visibility anchor for one canonical event: the LATEST
    plausible moment the information became public, same conservative
    rule EventStudyEngine itself uses.

    event_time is not populated anywhere upstream yet (Phase 4 does
    not extract it), so this always resolves through
    first_reported_at, which fusion sets to the earliest report's
    publication_time — the correct fallback per market_visibility_time
    when only a publication time is known.
    """
    row = conn.execute(
        "SELECT event_time, first_reported_at FROM canonical_events WHERE canonical_event_id = ?",
        (canonical_event_id,),
    ).fetchone()
    if not row:
        return None
    event_time_str, first_reported_str = row
    event_time = datetime.fromisoformat(event_time_str) if event_time_str else None
    publication_time = datetime.fromisoformat(first_reported_str) if first_reported_str else None
    visibility = market_visibility_time(event_time, publication_time)
    return visibility.latest if visibility else None


def is_range_cached(conn: sqlite3.Connection, instrument_id: str, interval: str,
                    start: datetime, end: datetime) -> bool:
    """True if an existing recorded request range fully contains [start, end]."""
    row = conn.execute("""
        SELECT 1 FROM price_cache_requests
        WHERE instrument_id = ? AND interval = ? AND range_start <= ? AND range_end >= ?
        LIMIT 1
    """, (instrument_id, interval, start.isoformat(), end.isoformat())).fetchone()
    return row is not None


def record_request(conn: sqlite3.Connection, instrument_id: str, interval: str,
                   start: datetime, end: datetime, row_count: int) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO price_cache_requests
        (instrument_id, interval, range_start, range_end, row_count, requested_at)
        VALUES (?,?,?,?,?,?)
    """, (instrument_id, interval, start.isoformat(), end.isoformat(),
          row_count, datetime.now(timezone.utc).isoformat()))


def store_candles(conn: sqlite3.Connection, instrument_id: str, interval: str, candles) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (instrument_id, interval, c.timestamp.isoformat(), c.open, c.high, c.low,
         c.close, c.adjusted_close, c.volume, "polygon", now)
        for c in candles
    ]
    conn.executemany("""
        INSERT OR IGNORE INTO price_candle_cache
        (instrument_id, interval, timestamp, open, high, low, close, adjusted_close, volume, source, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit-instruments", type=int, default=None,
                        help="Cap the number of distinct instruments processed (testing).")
    parser.add_argument("--skip-minute", action="store_true",
                        help="Fetch only daily candles, skip the minute-level cache.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be fetched without calling Polygon or writing.")
    args = parser.parse_args()

    api_key = os.environ.get("POLYGON_API_KEY")
    connector = PolygonConnector(api_key=api_key)
    if not connector.is_configured() and not args.dry_run:
        print("EROARE: POLYGON_API_KEY nu este setat. Folositi --dry-run pentru a vedea planul fara cheie.")
        return 1

    if not os.path.exists(args.db):
        print(f"EROARE: baza nu exista: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    initialize_price_cache_schema(conn)

    event_instruments = resolve_event_instruments(conn)
    print(f"Evenimente cu instrument rezolvabil: {len(event_instruments):,}")

    by_instrument: Dict[str, Dict] = {}
    skipped_bvb = 0
    for canonical_event_id, instrument_id, ticker, asset_class in event_instruments:
        symbol = normalize_ticker_for_polygon(ticker, asset_class)
        if symbol is None:
            skipped_bvb += 1
            continue
        anchor = anchor_for_event(conn, canonical_event_id)
        if anchor is None:
            continue
        entry = by_instrument.setdefault(instrument_id, {"symbol": symbol, "anchors": []})
        entry["anchors"].append(anchor)

    print(f"Instrumente distincte de interogat: {len(by_instrument):,}")
    print(f"Evenimente sarite (BVB, neacoperit) : {skipped_bvb:,}")

    if args.limit_instruments:
        keys = list(by_instrument.keys())[:args.limit_instruments]
        by_instrument = {k: by_instrument[k] for k in keys}
        print(f"LIMITAT la {len(by_instrument):,} instrumente (--limit-instruments)")

    daily_calls = 0
    daily_rows = 0
    daily_skipped_cached = 0
    minute_calls = 0
    minute_rows = 0
    minute_skipped_cached = 0

    for instrument_id, entry in by_instrument.items():
        symbol = entry["symbol"]
        anchors = entry["anchors"]

        # --- Daily: one request per instrument covers every event on it ---
        d_start = min(anchors) - timedelta(days=BASELINE_CALENDAR_DAYS)
        d_end = max(anchors) + timedelta(days=FORWARD_CALENDAR_DAYS)
        if not is_range_cached(conn, instrument_id, "1d", d_start, d_end):
            daily_calls += 1
            if not args.dry_run:
                candles = connector.get_daily_candles(symbol, d_start.date(), d_end.date())
                n = store_candles(conn, instrument_id, "1d", candles)
                record_request(conn, instrument_id, "1d", d_start, d_end, n)
                daily_rows += n
                conn.commit()
        else:
            daily_skipped_cached += 1

        if args.skip_minute:
            continue

        # --- Minute: grouped per (instrument, calendar day) ---
        by_day: Dict[str, List[datetime]] = defaultdict(list)
        for a in anchors:
            by_day[a.date().isoformat()].append(a)

        for _, day_anchors in by_day.items():
            m_start = min(day_anchors) - MINUTE_MARGIN
            m_end = max(day_anchors) + MINUTE_MARGIN
            if is_range_cached(conn, instrument_id, "1m", m_start, m_end):
                minute_skipped_cached += 1
                continue
            minute_calls += 1
            if not args.dry_run:
                candles = connector.get_minute_candles(symbol, m_start, m_end)
                n = store_candles(conn, instrument_id, "1m", candles)
                record_request(conn, instrument_id, "1m", m_start, m_end, n)
                minute_rows += n
                conn.commit()

    # --- Benchmark (SPY): one shared series covering every event ---
    all_anchors = [a for entry in by_instrument.values() for a in entry["anchors"]]
    b_daily_calls, b_daily_rows, b_minute_calls, b_minute_rows = cache_benchmark(
        conn, connector, all_anchors, args.dry_run)
    daily_calls += b_daily_calls
    daily_rows += b_daily_rows
    minute_calls += b_minute_calls
    minute_rows += b_minute_rows
    if b_daily_calls or b_minute_calls:
        print(f"Benchmark ({BENCHMARK_TICKER}): {b_daily_calls} cerere zilnica, "
              f"{b_minute_calls} cereri pe minute noi")
    else:
        print(f"Benchmark ({BENCHMARK_TICKER}): deja in cache")

    print()
    print(f"Cereri zilnice   : {daily_calls:,} noi, {daily_skipped_cached:,} deja in cache")
    print(f"Cereri pe minute : {minute_calls:,} noi, {minute_skipped_cached:,} deja in cache")
    total_calls = daily_calls + minute_calls
    est_minutes = total_calls * 12.5 / 60
    print(f"Total cereri Polygon: {total_calls:,} (~{est_minutes:.0f} minute la 5/min)")

    if args.dry_run:
        print("DRY RUN — nicio cerere reala, nimic scris.")
        conn.close()
        return 0

    print(f"Randuri zilnice scrise: {daily_rows:,}")
    print(f"Randuri pe minute scrise: {minute_rows:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
