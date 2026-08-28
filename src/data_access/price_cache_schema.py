"""
src/data_access/price_cache_schema.py
-------------------------------------
Raw price candle cache for Phase 6 (Market Impact Intelligence).

WHY THIS EXISTS, SEPARATELY FROM THE EVENT STUDY ITSELF
----------------------------------------------------------
yfinance's free intraday data has a hard 60-day lookback: minute-level
candles for an event older than 60 days can never be fetched again,
from anyone, at any price. If EventStudyEngine.build_study() pulled
candles live from yfinance every time it ran, a study computed today
and the "same" study computed in three months could silently differ —
or the second run could simply lose its minute-level windows. That
violates the project's own determinism and reproducibility rules.

This cache is the fix: fetch once, while the data is still available,
and store it. Every later computation reads from here, never from a
live API. The result is a study that is exactly as reproducible as any
other derived table in this database — recomputing it later uses the
same recorded inputs, not whatever yfinance happens to return that day.

WHAT IS CACHED, AND WHAT IS DELIBERATELY NOT
-----------------------------------------------
One row per (instrument_id, interval, timestamp). Shared across events:
if five canonical events involve the same instrument in overlapping
windows, the overlapping candles are fetched and stored once.

`fetched_at` is kept so a candle's provenance is inspectable — spec
rule 18 (data transformations require provenance). Candles are never
overwritten by a later fetch for the same key; INSERT OR IGNORE is
used throughout the caching script, because a price six months from
now for a slot that already has a value is not a correction of that
slot, it is a different, later observation that belongs at a different
timestamp.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_price_cache_schema(conn: sqlite3.Connection) -> None:
    """Create the price cache table and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_candle_cache (
            instrument_id   TEXT NOT NULL,
            interval        TEXT NOT NULL,   -- '1d' or '1m'
            timestamp       TEXT NOT NULL,   -- ISO 8601, UTC
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL,
            adjusted_close  REAL,
            volume          REAL,
            source          TEXT NOT NULL DEFAULT 'yfinance',
            fetched_at      TEXT NOT NULL,
            PRIMARY KEY (instrument_id, interval, timestamp)
        )
    """)

    # Records which (instrument, interval) ranges have already been
    # requested, so a re-run can skip a fetch entirely rather than
    # re-asking yfinance for a range it already has cached — including
    # a range that came back EMPTY (e.g. a delisted or unlisted
    # instrument), which would otherwise be re-requested forever.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_cache_requests (
            instrument_id   TEXT NOT NULL,
            interval        TEXT NOT NULL,
            range_start     TEXT NOT NULL,
            range_end       TEXT NOT NULL,
            row_count       INTEGER NOT NULL DEFAULT 0,
            requested_at    TEXT NOT NULL,
            PRIMARY KEY (instrument_id, interval, range_start, range_end)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_cache_lookup "
                 "ON price_candle_cache(instrument_id, interval, timestamp)")

    conn.commit()
