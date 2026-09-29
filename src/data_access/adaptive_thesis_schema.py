"""
src/data_access/adaptive_thesis_schema.py
----------------------------------------------------
Durable record of position theses and their revisions (Phase 25.9I-E).

WHY THIS LANDS NOW
----------------------
25.9I-A deferred thesis persistence deliberately, because the shape was
a guess: nobody knew yet whether revisions lived beside positions or in
their own history, or what a partial exit did to the record. 25.9I-E
answers both, so the tables are written against a known shape rather
than an imagined one.

THREE TABLES, EACH EARNING ITS PLACE
----------------------------------------
    adaptive_position_theses      one row per REVISION of a thesis,
                                  keyed by thesis_id. A revision is a
                                  new row, never an update, so the
                                  chain of belief is readable in full.
    adaptive_thesis_revisions     the transition itself: old -> new,
                                  its reason and its evidence. Stored
                                  separately because the question "what
                                  changed and why" is asked far more
                                  often than "what was the state".
    adaptive_exit_recommendations what the engine recommended, with the
                                  market-data state it would need.

WHAT IS NEVER WRITTEN
-------------------------
Positions, orders, intents, fills, targets or any table the canonical
Portfolio, Risk or Execution layers own. A thesis is a record of
reasoning, not a movement of anything. `src/adaptive/` imports none of
those packages.

APPEND-ONLY AND IDEMPOTENT
------------------------------
Ids derive from content (instrument + as_of + revision number), so
re-running the same reassessment writes the same rows and
`INSERT OR IGNORE` makes the repeat a no-op. Nothing updates a row that
has been written: the 09:20 thesis stays readable after the 14:05
revision replaced it.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "25.9I-E.1"


def initialize_adaptive_thesis_schema(conn: sqlite3.Connection) -> None:
    """Create the thesis tables. Safe to call repeatedly."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS adaptive_position_theses (
            thesis_id            TEXT PRIMARY KEY,
            instrument_id        TEXT NOT NULL,
            direction            TEXT NOT NULL,
            status               TEXT NOT NULL,
            revision             INTEGER NOT NULL,
            previous_thesis_id   TEXT,
            created_at           TEXT NOT NULL,
            as_of                TEXT,
            updated_at           TEXT,
            entry_price          REAL,
            entry_notional       REAL,
            target_low           REAL NOT NULL,
            target_high          REAL NOT NULL,
            invalidation_low     REAL NOT NULL,
            invalidation_high    REAL NOT NULL,
            horizon_calendar_days INTEGER,
            horizon_expires_at   TEXT,
            confidence           REAL,
            initial_confidence   REAL,
            remaining_fraction   REAL NOT NULL,
            realized_fraction    REAL NOT NULL,
            initial_summary      TEXT,
            current_summary      TEXT,
            risk_assumptions     TEXT,
            event_assumptions    TEXT,
            regime_assumptions   TEXT,
            evidence_references  TEXT,
            session_snapshot_id  TEXT,
            admission_decision_id TEXT,
            allocation_plan_id   TEXT,
            lineage_id           TEXT,
            position_id          TEXT,
            written_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adaptive_thesis_revisions (
            revision_id          TEXT PRIMARY KEY,
            previous_thesis_id   TEXT NOT NULL,
            revised_thesis_id    TEXT NOT NULL,
            instrument_id        TEXT NOT NULL,
            reason               TEXT NOT NULL,
            at                   TEXT NOT NULL,
            extends_target       INTEGER NOT NULL DEFAULT 0,
            tightens_protection  INTEGER NOT NULL DEFAULT 0,
            previous_target_low  REAL,
            previous_target_high REAL,
            revised_target_low   REAL,
            revised_target_high  REAL,
            previous_invalidation_low  REAL,
            previous_invalidation_high REAL,
            revised_invalidation_low   REAL,
            revised_invalidation_high  REAL,
            previous_remaining_fraction REAL,
            revised_remaining_fraction  REAL,
            evidence             TEXT,
            note                 TEXT,
            written_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS adaptive_exit_recommendations (
            recommendation_id    TEXT PRIMARY KEY,
            thesis_id            TEXT NOT NULL,
            instrument_id        TEXT NOT NULL,
            action               TEXT NOT NULL,
            at                   TEXT NOT NULL,
            exit_fraction        REAL,
            required_market_data_state TEXT NOT NULL,
            activation_reason    TEXT,
            freshness_max_age_seconds REAL,
            requires_realtime_marker  INTEGER NOT NULL DEFAULT 0,
            released_fraction    REAL,
            released_notional    REAL,
            reasons              TEXT,
            blocked_by_policy    TEXT,
            revision_id          TEXT,
            is_executable        INTEGER NOT NULL DEFAULT 0,
            written_at           TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_adaptive_theses_instrument
            ON adaptive_position_theses (instrument_id, revision);
        CREATE INDEX IF NOT EXISTS idx_adaptive_revisions_previous
            ON adaptive_thesis_revisions (previous_thesis_id);
        CREATE INDEX IF NOT EXISTS idx_adaptive_exit_thesis
            ON adaptive_exit_recommendations (thesis_id);
    """)
    conn.commit()


#: Tables this phase owns, for the safety tests.
ADAPTIVE_THESIS_TABLES = (
    "adaptive_position_theses",
    "adaptive_thesis_revisions",
    "adaptive_exit_recommendations",
)
