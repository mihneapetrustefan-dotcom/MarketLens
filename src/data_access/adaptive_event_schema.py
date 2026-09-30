"""
src/data_access/adaptive_event_schema.py
------------------------------------------------------
What an event meant for a position, and whether it was already seen
(Phase 25.9I-F).

WHY THIS EARNS ITS PLACE
----------------------------
Novelty is the only part of this phase that CANNOT be recomputed. Every
other output -- relevance, direction, materiality, urgency -- is a pure
function of the event, the thesis and `as_of`, so it can be derived
again at any time. Whether a thesis has already reacted to an event is
a fact about history, and if it is not written down the system reacts
to the same acquisition every time the crawler runs.

That is the failure this schema exists to prevent, and it is why three
tables are added rather than none.

THREE TABLES
----------------
    adaptive_position_event_assessments  one row per (event revision,
                                         thesis, as_of): the verdict and
                                         every reason behind it
    adaptive_event_thesis_links          the consumption ledger -- has
                                         THIS thesis seen THIS revision
    adaptive_event_reassessment_requests what was asked for, and the
                                         market-data state it wanted

The link table is not redundant with the assessment table. The
assessment is the reasoning; the link is the fast answer to "have we
done this already", carrying the material fields needed to tell a
material revision from another syndicated copy without rehydrating a
whole assessment.

REVISION LINEAGE
--------------------
`canonical_events` has no `version` column, so a revision is identified
by `event_revision_key` -- a content hash over lifecycle state,
corroboration state, report count, contradictions and last update.
`previous_assessment_id` chains one assessment to the one it followed,
so a rumour-then-filing sequence is readable in order.

APPEND-ONLY AND IDEMPOTENT
------------------------------
Assessment ids derive from (event, revision, thesis, as_of), so
re-running the same assessment writes the same row and
`INSERT OR IGNORE` makes the repeat a no-op. Nothing is updated after
writing.

WHAT IS NEVER WRITTEN
-------------------------
No order, intent, fill, position or execution row. No target zone, no
horizon and no exit fraction -- those columns do not exist here,
because event intelligence never produces them. `is_executable` is
stored as a literal 0 so a future reader can see in the data that these
were never instructions.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "25.9I-F.1"


def initialize_adaptive_event_schema(conn: sqlite3.Connection) -> None:
    """Create the event-reassessment tables. Safe to call repeatedly."""
    conn.executescript("""
        -- The verdict for one event revision against one thesis.
        CREATE TABLE IF NOT EXISTS adaptive_position_event_assessments (
            assessment_id           TEXT PRIMARY KEY,
            canonical_event_id      TEXT NOT NULL,
            event_revision_key      TEXT NOT NULL,
            thesis_id               TEXT NOT NULL,
            instrument_id           TEXT NOT NULL,
            as_of                   TEXT NOT NULL,
            -- how the event reaches this position
            relation                TEXT NOT NULL DEFAULT 'unknown',
            participation_role      TEXT NOT NULL DEFAULT '',
            thesis_relevance        TEXT NOT NULL DEFAULT 'unknown',
            assumption_impacts      TEXT NOT NULL DEFAULT '',
            -- what it says
            direction               TEXT NOT NULL DEFAULT 'unknown',
            evidence_direction      TEXT NOT NULL DEFAULT 'neutral',
            materiality             TEXT NOT NULL DEFAULT 'unknown',
            materiality_score       REAL,
            conflict_state          TEXT NOT NULL DEFAULT 'unknown',
            urgency                 TEXT NOT NULL DEFAULT 'normal',
            consumption_status      TEXT NOT NULL DEFAULT 'new',
            -- confidence, kept decomposed; never one collapsed number
            confidence_entity_match REAL,
            confidence_extraction   REAL,
            confidence_fact         REAL,
            confidence_materiality  REAL,
            confidence_thesis_impact REAL,
            evidence_completeness   REAL,
            -- corroboration, with independence stated separately
            corroboration_state     TEXT NOT NULL DEFAULT 'unconfirmed',
            source_independence_known INTEGER NOT NULL DEFAULT 0,
            -- what follows from it
            strengthens_thesis      INTEGER NOT NULL DEFAULT 0,
            weakens_thesis          INTEGER NOT NULL DEFAULT 0,
            invalidates_thesis      INTEGER NOT NULL DEFAULT 0,
            target_reestimation_required   INTEGER NOT NULL DEFAULT 0,
            horizon_reestimation_required  INTEGER NOT NULL DEFAULT 0,
            partial_exit_review_required   INTEGER NOT NULL DEFAULT 0,
            data_integrity_review_required INTEGER NOT NULL DEFAULT 0,
            integrity_concerns      TEXT NOT NULL DEFAULT '',
            requested_market_data_state TEXT NOT NULL DEFAULT 'cold',
            is_stale                INTEGER NOT NULL DEFAULT 0,
            -- a stored constant: these were never instructions
            is_executable           INTEGER NOT NULL DEFAULT 0,
            reasons                 TEXT NOT NULL DEFAULT '',
            -- lineage, carried never derived
            previous_assessment_id  TEXT,
            evidence_id             TEXT,
            recommendation_id       TEXT,
            recommendation          TEXT NOT NULL DEFAULT '',
            lineage_id              TEXT,
            session_snapshot_id     TEXT,
            allocation_plan_id      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pea_thesis
            ON adaptive_position_event_assessments (thesis_id, as_of);
        CREATE INDEX IF NOT EXISTS idx_pea_event
            ON adaptive_position_event_assessments (canonical_event_id);

        -- The consumption ledger: has this thesis seen this revision?
        CREATE TABLE IF NOT EXISTS adaptive_event_thesis_links (
            canonical_event_id      TEXT NOT NULL,
            thesis_id               TEXT NOT NULL,
            event_revision_key      TEXT NOT NULL,
            assessment_id           TEXT NOT NULL,
            assessed_at             TEXT NOT NULL,
            consumption_status      TEXT NOT NULL DEFAULT 'new',
            -- the material fields, so a material revision can be told
            -- apart from another syndicated copy without rehydrating
            lifecycle_state         TEXT NOT NULL DEFAULT '',
            corroboration_state     TEXT NOT NULL DEFAULT '',
            total_report_count      INTEGER NOT NULL DEFAULT 0,
            has_contradictions      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (canonical_event_id, thesis_id, event_revision_key)
        );
        CREATE INDEX IF NOT EXISTS idx_etl_thesis
            ON adaptive_event_thesis_links (thesis_id);

        -- What was asked for. A request, never an instruction.
        CREATE TABLE IF NOT EXISTS adaptive_event_reassessment_requests (
            request_id              TEXT PRIMARY KEY,
            canonical_event_id      TEXT NOT NULL,
            thesis_id               TEXT NOT NULL,
            instrument_id           TEXT NOT NULL,
            as_of                   TEXT NOT NULL,
            urgency                 TEXT NOT NULL DEFAULT 'normal',
            requested_market_data_state TEXT NOT NULL DEFAULT 'cold',
            assessment_id           TEXT,
            is_executable           INTEGER NOT NULL DEFAULT 0,
            reasons                 TEXT NOT NULL DEFAULT '',
            lineage_id              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_err_thesis
            ON adaptive_event_reassessment_requests (thesis_id, as_of);
    """)
    conn.commit()
