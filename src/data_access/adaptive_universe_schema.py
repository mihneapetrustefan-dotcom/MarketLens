"""
src/data_access/adaptive_universe_schema.py
------------------------------------------------------
Durable record of dynamic-universe decisions (Phase 25.9I-B).

WHY THESE TABLES EXIST NOW, AFTER I-A DEFERRED PERSISTENCE
--------------------------------------------------------------
25.9I-A deliberately stored nothing: the shape of a session universe was
still a guess. It is not any more. A selection has members, an as-of, a
revision, config versions and per-candidate reasons, and all five are
needed to answer "why was this instrument watched on that day" after the
fact. Guessing that shape was the risk; recording it now is not.

WHAT IS AND IS NOT WRITTEN
------------------------------
Written: parent-universe versions, session snapshots and their revisions,
per-member rows, admission decisions with their reasons and score
components, and expiry events.

Never written: orders, intents, fills, positions, capital, or anything a
broker reads. These tables describe ATTENTION, and attention is not an
instruction. Nothing in `src/adaptive/` imports the execution or trading
packages, so there is no path from here to a venue.

APPEND-ONLY, AND IDEMPOTENT
-------------------------------
Every primary key is derived from the decision itself: a snapshot id from
(session, as_of, revision), an admission id from (instrument, as_of,
outcome). Re-running the same selection therefore writes the same rows,
and `INSERT OR IGNORE` makes the second run a no-op rather than a
duplicate. A revision never updates its predecessor: 09:20's universe is
still readable after 14:05 replaced it, which is the point of recording
revisions at all.

NON-DESTRUCTIVE
-------------------
`CREATE TABLE IF NOT EXISTS` only. No existing table is altered, renamed
or dropped, and this schema is initialized on demand by the callers that
need it rather than being added to any existing startup path.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "25.9I-B.1"


def initialize_adaptive_universe_schema(conn: sqlite3.Connection) -> None:
    """Create the adaptive-universe tables. Safe to call repeatedly."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS adaptive_parent_universe_versions (
            version          TEXT PRIMARY KEY,
            as_of            TEXT NOT NULL,
            members_sha256   TEXT NOT NULL,
            member_count     INTEGER NOT NULL,
            sector_count     INTEGER,
            rule             TEXT,
            source           TEXT,
            registered_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adaptive_session_snapshots (
            snapshot_id               TEXT PRIMARY KEY,
            session_date              TEXT NOT NULL,
            as_of                     TEXT NOT NULL,
            revision                  INTEGER NOT NULL,
            max_capacity              INTEGER NOT NULL,
            selected_count            INTEGER NOT NULL,
            unused_capacity           INTEGER NOT NULL,
            waitlist_count            INTEGER NOT NULL,
            content_hash              TEXT NOT NULL,
            parent_universe_version   TEXT,
            parent_universe_sha256    TEXT,
            eligibility_config_version TEXT,
            scoring_config_version    TEXT,
            admission_config_version  TEXT,
            lineage_id                TEXT,
            previous_snapshot_id      TEXT,
            benchmarks                TEXT,
            created_at                TEXT NOT NULL,
            UNIQUE (session_date, revision)
        );

        CREATE TABLE IF NOT EXISTS adaptive_session_members (
            snapshot_id   TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            sector_id     TEXT,
            score_overall REAL,
            score_completeness REAL,
            requires_market_data_activation INTEGER NOT NULL DEFAULT 0,
            admitted_at   TEXT,
            PRIMARY KEY (snapshot_id, instrument_id)
        );

        CREATE TABLE IF NOT EXISTS adaptive_admission_decisions (
            decision_id        TEXT PRIMARY KEY,
            snapshot_id        TEXT NOT NULL,
            instrument_id      TEXT NOT NULL,
            as_of              TEXT NOT NULL,
            verdict            TEXT NOT NULL,
            outcome            TEXT,
            eligibility_status TEXT NOT NULL,
            eligibility_reasons TEXT,
            score_overall      REAL,
            score_completeness REAL,
            score_components   TEXT,
            unavailable_components TEXT,
            reasons            TEXT,
            capacity_at_decision  INTEGER,
            occupied_at_decision  INTEGER
        );

        CREATE TABLE IF NOT EXISTS adaptive_expiry_events (
            snapshot_id   TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            at            TEXT NOT NULL,
            reason        TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, instrument_id, at)
        );

        CREATE INDEX IF NOT EXISTS idx_adaptive_snapshots_session
            ON adaptive_session_snapshots (session_date, revision);
        CREATE INDEX IF NOT EXISTS idx_adaptive_decisions_snapshot
            ON adaptive_admission_decisions (snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_adaptive_members_instrument
            ON adaptive_session_members (instrument_id);
    """)
    conn.commit()


#: Tables this phase owns. Used by the safety tests to assert that
#: nothing else was touched.
ADAPTIVE_UNIVERSE_TABLES = (
    "adaptive_parent_universe_versions",
    "adaptive_session_snapshots",
    "adaptive_session_members",
    "adaptive_admission_decisions",
    "adaptive_expiry_events",
)
