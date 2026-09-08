"""
src/data_access/challenger_schema.py
----------------------------------------------
Phase 24 storage — challengers, their runs, their verdicts, and the
human decisions taken about them.

WHAT IS DELIBERATELY NOT HERE (§67)
---------------------------------------
No candidate table — Phase 23 owns `autoresearch_candidates`.
No experiment table — Phase 22 owns those.
No model or strategy registry — Phases 8 and 11 own those.
No backtest table — Phase 12 owns that.

This schema adds only what genuinely did not exist: the challenger
definition, its runs, its comparison results, its review decisions, and
a queue. Everything else is referenced by id.

WHY `challengers` IS KEYED ON (id, version)
-----------------------------------------------
§9: a change requires a new version, never a silent mutation. The
primary key makes that structural rather than a convention — writing a
changed definition under the same version is a constraint violation, so
the mistake cannot be made quietly.

`challenger_reviews` IS APPEND-ONLY BY DESIGN
-------------------------------------------------
A review is a human act with an actor, a timestamp and a reason (§69).
There is no UPDATE path: changing your mind means a second review row,
so the history of what was decided and when survives.

SAFE TO RUN REPEATEDLY: CREATE TABLE / INDEX IF NOT EXISTS plus an
additive column migration.
"""

from __future__ import annotations

