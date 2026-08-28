"""
src/data_access/fusion_schema.py
-------------------------------------
SQL persistence for Phase 5 (Event Fusion).

WHY THIS MODULE EXISTS
----------------------
FusionEngine holds everything in memory: canonical_events, reports,
decisions, contradictions, timeline, review_cases are plain dicts and
lists on the instance. That is correct for the engine itself — fusion
logic should not know about SQL — but it means nothing survives the
process. This module gives that output a durable home.

WHAT IS DELIBERATELY *NOT* DUPLICATED
--------------------------------------
Event reports are NOT copied here. A report is a Phase 4 artifact and
already lives in `events`; `canonical_event_reports` holds only the
link. Copying report bodies into a Phase 5 table would create exactly
the parallel data model the architecture rules forbid.

The distinction the schema enforces, and the reason it is separate
from `events` at all: an Event Report is one source's claim, a
Canonical Event is the occurrence those claims are about. One row in
`canonical_events` can reference many rows in `events`.

WHAT IS PRESERVED
-----------------
Fusion never deletes a report, and this schema never overwrites the
history of how a decision was reached. `fusion_decisions` keeps one
row per decision with its score and reason, so why a report was
attached to an event stays reconstructable. `fusion_timeline` keeps
the ordered record of what happened to each canonical event.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_fusion_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 5 tables and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS canonical_events (
            canonical_event_id       TEXT PRIMARY KEY,
            event_type               TEXT NOT NULL,
            category                 TEXT NOT NULL,
            subtype                  TEXT,
            title                    TEXT,
            geography_json           TEXT,
            attributes_json          TEXT NOT NULL DEFAULT '{}',
            first_reported_at        TEXT,
            last_updated_at          TEXT,
            event_time               TEXT,
            lifecycle_state          TEXT NOT NULL,
            corroboration_state      TEXT NOT NULL,
            independent_source_count INTEGER NOT NULL DEFAULT 0,
            total_report_count       INTEGER NOT NULL DEFAULT 0,
            has_contradictions       INTEGER NOT NULL DEFAULT 0,
            quality_confidence       REAL NOT NULL DEFAULT 0.0,
            fingerprint              TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS canonical_event_participants (
            canonical_event_id    TEXT NOT NULL,
            entity_id             TEXT NOT NULL,
            role                  TEXT NOT NULL,
            entity_type           TEXT NOT NULL DEFAULT 'company',
            resolution_confidence REAL,
            PRIMARY KEY (canonical_event_id, entity_id, role)
        )
    """)

    # The link, not a copy: report_id references events.event_id.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS canonical_event_reports (
            canonical_event_id TEXT NOT NULL,
            report_id          TEXT NOT NULL,
            PRIMARY KEY (canonical_event_id, report_id)
        )
    """)

    # decision_id is derived from report_id by the caller so that
    # re-running fusion rewrites the same row instead of appending a
    # near-duplicate decision every time.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fusion_decisions (
            decision_id        TEXT PRIMARY KEY,
            report_id          TEXT NOT NULL,
            canonical_event_id TEXT,
            state              TEXT NOT NULL,
            score              REAL,
            method             TEXT,
            reason             TEXT,
            candidate_count    INTEGER NOT NULL DEFAULT 0,
            decided_at         TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fusion_contradictions (
            contradiction_id   TEXT PRIMARY KEY,
            canonical_event_id TEXT NOT NULL,
            contradiction_type TEXT NOT NULL,
            field_name         TEXT,
            description        TEXT,
            detected_at        TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fusion_timeline (
            entry_id           TEXT PRIMARY KEY,
            canonical_event_id TEXT NOT NULL,
            entry_type         TEXT NOT NULL,
            occurred_at        TEXT,
            description        TEXT,
            report_id          TEXT,
            old_value          TEXT,
            new_value          TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fusion_review_cases (
            review_id          TEXT PRIMARY KEY,
            reason             TEXT NOT NULL,
            report_id          TEXT,
            canonical_event_id TEXT,
            description        TEXT,
            created_at         TEXT,
            resolved           INTEGER NOT NULL DEFAULT 0
        )
    """)

    # --- Indexes: one per named query pattern ---
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canonical_type ON canonical_events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canonical_event_time ON canonical_events(event_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canonical_corroboration ON canonical_events(corroboration_state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cer_report ON canonical_event_reports(report_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cep_entity ON canonical_event_participants(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_report ON fusion_decisions(report_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_event ON fusion_decisions(canonical_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contradictions_event ON fusion_contradictions(canonical_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_event ON fusion_timeline(canonical_event_id, occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_unresolved ON fusion_review_cases(resolved)")

    conn.commit()
