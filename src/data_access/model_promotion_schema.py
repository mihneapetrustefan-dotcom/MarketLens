"""
src/data_access/model_promotion_schema.py
---------------------------------------------------
The record of who promoted a model, when, and on what evidence.

WHY A TABLE AND NOT A STATUS FLAG
-------------------------------------
`trained_models.status` can say ACTIVE. It cannot say *why*, *who
decided*, *which evaluation they were looking at*, or *what the code
looked like at the time* — and those are the four questions asked
after a model turns out to have been wrong.

A status is the current answer. This table is the history, and the
history is what an audit needs. Phase 16 reached the same conclusion
for execution-level promotions (`promotion_requests`), and this mirrors
it deliberately rather than inventing a second shape for the same idea.

NOTHING HERE IS EVER DELETED
--------------------------------
A demotion appends a row; it does not erase the promotion. A model that
was active for three weeks and produced 400 predictions must still be
explicable afterwards, and "we removed the row" is not an explanation.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

from __future__ import annotations

import sqlite3


def initialize_model_promotion_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_promotions (
            promotion_id        TEXT PRIMARY KEY,
            trained_model_id    TEXT NOT NULL,
            model_qualified_id  TEXT NOT NULL DEFAULT '',
            label_name          TEXT NOT NULL DEFAULT '',

            -- 'promote' or 'demote'. Both are recorded; a demotion is
            -- an event with a reason, not the absence of a promotion.
            action              TEXT NOT NULL,
            from_status         TEXT NOT NULL DEFAULT '',
            to_status           TEXT NOT NULL DEFAULT '',

            -- WHO. Never defaulted, never inferred from the environment:
            -- an automated promotion is the thing this table exists to
            -- make impossible, and a blank approver would be one.
            approved_by         TEXT NOT NULL,
            reason              TEXT NOT NULL,

            -- The evidence that was on the table at the moment of the
            -- decision, frozen. The evaluation row can be superseded
            -- later; what the approver saw cannot change afterwards.
            evaluation_id       TEXT,
            metrics_json        TEXT NOT NULL DEFAULT '{}',
            beats_all_baselines INTEGER,
            effective_sample    INTEGER,
            deployable          INTEGER,

            -- Reproducibility: which data, which features, which label,
            -- which code.
            dataset_version     TEXT NOT NULL DEFAULT '',
            feature_set_version TEXT NOT NULL DEFAULT '',
            label_version       TEXT NOT NULL DEFAULT '',
            code_version        TEXT NOT NULL DEFAULT '',

            promoted_at         TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_model_promotions_model
        ON model_promotions (trained_model_id, promoted_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_model_promotions_time
        ON model_promotions (promoted_at)
    """)
    conn.commit()
