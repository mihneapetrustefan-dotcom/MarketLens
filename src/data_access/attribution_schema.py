"""
src/data_access/attribution_schema.py
-------------------------------------------------
Persistence for error attributions, their evidence, and the review
queue.

THREE TABLES, AND WHY NOT ONE (§54)
---------------------------------------
`error_attributions` is the conclusion. `attribution_evidence` is the
facts behind it, one row per fact. `attribution_review_queue` is the
cases a person should look at.

Evidence is a separate table rather than a JSON blob on the conclusion
because §22 requires every attribution to reference evidence, and a
blob makes "show me every attribution resting on a capture ratio" an
unanswerable question. The join is the point: evidence is queryable,
countable and auditable, and an orphan check becomes one SQL statement.

NOTHING HERE DUPLICATES PHASE 19 (§54)
------------------------------------------
No return, no MFE, no MAE and no price is stored again. An attribution
references `outcome_measurements` by its natural key and copies only
the handful of context columns needed to slice attributions without a
join — the same convention Phase 19 used, for the same reason.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS. Nothing here alters or drops an existing table.
"""

from __future__ import annotations

import sqlite3


def initialize_attribution_schema(conn: sqlite3.Connection) -> None:
    """Create the attribution tables. Idempotent and additive."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS error_attributions (
            -- IDENTITY (§56). The error type is part of the key because
            -- one outcome may legitimately carry several attributions
            -- (§19); the methodology version is part of it because a
            -- rule change must write new rows rather than rewrite a
            -- conclusion somebody has read (§44).
            subject_kind        TEXT NOT NULL,
            subject_id          TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            error_type          TEXT NOT NULL,

            -- 'primary' | 'contributing' (§20)
            role                TEXT NOT NULL,
            -- 'high' | 'medium' | 'low' | 'insufficient_evidence'.
            -- Ordinal, NOT a probability: nothing has ever checked how
            -- often an attribution turns out to be right (§21).
            confidence          TEXT NOT NULL,
            -- How much it mattered. Deliberately independent of
            -- confidence (§25).
            severity            TEXT NOT NULL,
            status              TEXT NOT NULL,
            -- 'observed' | 'hypothetical'. A counterfactual must never
            -- be readable as history (§24).
            observability       TEXT NOT NULL DEFAULT 'observed',

            summary             TEXT NOT NULL DEFAULT '',

            -- What was claimed and what happened, copied for reading
            -- without a join back to the outcome.
            expected_direction  TEXT NOT NULL DEFAULT '',
            expected_return     REAL,
            realized_return     REAL,
            deviation           REAL,

            -- Context for clustering (§28-§34).
            instrument_id       TEXT NOT NULL DEFAULT '',
            trained_model_id    TEXT,
            model_status        TEXT,
            strategy_id         TEXT,
            market_regime       TEXT,
            event_type          TEXT,
            confidence_score    REAL,
            strength            REAL,
            signal_status       TEXT,

            -- Which Phase 19 measurement this rests on, so the two
            -- versions can never be silently mismatched.
            outcome_method_version TEXT NOT NULL DEFAULT '',
            attributed_at       TEXT NOT NULL,

            PRIMARY KEY (subject_kind, subject_id, horizon,
                         method_version, error_type)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attribution_evidence (
            evidence_id         TEXT PRIMARY KEY,
            subject_kind        TEXT NOT NULL,
            subject_id          TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            error_type          TEXT NOT NULL,

            -- 'direction', 'magnitude', 'excursion', 'capture',
            -- 'data_quality', 'missing_input', 'caveat', ...
            kind                TEXT NOT NULL,
            -- The sentence a person reads.
            statement           TEXT NOT NULL,
            -- The table and column it came from, so it can be checked.
            source              TEXT NOT NULL DEFAULT '',
            -- The numbers, kept structured as well as formatted: an
            -- aggregate over evidence is useful, an aggregate over
            -- prose is not.
            value               REAL,
            comparison          REAL,
            detail_json         TEXT NOT NULL DEFAULT '{}',
            position            INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attribution_review_queue (
            review_id           TEXT PRIMARY KEY,
            subject_kind        TEXT NOT NULL,
            subject_id          TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            method_version      TEXT NOT NULL,

            -- Why a person is needed rather than a rule (§47).
            reason              TEXT NOT NULL,
            candidate_types     TEXT NOT NULL DEFAULT '[]',
            recommended_check   TEXT NOT NULL DEFAULT '',
            severity            TEXT NOT NULL DEFAULT 'info',
            -- 'open' | 'reviewed'. Never auto-closed: the queue exists
            -- because a rule could not decide, so a rule must not
            -- decide it is finished either.
            state               TEXT NOT NULL DEFAULT 'open',
            queued_at           TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_attr_subject
        ON error_attributions (subject_kind, subject_id, horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_attr_type
        ON error_attributions (error_type, role, horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_attr_model
        ON error_attributions (trained_model_id, error_type)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_evidence_attr
        ON attribution_evidence (subject_kind, subject_id, horizon,
                                 method_version, error_type)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_review_state
        ON attribution_review_queue (state, severity)
    """)
    conn.commit()
