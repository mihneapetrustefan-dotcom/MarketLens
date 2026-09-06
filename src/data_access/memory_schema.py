"""
src/data_access/memory_schema.py
--------------------------------------------
Persistence for trading experience, patterns, and snapshots.

FOUR TABLES (§52)
---------------------
`trading_experiences`     one row per subject × horizon × memory version
`memory_patterns`         deterministic aggregates over experiences
`memory_pattern_evidence` which experiences formed which pattern
`memory_snapshots`        what the system knew at a moment

`memory_pattern_evidence` is the one that stops "knowledge" floating
free of the record (§25). A pattern that cannot name the experiences
behind it is a claim, and this schema makes that state unrepresentable:
the CLI's integrity check counts patterns without evidence and the
number must be zero.

WHAT IS NOT DUPLICATED (§52)
--------------------------------
No price, no candle, no attribution evidence text, no outcome
measurement is stored again. `trading_experiences` references Phase 19
and Phase 20 by their natural keys and copies only the values a memory
query filters or aggregates on — direction, returns, excursions,
attribution labels. Joining three tables on every point-in-time
retrieval would make the retrieval unusable, and that is the whole
reason those columns are here rather than fetched.

THE COLUMN THE WHOLE PHASE TURNS ON
---------------------------------------
`trading_experiences.available_at` — when the experience became
KNOWABLE, which is when its outcome window closed. Indexed, because
every point-in-time query filters on it, and `memory_as_of(T)` is the
interface Phase 22 and every later learning phase depend on.

Dating experiences by when they were computed would put the entire
record at one instant and make historical retrieval silently return the
future.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS. Nothing here alters or drops an existing table.
"""

from __future__ import annotations

import sqlite3


