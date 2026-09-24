"""
src/capture/schema.py
-----------------------------
The capture store (Phase 25.9G).

WHY A DEDICATED DATABASE, AND NOT THE PRODUCTION ONE
--------------------------------------------------------
Production `marketlens.db` is a GitHub Release asset owned by CI: the daily
and research workflows download it, change it and upload it with
`--clobber`. A second writer on this machine uploading its own copy is
exactly the race that failed the daily run of 2026-09-17 -- two uploaders,
last one wins, the other's work silently discarded. The capture process
must therefore never upload that asset, and a local copy of it would drift
from production within a day.

So capture owns its own file, `data/capture/intraday_capture.db` by
default. It holds exactly what capture produces and needs:

    market_data_state / _bars / _cycles   operational (Phase 25.7 schema)
    price_candle_cache (interval 1m)      the durable research archive
                                          (Phase 25.9F contract)
    broker_instrument_mapping             resolved contracts (Phase 14)
    instruments / securities / exchanges  the universe members, so the
                                          25.9F session rules apply
    capture_*                             lifecycle, provenance, quality

Every research tool built in 25.9F takes a connection, so it reads this file
unchanged. Moving captured history into the research release would be a
separate, deliberate step -- not built in this phase, and never a side
effect of capturing.

ADDITIVE AND IDEMPOTENT, like every schema module in this project.
"""

from __future__ import annotations

import sqlite3

CAPTURE_TABLES = (
    "capture_universe_versions",
    "capture_sessions",
    "capture_session_members",
    "capture_instances",
    "capture_events",
    "capture_ticks",
    "capture_archive_log",
    "capture_mappings",
    "intraday_feature_values",
    "capture_quote_samples",
)

#: Columns added after a store was first created (Phase 25.9H). Added in
#: place, so a live store gains them on the next start and loses nothing.
ADDED_COLUMNS = {
    "capture_instances": {"transport": "TEXT"},
    "capture_ticks": {"realtime": "INTEGER", "delayed": "INTEGER",
                      "unknown_availability": "INTEGER", "unavailable": "INTEGER",
                      "venue_spread_seconds": "REAL", "venue_lag_seconds": "REAL",
                      "requests_last_minute": "INTEGER"},
}


