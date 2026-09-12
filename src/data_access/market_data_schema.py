"""
src/data_access/market_data_schema.py
-------------------------------------------
Operational market-data schema (Phase 25.7).

THE SEPARATION THIS FILE EXISTS TO ENFORCE
----------------------------------------------
Nothing here touches `price_candle_cache`. That table is the
reproducible research record and is owned by
`price_cache_schema.py`; an event study recomputed in six months must
read exactly the prices it read the first time. Writing live
observations into it -- especially partial ones for a minute still in
progress -- would silently change history.

So operational state lives in its own tables, with its own lifecycle:

    market_data_state    ONE row per instrument, REPLACED every cycle.
                         The answer to "what is this worth right now".
                         Has no history by design.

    market_data_bars     Completed 1-minute bars built from those
                         observations, plus explicit gap rows. Bounded
                         retention: this is operational telemetry, not
                         the research corpus.

    market_data_cycles   Append-only record of acquisition cycles, for
                         capacity accounting and diagnosis. Bounded.

WHY NO TICK TABLE
---------------------
The production database is already ~276 MB as a GitHub Release asset.
An unbounded tick store in SQLite would dwarf the research corpus
within days and buy nothing: no current strategy reads below the
5-minute horizon. `market_data_state` answers "now" and
`market_data_bars` answers "this session", which is the whole
requirement. If a strategy ever genuinely needs ticks, that is a
storage decision to take deliberately, not a side effect of this
phase.

RETENTION
-------------
`prune()` enforces the bounds. It is the caller's job to invoke it;
nothing here grows without a ceiling being applied.
"""

from __future__ import annotations

import sqlite3
from typing import List

#: Bars are operational telemetry. Thirty days is enough to diagnose a
#: session that went wrong and to compare a fill against the minute it
#: happened in, without turning SQLite into a market-data warehouse.
DEFAULT_BAR_RETENTION_DAYS = 30

#: Cycle rows are tiny but written every minute of every session. Kept
#: shorter than bars: their value is almost entirely recent.
DEFAULT_CYCLE_RETENTION_DAYS = 7

MARKET_DATA_TABLES = (
    "market_data_state",
    "market_data_bars",
    "market_data_cycles",
)


def initialize_market_data_schema(conn: sqlite3.Connection) -> None:
    """
    Create the operational market-data tables.

    Idempotent and additive, like every other schema module here: safe
    to call on every invocation, creates nothing that already exists,
    and never drops or rewrites a table.
    """
    cursor = conn.cursor()

    # ---------------- current state ----------------
    #
    # One row per instrument, replaced in place. The primary key is the
    # instrument alone precisely BECAUSE there is no history here: a
    # second row for the same instrument would mean two answers to
    # "what is it worth now", and the newer one is the only one that
    # can be right.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_data_state (
            instrument_id   TEXT PRIMARY KEY,
            conid           TEXT NOT NULL DEFAULT '',
            last            REAL,
            bid             REAL,
            ask             REAL,
            mid             REAL,
            volume          REAL,
            availability    TEXT NOT NULL DEFAULT 'unknown',
            freshness       TEXT NOT NULL DEFAULT 'unavailable',
            -- Three timestamps, never collapsed. broker_at is when the
            -- venue says it happened, received_at when we saw it,
            -- evaluated_at when freshness was judged.
            broker_at       TEXT,
            received_at     TEXT,
            evaluated_at    TEXT,
            source          TEXT NOT NULL DEFAULT '',
            session_id      TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_data_state_freshness
        ON market_data_state (freshness)
    """)

    # ---------------- completed bars ----------------
    #
    # UNIQUE on (instrument, bar_start) so a replayed or duplicated
    # cycle cannot write the same minute twice. `is_gap` marks a minute
    # that produced no observation: recorded explicitly rather than
    # interpolated, because an invented bar is indistinguishable from a
    # real one once stored.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_data_bars (
            instrument_id     TEXT NOT NULL,
            bar_start         TEXT NOT NULL,
            bar_end           TEXT NOT NULL,
            open              REAL,
            high              REAL,
            low               REAL,
            close             REAL,
            volume            REAL,
            observation_count INTEGER NOT NULL DEFAULT 0,
            is_complete       INTEGER NOT NULL DEFAULT 0,
            is_gap            INTEGER NOT NULL DEFAULT 0,
            source            TEXT NOT NULL DEFAULT '',
            session_id        TEXT NOT NULL DEFAULT '',
            created_at        TEXT NOT NULL,
            PRIMARY KEY (instrument_id, bar_start)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_data_bars_start
        ON market_data_bars (bar_start)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_data_bars_session
        ON market_data_bars (session_id)
    """)

    # ---------------- cycle audit ----------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_data_cycles (
            cycle_id         TEXT PRIMARY KEY,
            session_id       TEXT NOT NULL DEFAULT '',
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            duration_seconds REAL,
            requested        INTEGER NOT NULL DEFAULT 0,
            received         INTEGER NOT NULL DEFAULT 0,
            tradeable        INTEGER NOT NULL DEFAULT 0,
            stale            INTEGER NOT NULL DEFAULT 0,
            unavailable      INTEGER NOT NULL DEFAULT 0,
            invalid          INTEGER NOT NULL DEFAULT 0,
            broker_requests  INTEGER NOT NULL DEFAULT 0,
            bars_written     INTEGER NOT NULL DEFAULT 0,
            gaps_recorded    INTEGER NOT NULL DEFAULT 0,
            health           TEXT NOT NULL DEFAULT '',
            notes_json       TEXT NOT NULL DEFAULT '[]'
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_data_cycles_started
        ON market_data_cycles (started_at)
    """)

    conn.commit()


def prune(conn: sqlite3.Connection,
          bar_retention_days: int = DEFAULT_BAR_RETENTION_DAYS,
          cycle_retention_days: int = DEFAULT_CYCLE_RETENTION_DAYS) -> dict:
    """
    Apply the retention ceiling.

    Returns what was removed, so a caller can report it rather than
    discover the database quietly shrinking. `market_data_state` is
    never pruned -- it holds exactly one row per instrument and is the
    current answer.
    """
    removed = {"bars": 0, "cycles": 0}
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM market_data_bars WHERE bar_start < "
        "datetime('now', ?)", (f"-{int(bar_retention_days)} days",))
    removed["bars"] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    cursor.execute(
        "DELETE FROM market_data_cycles WHERE started_at < "
        "datetime('now', ?)", (f"-{int(cycle_retention_days)} days",))
    removed["cycles"] = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    return removed


def market_data_tables_present(conn: sqlite3.Connection) -> List[str]:
    """Which of this phase's tables actually exist."""
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    return [t for t in MARKET_DATA_TABLES if t in names]