def initialize_memory_schema(conn: sqlite3.Connection) -> None:
    """Create the memory tables. Idempotent and additive."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trading_experiences (
            experience_id       TEXT NOT NULL,
            memory_version      TEXT NOT NULL,
            kind                TEXT NOT NULL,

            -- PROVENANCE (§5). References, not copies: these point at
            -- the canonical Phase 19 and Phase 20 records.
            subject_kind        TEXT NOT NULL,
            subject_id          TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            outcome_method_version      TEXT NOT NULL DEFAULT '',
            attribution_method_version  TEXT NOT NULL DEFAULT '',
            trained_model_id    TEXT,
            model_status        TEXT,
            strategy_id         TEXT,
            observation_id      TEXT,

            -- WHEN. `information_cutoff` is what the decision knew;
            -- `available_at` is when the OUTCOME became knowable. The
            -- second is the point-in-time key and is never
            -- `created_at`.
            information_cutoff  TEXT,
            available_at        TEXT,

            -- EXPECTATION (§10) — what the original system actually
            -- produced. Never reconstructed.
            expected_direction  TEXT NOT NULL DEFAULT '',
            expected_return     REAL,
            expected_horizon    TEXT NOT NULL DEFAULT '',
            signal_confidence   REAL,
            signal_strength     REAL,

            -- OUTCOME (§11) — mirrored from Phase 19 for query speed.
            actual_return       REAL,
            actual_direction    TEXT,
            direction_result    TEXT,
            mfe                 REAL,
            mae                 REAL,
            time_to_mfe_seconds REAL,

            -- ATTRIBUTION (§12) — mirrored from Phase 20. The evidence
            -- text itself stays in `attribution_evidence`; only the
            -- count travels, so a consumer knows how much backs it.
            primary_error       TEXT,
            contributing_errors TEXT NOT NULL DEFAULT '[]',
            attribution_confidence TEXT,
            attribution_severity   TEXT,
            evidence_count      INTEGER NOT NULL DEFAULT 0,

            -- CONTEXT (§8, §9) — decision-time only, versioned
            -- separately because context definitions and aggregation
            -- rules change for different reasons.
            context_schema_version TEXT NOT NULL DEFAULT '',
            context_json        TEXT NOT NULL DEFAULT '{}',
            market_regime       TEXT,
            event_type          TEXT,
            instrument_id       TEXT NOT NULL DEFAULT '',
            asset_class         TEXT,
            sector_id           TEXT,

            experience_class    TEXT NOT NULL,
            -- 'validated' | 'experimental' | 'incomplete' | 'superseded'.
            -- Experimental experience is KEPT and never silently pooled.
            quality             TEXT NOT NULL,
            notes_json          TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,

            PRIMARY KEY (subject_kind, subject_id, horizon, memory_version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_patterns (
            pattern_id          TEXT NOT NULL,
            memory_version      TEXT NOT NULL,
            context_schema_version TEXT NOT NULL DEFAULT '',
            pattern_type        TEXT NOT NULL,
            conditions_json     TEXT NOT NULL DEFAULT '{}',

            -- SAMPLE (§19, §26). Never optional.
            sample_size         INTEGER NOT NULL DEFAULT 0,
            instrument_count    INTEGER NOT NULL DEFAULT 0,
            experimental_count  INTEGER NOT NULL DEFAULT 0,

            hits                INTEGER NOT NULL DEFAULT 0,
            misses              INTEGER NOT NULL DEFAULT 0,
            neutrals            INTEGER NOT NULL DEFAULT 0,
            hit_rate            REAL,

            mean_return         REAL,
            median_return       REAL,
            stdev_return        REAL,
            mean_mfe            REAL,
            mean_mae            REAL,

            error_counts_json   TEXT NOT NULL DEFAULT '{}',
            class_counts_json   TEXT NOT NULL DEFAULT '{}',

            -- RECENCY (§28) and STABILITY (§29)
            first_seen          TEXT,
            last_seen           TEXT,
            periods_json        TEXT NOT NULL DEFAULT '[]',
            stability           TEXT NOT NULL DEFAULT 'unknown',

            -- REGIME DEPENDENCE (§30). Stored beside the all-regime
            -- numbers, never collapsed into them.
            regime_breakdown_json TEXT NOT NULL DEFAULT '{}',
            -- CONTRADICTIONS (§32). Kept visible on purpose.
            contradictions_json TEXT NOT NULL DEFAULT '[]',

            quality             TEXT NOT NULL,
            confidence          TEXT NOT NULL,
            notes_json          TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,

            PRIMARY KEY (pattern_id, memory_version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_pattern_evidence (
            pattern_id          TEXT NOT NULL,
            memory_version      TEXT NOT NULL,
            experience_id       TEXT NOT NULL,
            -- Copied so a pattern's own time range can be checked
            -- without joining back, and so a point-in-time query can
            -- rebuild a pattern from only the evidence that existed.
            available_at        TEXT,
            PRIMARY KEY (pattern_id, memory_version, experience_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_snapshots (
            snapshot_id         TEXT PRIMARY KEY,
            memory_version      TEXT NOT NULL,
            -- The moment the snapshot describes. Everything in it was
            -- available at or before this (§37, §38).
            as_of               TEXT NOT NULL,
            experience_count    INTEGER NOT NULL DEFAULT 0,
            pattern_count       INTEGER NOT NULL DEFAULT 0,
            validated_count     INTEGER NOT NULL DEFAULT 0,
            experimental_count  INTEGER NOT NULL DEFAULT 0,
            class_counts_json   TEXT NOT NULL DEFAULT '{}',
            error_counts_json   TEXT NOT NULL DEFAULT '{}',
            summary_json        TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL
        )
    """)

    # Indexes chosen from the queries that actually exist (§50), not
    # speculatively: every retrieval filters on `available_at`, memory
    # search filters on the context dimensions, and pattern evidence is
    # always looked up by pattern.
    # `experience_id` is the join key from `memory_pattern_evidence`, and
    # it is NOT the primary key — that is the natural key
    # (subject, horizon, version). Without this index the integrity
    # check joined 36,311 evidence rows against 6,510 experiences by a
    # full scan each time and took over five minutes; with it, under a
    # second.
    #
    # Found by measuring a query that hung, not by adding indexes
    # speculatively (§50).
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_id
        ON trading_experiences (experience_id, memory_version)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experience_available
        ON trading_experiences (available_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experience_context
        ON trading_experiences (instrument_id, event_type, expected_direction,
                                horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experience_model
        ON trading_experiences (trained_model_id, quality)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experience_class
        ON trading_experiences (experience_class, memory_version)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pattern_type
        ON memory_patterns (pattern_type, quality, memory_version)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pattern_evidence_experience
        ON memory_pattern_evidence (experience_id)
    """)
    conn.commit()
