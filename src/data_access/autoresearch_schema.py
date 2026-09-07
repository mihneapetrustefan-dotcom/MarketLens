"""
src/data_access/autoresearch_schema.py
------------------------------------------------
Phase 23 storage — the research record.

NAMING: every table is prefixed `autoresearch_`. Phase 6 already owns
`research_observations`, `research_features` and `research_labels`,
and they mean something else entirely (the modelling dataset). An
unprefixed `research_questions` beside them would read as part of that
system.

WHAT IS DELIBERATELY NOT HERE
---------------------------------
No experiment tables. Phase 22 owns `experiments`, `experiment_runs`,
`experiment_results` and `hypothesis_families`, and §27 is explicit
that Phase 23 must not build a second experiment system. The research
layer REFERENCES those by id and never writes them.

THE TWO GOVERNANCE TABLES ARE THE POINT
-------------------------------------------
`autoresearch_protected_windows` and `autoresearch_window_usage` are
what make §22, §23 and §25 enforceable rather than aspirational. A
researcher that can tune against the same held-out period forever will
eventually find something, and the only defence is a ledger that
counts how often each window has been touched and a region it is not
allowed to touch at all.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS, plus an additive column migration at the end.
"""

from __future__ import annotations

import sqlite3


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Additive migration for tables that already exist.

    CREATE TABLE IF NOT EXISTS does nothing to a table that is already
    there, so a database written by an earlier build keeps the old
    shape and every INSERT naming a newer column fails. Phase 22 hit
    exactly this. Columns added here are nullable with defaults, so
    old rows stay valid and simply carry nothing.
    """
    wanted = {
        "autoresearch_questions": [
            ("triage_reason", "TEXT NOT NULL DEFAULT ''"),
        ],
        "autoresearch_conclusions": [
            ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
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


def initialize_autoresearch_schema(conn: sqlite3.Connection) -> None:
    """Create the research tables. Idempotent and additive."""

    # ------------------------------------------------------------------
    # Observations — what the researcher noticed, and the rows proving it
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_observations (
            observation_id      TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            kind                TEXT NOT NULL,
            subject             TEXT NOT NULL DEFAULT '',
            statement           TEXT NOT NULL,
            sample_size         INTEGER NOT NULL DEFAULT 0,
            -- A list of {kind, reference, detail}. Never empty: an
            -- observation without evidence is an opinion (§4).
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            measures_json       TEXT NOT NULL DEFAULT '{}',
            source_kind         TEXT NOT NULL DEFAULT '',
            source_reference    TEXT NOT NULL DEFAULT '',
            observed_at         TEXT NOT NULL,
            PRIMARY KEY (observation_id, method_version)
        )
    """)

    # ------------------------------------------------------------------
    # Questions — with the triage decision, including "no"
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_questions (
            question_id         TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            title               TEXT NOT NULL,
            question            TEXT NOT NULL,
            description         TEXT NOT NULL DEFAULT '',
            source_type         TEXT NOT NULL,
            source_id           TEXT NOT NULL DEFAULT '',
            observation_id      TEXT NOT NULL DEFAULT '',
            -- The decision NOT to research something is a research
            -- decision and is stored with its reason (§6).
            triage              TEXT NOT NULL DEFAULT 'queued',
            triage_reason       TEXT NOT NULL DEFAULT '',
            priority            REAL NOT NULL DEFAULT 0,
            priority_json       TEXT NOT NULL DEFAULT '{}',
            cost_json           TEXT NOT NULL DEFAULT '{}',
            sample_size         INTEGER NOT NULL DEFAULT 0,
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,
            PRIMARY KEY (question_id, method_version)
        )
    """)

    # ------------------------------------------------------------------
    # Hypotheses — falsifiable claims, keyed by what they claim
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_hypotheses (
            hypothesis_id       TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            question_id         TEXT NOT NULL DEFAULT '',
            statement           TEXT NOT NULL,
            mechanism           TEXT NOT NULL,
            population          TEXT NOT NULL DEFAULT '',
            condition_json      TEXT NOT NULL DEFAULT '{}',
            expected_direction  TEXT NOT NULL DEFAULT '',
            -- What would make this fail, fixed before the test (§10).
            falsifiability_json TEXT NOT NULL DEFAULT '{}',
            -- Phase 22 family, reused rather than reinvented (§14).
            family_id           TEXT NOT NULL DEFAULT '',
            family_name         TEXT NOT NULL DEFAULT '',
            source              TEXT NOT NULL DEFAULT '',
            source_reference    TEXT NOT NULL DEFAULT '',
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            sample_size         INTEGER NOT NULL DEFAULT 0,
            -- A REGISTERED evaluator name. Never code (§29, §49).
            evaluator           TEXT NOT NULL DEFAULT '',
            parameters_json     TEXT NOT NULL DEFAULT '{}',
            baseline            TEXT NOT NULL DEFAULT '',
            author              TEXT NOT NULL DEFAULT 'system',
            -- Identity of the CLAIM, ignoring wording (§13).
            claim_fingerprint   TEXT NOT NULL DEFAULT '',
            -- The Phase 22 experiment this became, if it became one.
            experiment_id       TEXT,
            quality_problems_json TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,
            PRIMARY KEY (hypothesis_id, method_version)
        )
    """)

    # ------------------------------------------------------------------
    # Queue — with the states that mean "we decided not to"
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_queue (
            queue_id            TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            hypothesis_id       TEXT NOT NULL,
            question_id         TEXT NOT NULL DEFAULT '',
            cycle_id            TEXT,
            state               TEXT NOT NULL DEFAULT 'queued',
            priority            REAL NOT NULL DEFAULT 0,
            reason              TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT,
            queued_at           TEXT NOT NULL,
            started_at          TEXT,
            finished_at         TEXT
        )
    """)

    # ------------------------------------------------------------------
    # Cycles — one bounded pass of the loop, with its budget
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_cycles (
            cycle_id            TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            trigger             TEXT NOT NULL DEFAULT 'manual',
            actor               TEXT NOT NULL DEFAULT 'system',
            budget_json         TEXT NOT NULL DEFAULT '{}',
            observations_made   INTEGER NOT NULL DEFAULT 0,
            questions_raised    INTEGER NOT NULL DEFAULT 0,
            hypotheses_formed   INTEGER NOT NULL DEFAULT 0,
            experiments_run     INTEGER NOT NULL DEFAULT 0,
            conclusions_drawn   INTEGER NOT NULL DEFAULT 0,
            candidates_proposed INTEGER NOT NULL DEFAULT 0,
            duplicates_skipped  INTEGER NOT NULL DEFAULT 0,
            -- Why the cycle stopped. Always recorded: a loop that ends
            -- without saying why is indistinguishable from one that
            -- crashed (§52).
            termination_reason  TEXT NOT NULL DEFAULT '',
            runtime_seconds     REAL NOT NULL DEFAULT 0,
            rows_scanned        INTEGER NOT NULL DEFAULT 0,
            started_at          TEXT NOT NULL,
            finished_at         TEXT
        )
    """)

    # ------------------------------------------------------------------
    # Conclusions — negative results are stored exactly like positive
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_conclusions (
            conclusion_id       TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            hypothesis_id       TEXT NOT NULL,
            question_id         TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT NOT NULL DEFAULT '',
            cycle_id            TEXT,
            conclusion          TEXT NOT NULL,
            confidence          TEXT NOT NULL DEFAULT 'insufficient',
            effect              REAL,
            effect_in_sample    REAL,
            effect_low          REAL,
            effect_high         REAL,
            sample_size         INTEGER NOT NULL DEFAULT 0,
            reasons_json        TEXT NOT NULL DEFAULT '[]',
            limitations_json    TEXT NOT NULL DEFAULT '[]',
            warnings_json       TEXT NOT NULL DEFAULT '[]',
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            family_experiment_count INTEGER NOT NULL DEFAULT 1,
            promising           INTEGER NOT NULL DEFAULT 0,
            concluded_at        TEXT NOT NULL,
            PRIMARY KEY (conclusion_id, method_version)
        )
    """)

    # ------------------------------------------------------------------
    # Candidates — a record, never a deployment
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_candidates (
            candidate_id        TEXT NOT NULL,
            method_version      TEXT NOT NULL,
            candidate_type      TEXT NOT NULL,
            name                TEXT NOT NULL,
            hypothesis_id       TEXT NOT NULL DEFAULT '',
            conclusion_id       TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'proposed',
            -- §56: a candidate that does not name what it changes from
            -- cannot be reproduced or reviewed.
            base_version        TEXT NOT NULL DEFAULT '',
            changes_json        TEXT NOT NULL DEFAULT '{}',
            dataset_snapshot_id TEXT NOT NULL DEFAULT '',
            code_version        TEXT NOT NULL DEFAULT '',
            requires_review     INTEGER NOT NULL DEFAULT 1,
            review_reason       TEXT NOT NULL DEFAULT '',
            effect              REAL,
            created_at          TEXT NOT NULL,
            PRIMARY KEY (candidate_id, method_version)
        )
    """)

    # ------------------------------------------------------------------
    # Protected windows — the data the researcher may not tune against
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_protected_windows (
            window_id           TEXT PRIMARY KEY,
            label               TEXT NOT NULL DEFAULT '',
            starts_at           TEXT NOT NULL,
            ends_at             TEXT NOT NULL,
            -- 'protected' = never usable by autonomous research;
            -- 'monitored' = usable but every touch is counted (§22).
            policy              TEXT NOT NULL DEFAULT 'protected',
            reason              TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # Window usage — the data-snooping ledger
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_window_usage (
            usage_id            TEXT PRIMARY KEY,
            window_key          TEXT NOT NULL,
            hypothesis_id       TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT NOT NULL DEFAULT '',
            family_id           TEXT NOT NULL DEFAULT '',
            cycle_id            TEXT,
            used_at             TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # Audit — every research action, by whom, and why (§77)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_audit (
            audit_id            TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            -- 'human' | 'system' | 'llm'. Never inferred: an action
            -- whose actor is unknown cannot be audited.
            actor               TEXT NOT NULL,
            action              TEXT NOT NULL,
            question_id         TEXT NOT NULL DEFAULT '',
            hypothesis_id       TEXT NOT NULL DEFAULT '',
            experiment_id       TEXT NOT NULL DEFAULT '',
            cycle_id            TEXT,
            decision            TEXT NOT NULL DEFAULT '',
            reason              TEXT NOT NULL DEFAULT '',
            evidence_json       TEXT NOT NULL DEFAULT '[]',
            occurred_at         TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # Family state — dead ends and reactivation (§65, §66)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autoresearch_family_state (
            family_id           TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active',
            experiments         INTEGER NOT NULL DEFAULT 0,
            supported           INTEGER NOT NULL DEFAULT 0,
            rejected            INTEGER NOT NULL DEFAULT 0,
            inconclusive        INTEGER NOT NULL DEFAULT 0,
            best_effect         REAL,
            median_effect       REAL,
            reason              TEXT NOT NULL DEFAULT '',
            -- Why a depleted family may become interesting again: new
            -- data, a new regime, a new feature, a new model (§66).
            reactivation_reason TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL
        )
    """)

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_ar_question_triage "
        "ON autoresearch_questions (triage, priority DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ar_hypothesis_claim "
        "ON autoresearch_hypotheses (claim_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_ar_hypothesis_family "
        "ON autoresearch_hypotheses (family_id)",
        "CREATE INDEX IF NOT EXISTS idx_ar_queue_state "
        "ON autoresearch_queue (state, priority DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ar_conclusion_hypothesis "
        "ON autoresearch_conclusions (hypothesis_id)",
        "CREATE INDEX IF NOT EXISTS idx_ar_conclusion_experiment "
        "ON autoresearch_conclusions (experiment_id)",
        "CREATE INDEX IF NOT EXISTS idx_ar_candidate_status "
        "ON autoresearch_candidates (status)",
        "CREATE INDEX IF NOT EXISTS idx_ar_usage_window "
        "ON autoresearch_window_usage (window_key)",
        "CREATE INDEX IF NOT EXISTS idx_ar_audit_time "
        "ON autoresearch_audit (occurred_at DESC)",
    ):
        conn.execute(statement)

    _add_missing_columns(conn)
    conn.commit()