def initialize_capture_schema(conn: sqlite3.Connection) -> None:
    from src.data_access.execution_schema import initialize_execution_schema
    from src.data_access.market_data_schema import initialize_market_data_schema
    from src.data_access.price_cache_schema import initialize_price_cache_schema
    from src.data_access.schema import initialize_schema

    initialize_schema(conn)
    initialize_market_data_schema(conn)
    initialize_price_cache_schema(conn)
    initialize_execution_schema(conn)

    statements = [
        # One row per immutable universe version. The sha makes an edited
        # config file detectable: same version, different content, refused.
        """CREATE TABLE IF NOT EXISTS capture_universe_versions (
            version          TEXT PRIMARY KEY,
            sha256           TEXT NOT NULL,
            definition_json  TEXT NOT NULL,
            registered_at    TEXT NOT NULL)""",

        """CREATE TABLE IF NOT EXISTS capture_sessions (
            session_id       TEXT PRIMARY KEY,
            session_date     TEXT NOT NULL,
            session_type     TEXT NOT NULL,          -- regular | early_close
            opens_at         TEXT NOT NULL,
            closes_at        TEXT NOT NULL,
            universe_version TEXT NOT NULL,
            status           TEXT NOT NULL,          -- open | finalized
            quality          TEXT,                   -- GOOD | PARTIAL | DEGRADED | FAILED
            observed_from    TEXT,
            observed_until   TEXT,
            created_at       TEXT NOT NULL,
            finalized_at     TEXT,
            summary_json     TEXT NOT NULL DEFAULT '{}')""",

        # Point-in-time membership: which instruments were EXPECTED this
        # session, and what their contracts were. Never rewritten by a
        # later universe version (§18).
        """CREATE TABLE IF NOT EXISTS capture_session_members (
            session_id       TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            ticker           TEXT NOT NULL,
            mapping_status   TEXT NOT NULL,
            conid            TEXT,
            detail           TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, instrument_id))""",

        # One row per process. `lease_owner` is stable across supervised
        # restarts; `instance_id` is not -- that pair is what tells a
        # restart from a duplicate runner (§56).
        """CREATE TABLE IF NOT EXISTS capture_instances (
            instance_id      TEXT PRIMARY KEY,
            lease_owner      TEXT NOT NULL,
            pid              INTEGER,
            host             TEXT NOT NULL DEFAULT '',
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            exit_reason      TEXT,
            state            TEXT NOT NULL,
            auth_state       TEXT NOT NULL DEFAULT 'unknown',
            session_id       TEXT,
            heartbeat_at     TEXT,
            last_quote_at    TEXT,
            last_bar_at      TEXT,
            last_archive_at  TEXT,
            last_feature_at  TEXT,
            last_error       TEXT NOT NULL DEFAULT '',
            broker_write_attempts INTEGER NOT NULL DEFAULT 0)""",

        """CREATE TABLE IF NOT EXISTS capture_events (
            event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            at               TEXT NOT NULL,
            instance_id      TEXT NOT NULL,
            session_id       TEXT,
            kind             TEXT NOT NULL,
            detail           TEXT NOT NULL DEFAULT '')""",

        # One row per acquisition tick: the synchronisation record. All
        # instruments of a tick share ONE batched request, so requested_at
        # and received_at bound the whole cross-section (§23).
        """CREATE TABLE IF NOT EXISTS capture_ticks (
            session_id       TEXT NOT NULL,
            tick_at          TEXT NOT NULL,
            instance_id      TEXT NOT NULL,
            requested_at     TEXT,
            received_at      TEXT,
            target_minute    TEXT,
            requested        INTEGER NOT NULL DEFAULT 0,
            tradeable        INTEGER NOT NULL DEFAULT 0,
            bars_written     INTEGER NOT NULL DEFAULT 0,
            archived         INTEGER NOT NULL DEFAULT 0,
            features         INTEGER NOT NULL DEFAULT 0,
            duration_seconds REAL,
            health           TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, tick_at))""",

        # Provenance: which session and process archived each research bar,
        # under which archival version.
        """CREATE TABLE IF NOT EXISTS capture_archive_log (
            instrument_id    TEXT NOT NULL,
            bar_start        TEXT NOT NULL,
            session_id       TEXT NOT NULL,
            instance_id      TEXT NOT NULL,
            archive_version  TEXT NOT NULL,
            archived_at      TEXT NOT NULL,
            PRIMARY KEY (instrument_id, bar_start))""",

        """CREATE TABLE IF NOT EXISTS capture_mappings (
            instrument_id    TEXT PRIMARY KEY,
            status           TEXT NOT NULL,   -- RESOLVED | FAILED | AMBIGUOUS | UNSUPPORTED
            conid            TEXT,
            attempts         INTEGER NOT NULL DEFAULT 0,
            last_attempt_at  TEXT,
            next_retry_at    TEXT,
            detail           TEXT NOT NULL DEFAULT '')""",

        # Durable feature values, recomputable from archived bars: a feature
        # bug is repaired by recomputation, never by re-collecting (§41).
        """CREATE TABLE IF NOT EXISTS intraday_feature_values (
            instrument_id    TEXT NOT NULL,
            cutoff           TEXT NOT NULL,
            feature_id       TEXT NOT NULL,
            feature_version  TEXT NOT NULL,
            value            REAL,
            session_id       TEXT NOT NULL DEFAULT '',
            computed_at      TEXT NOT NULL,
            PRIMARY KEY (instrument_id, cutoff, feature_id, feature_version))""",
        # A bounded sample of normalized real quotes (Phase 25.9H): the
        # evidence for snapshot shape, realtime/delayed status and clocks.
        """CREATE TABLE IF NOT EXISTS capture_quote_samples (
            session_id       TEXT NOT NULL,
            tick_at          TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            conid            TEXT,
            last             REAL,
            bid              REAL,
            ask              REAL,
            mid              REAL,
            volume           REAL,
            availability     TEXT,
            freshness        TEXT,
            broker_at        TEXT,
            received_at      TEXT,
            note             TEXT,
            PRIMARY KEY (session_id, tick_at, instrument_id))""",
        "CREATE INDEX IF NOT EXISTS idx_capture_features_cutoff "
        "ON intraday_feature_values (cutoff)",
        "CREATE INDEX IF NOT EXISTS idx_capture_events_at "
        "ON capture_events (at)",
        "CREATE INDEX IF NOT EXISTS idx_capture_archive_session "
        "ON capture_archive_log (session_id)",
    ]
    for statement in statements:
        conn.execute(statement)
    for table, columns in ADDED_COLUMNS.items():
        present = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for name, kind in columns.items():
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {name} {kind}')
    conn.commit()


#: Every table the capture process may write. The write-boundary test
#: asserts nothing outside this set changes during capture (§111, §112).
CAPTURE_WRITABLE = frozenset(CAPTURE_TABLES) | {
    "market_data_state", "market_data_bars", "market_data_cycles",
    "price_candle_cache", "broker_instrument_mapping",
    "instruments", "securities", "exchanges", "companies",
    "session_runner_leases", "sqlite_sequence",
}