import sqlite3


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Additive migration for tables that already exist.

    CREATE TABLE IF NOT EXISTS does nothing to a table already there,
    so a database written by an earlier build keeps the old shape and
    every INSERT naming a newer column fails. Phases 22 and 23 both hit
    this; the columns here are nullable with defaults so old rows stay
    valid and simply carry nothing.
    """
    wanted = {
        "challenger_results": [
            ("economic_note", "TEXT NOT NULL DEFAULT ''"),
            ("window_reuse_count", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "challengers": [
            ("experimental_basis", "INTEGER NOT NULL DEFAULT 1"),
        ],
    }
    for table, columns in wanted.items():
        have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            continue
        for name, declaration in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                             % (table, name, declaration))


def initialize_challenger_schema(conn: sqlite3.Connection) -> None:
    """Create the challenger tables. Idempotent and additive."""

    # ------------------------------------------------------------------
    # Definitions — one row per (challenger, version)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challengers (
            challenger_id       TEXT NOT NULL,
            version             INTEGER NOT NULL,
            method_version      TEXT NOT NULL,
            variant_type        TEXT NOT NULL,
            name                TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'proposed',

            -- Provenance (§7). An orphan challenger cannot be traced
            -- back to the evidence that justified it, so all four are
            -- required by `Challenger.validate()`.
            candidate_id        TEXT NOT NULL DEFAULT '',
            hypothesis_id       TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT NOT NULL DEFAULT '',
            conclusion_id       TEXT NOT NULL DEFAULT '',
            family_id           TEXT NOT NULL DEFAULT '',

            -- The baseline, pinned. A baseline that moves silently
            -- during evaluation turns a comparison into an anecdote.
            baseline_kind       TEXT NOT NULL DEFAULT '',
            baseline_name       TEXT NOT NULL DEFAULT '',
            baseline_version    TEXT NOT NULL DEFAULT '',
            baseline_json       TEXT NOT NULL DEFAULT '{}',

            -- What changed (§8), as structured values, never code.
            change_kind         TEXT NOT NULL DEFAULT '',
            change_summary      TEXT NOT NULL DEFAULT '',
            change_json         TEXT NOT NULL DEFAULT '{}',

            plan_json           TEXT NOT NULL DEFAULT '{}',
            limits_json         TEXT NOT NULL DEFAULT '{}',

            -- Versions (§68).
            dataset_cutoff      TEXT NOT NULL DEFAULT '',
            dataset_version     TEXT NOT NULL DEFAULT '',
            feature_version     TEXT NOT NULL DEFAULT '',
            label_version       TEXT NOT NULL DEFAULT '',
            model_version       TEXT NOT NULL DEFAULT '',
            strategy_version    TEXT NOT NULL DEFAULT '',
            code_version        TEXT NOT NULL DEFAULT '',

            -- A challenger built on a model that has not been promoted
            -- is research on research, and must say so (§75).
            experimental_basis  INTEGER NOT NULL DEFAULT 1,

            fingerprint         TEXT NOT NULL,
            notes_json          TEXT NOT NULL DEFAULT '[]',
            created_by          TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL,
            started_at          TEXT,
            PRIMARY KEY (challenger_id, version)
        )
    """)

    # ------------------------------------------------------------------
    # Runs — a definition may be evaluated more than once (§30)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenger_runs (
            run_id              TEXT PRIMARY KEY,
            challenger_id       TEXT NOT NULL,
            challenger_version  INTEGER NOT NULL,
            method_version      TEXT NOT NULL,
            -- 'research' | 'backtest' | 'paper'. Never defaulted to
            -- the permissive one (§31).
            environment         TEXT NOT NULL DEFAULT 'research',
            status              TEXT NOT NULL DEFAULT 'queued',
            seed                INTEGER NOT NULL DEFAULT 0,
            -- The fingerprint the run EXECUTED. A run whose fingerprint
            -- differs from its definition's is evidence the definition
            -- moved after the numbers existed (§10).
            fingerprint         TEXT NOT NULL DEFAULT '',
            dataset_cutoff      TEXT NOT NULL DEFAULT '',
            code_version        TEXT NOT NULL DEFAULT '',
            rows_examined       INTEGER NOT NULL DEFAULT 0,
            cache_hit           INTEGER NOT NULL DEFAULT 0,
            cached_from_run     TEXT,
            error               TEXT NOT NULL DEFAULT '',
            cancelled_reason    TEXT NOT NULL DEFAULT '',
            queued_at           TEXT NOT NULL,
            started_at          TEXT,
            completed_at        TEXT,
            duration_seconds    REAL
        )
    """)

    # ------------------------------------------------------------------
    # Results — the comparison and its verdict
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenger_results (
            run_id              TEXT PRIMARY KEY,
            challenger_id       TEXT NOT NULL,
            challenger_version  INTEGER NOT NULL,
            method_version      TEXT NOT NULL,
            metric              TEXT NOT NULL DEFAULT 'directional_accuracy',

            baseline_oos_json   TEXT NOT NULL DEFAULT '{}',
            challenger_oos_json TEXT NOT NULL DEFAULT '{}',
            effect              REAL,
            effect_in_sample    REAL,
            effect_low          REAL,
            effect_high         REAL,

            walk_forward_folds  INTEGER NOT NULL DEFAULT 0,
            walk_forward_favourable INTEGER NOT NULL DEFAULT 0,
            robust_slices       INTEGER NOT NULL DEFAULT 0,
            robust_favourable   INTEGER NOT NULL DEFAULT 0,
            -- Per-context results, preserved rather than averaged (§39).
            slices_json         TEXT NOT NULL DEFAULT '[]',
            sensitivity_json    TEXT NOT NULL DEFAULT '{}',

            complexity_ratio    REAL,
            economically_significant INTEGER,
            economic_note       TEXT NOT NULL DEFAULT '',

            -- Selection bias, carried with the verdict (§15, §22).
            family_challenger_count INTEGER NOT NULL DEFAULT 1,
            family_run_count    INTEGER NOT NULL DEFAULT 1,
            window_reuse_count  INTEGER NOT NULL DEFAULT 0,
            warnings_json       TEXT NOT NULL DEFAULT '[]',

            -- Six dimensions, stored separately. There is no total
            -- column, because a stored total gets sorted (§37).
            scorecard_json      TEXT NOT NULL DEFAULT '{}',
            decision            TEXT NOT NULL,
            reasons_json        TEXT NOT NULL DEFAULT '[]',
            limitations_json    TEXT NOT NULL DEFAULT '[]',
            computed_at         TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # Reviews — append-only human decisions (§34, §69)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenger_reviews (
            review_id           TEXT PRIMARY KEY,
            challenger_id       TEXT NOT NULL,
            challenger_version  INTEGER NOT NULL,
            run_id              TEXT,
            -- 'approved_for_paper' | 'rejected' | 'deferred'.
            -- There is no 'approved_for_production': this phase cannot
            -- express it (§35).
            outcome             TEXT NOT NULL,
            reviewer            TEXT NOT NULL,
            reason              TEXT NOT NULL,
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            reviewed_at         TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # Queue — the same atomic-claim shape Phase 23.5 established
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenger_queue (
            queue_id            TEXT PRIMARY KEY,
            challenger_id       TEXT NOT NULL,
            challenger_version  INTEGER NOT NULL,
            method_version      TEXT NOT NULL,
            state               TEXT NOT NULL DEFAULT 'queued',
            priority            REAL NOT NULL DEFAULT 0,
            reason              TEXT NOT NULL DEFAULT '',
            run_id              TEXT,
            queued_at           TEXT NOT NULL,
            started_at          TEXT,
            finished_at         TEXT
        )
    """)

    # ------------------------------------------------------------------
    # Audit — every challenger action (§69)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS challenger_audit (
            audit_id            TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            actor               TEXT NOT NULL,
            action              TEXT NOT NULL,
            challenger_id       TEXT NOT NULL DEFAULT '',
            challenger_version  INTEGER,
            run_id              TEXT,
            decision            TEXT NOT NULL DEFAULT '',
            reason              TEXT NOT NULL DEFAULT '',
            occurred_at         TEXT NOT NULL
        )
    """)

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_ch_status "
        "ON challengers (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ch_candidate "
        "ON challengers (candidate_id)",
        "CREATE INDEX IF NOT EXISTS idx_ch_family "
        "ON challengers (family_id)",
        "CREATE INDEX IF NOT EXISTS idx_ch_fingerprint "
        "ON challengers (fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_ch_run_challenger "
        "ON challenger_runs (challenger_id, challenger_version)",
        "CREATE INDEX IF NOT EXISTS idx_ch_run_fingerprint "
        "ON challenger_runs (fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_ch_result_challenger "
        "ON challenger_results (challenger_id, challenger_version)",
        "CREATE INDEX IF NOT EXISTS idx_ch_review_challenger "
        "ON challenger_reviews (challenger_id, challenger_version)",
        "CREATE INDEX IF NOT EXISTS idx_ch_queue_state "
        "ON challenger_queue (state, priority DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ch_audit_time "
        "ON challenger_audit (occurred_at DESC)",
    ):
        conn.execute(statement)

    _add_missing_columns(conn)
    conn.commit()
