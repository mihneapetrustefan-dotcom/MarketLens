"""
src/data_access/experiment_schema.py
------------------------------------------------
Persistence for hypotheses, experiments, runs, results and artifacts.

FIVE TABLES (§72)
---------------------
`hypothesis_families`   variations of one idea, grouped
`experiments`           the immutable definition + its fingerprint
`experiment_runs`       one execution; several per definition
`experiment_results`    the comparison and the verdict
`experiment_artifacts`  references to anything large

WHAT IS NOT DUPLICATED (§72)
--------------------------------
No backtest table — Phase 12's `backtests`, `backtest_metrics` and
`backtest_trades` are referenced by id from `experiment_artifacts`, not
copied. No trained model, no prediction, no outcome, no attribution, no
memory. An experiment references what it used and stores only its own
comparison.

THE FINGERPRINT COLUMN
--------------------------
`experiments.fingerprint` hashes the hypothesis, both arms, the dataset
snapshot, the protocol AND the acceptance criteria. It is what makes
§32, §46 and §73 structural rather than procedural: an experiment that
has started refuses a changed fingerprint, so the goal posts cannot
move after the answer is known.

`experiment_runs.fingerprint` records the fingerprint the run actually
executed against. A run whose fingerprint differs from its
experiment's current one is evidence the definition was tampered with,
and the integrity check counts exactly that.

ARTIFACTS ARE REFERENCES (§35)
----------------------------------
Model files, prediction sets and backtest outputs are named by
location and checksum, never inlined. A relational row holding a
megabyte of pickled model is a table that cannot be queried and a
backup that cannot be restored.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS. Nothing here alters or drops an existing table.
"""

from __future__ import annotations

import sqlite3


def _add_missing_columns(conn) -> None:
    """
    Bring an existing `experiments` table up to the current columns.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already
    exists, so a database written before the arm descriptions were
    stored keeps the old shape and every INSERT naming the new columns
    fails. Adding them here is the whole migration: both are nullable
    with a default, so old rows stay valid and simply carry no prose.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
    if not have:
        return
    for column in ("baseline_description", "candidate_description"):
        if column not in have:
            conn.execute("ALTER TABLE experiments ADD COLUMN "
                         f"{column} TEXT NOT NULL DEFAULT ''")


def initialize_experiment_schema(conn: sqlite3.Connection) -> None:
    """Create the experiment tables. Idempotent and additive."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS hypothesis_families (
            family_id           TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            description         TEXT NOT NULL DEFAULT '',
            -- The shared idea these experiments are variations of.
            -- Grouping exists so that "we tested this fifty ways" is a
            -- query rather than an anecdote (§42, §43).
            core_statement      TEXT NOT NULL DEFAULT '',
            created_by          TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id       TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            name                TEXT NOT NULL,
            experiment_type     TEXT NOT NULL,
            status              TEXT NOT NULL,
            description         TEXT NOT NULL DEFAULT '',
            created_by          TEXT NOT NULL DEFAULT '',

            -- HYPOTHESIS (§5). `mechanism` is NOT NULL because a
            -- statement with no proposed reason is a pattern-match.
            family_id           TEXT,
            statement           TEXT NOT NULL,
            mechanism           TEXT NOT NULL,
            expected_effect     TEXT NOT NULL DEFAULT '',
            population          TEXT NOT NULL DEFAULT '',
            conditions_json     TEXT NOT NULL DEFAULT '{}',
            metric              TEXT NOT NULL DEFAULT 'directional_accuracy',
            minimum_detectable_effect REAL,
            hypothesis_source   TEXT NOT NULL DEFAULT 'researcher',
            -- Which memory pattern or recurring error prompted this
            -- (§21, §22). Provenance changes how a result should be
            -- read: a hypothesis mined from the same record it is
            -- tested against is more exposed to snooping.
            source_reference    TEXT,

            -- ARMS (§6, §7). `evaluator` is a REGISTERED NAME, never
            -- code — §80 forbids arbitrary execution, and the way to
            -- forbid it structurally is for the schema to be unable to
            -- express it.
            baseline_name       TEXT NOT NULL,
            baseline_evaluator  TEXT NOT NULL,
            baseline_params_json TEXT NOT NULL DEFAULT '{}',
            baseline_complexity INTEGER NOT NULL DEFAULT 1,
            baseline_description TEXT NOT NULL DEFAULT '',
            candidate_name      TEXT NOT NULL,
            candidate_evaluator TEXT NOT NULL,
            candidate_params_json TEXT NOT NULL DEFAULT '{}',
            candidate_complexity INTEGER NOT NULL DEFAULT 1,
            candidate_description TEXT NOT NULL DEFAULT '',
            changed_variables_json TEXT NOT NULL DEFAULT '[]',

            -- DATASET (§10) and VERSIONS (§13, §14, §15, §16, §74)
            dataset_snapshot_id TEXT NOT NULL DEFAULT '',
            dataset_json        TEXT NOT NULL DEFAULT '{}',
            dataset_version     TEXT NOT NULL DEFAULT '',
            feature_version     TEXT NOT NULL DEFAULT '',
            label_version       TEXT NOT NULL DEFAULT '',
            model_version       TEXT NOT NULL DEFAULT '',
            strategy_version    TEXT NOT NULL DEFAULT '',
            configuration_version TEXT NOT NULL DEFAULT '1',
            code_version        TEXT NOT NULL DEFAULT '',

            protocol_json       TEXT NOT NULL DEFAULT '{}',
            -- ACCEPTANCE CRITERIA (§46), inside the fingerprint. This
            -- is what stops success being defined after the result.
            criteria_json       TEXT NOT NULL DEFAULT '{}',
            limits_json         TEXT NOT NULL DEFAULT '{}',

            fingerprint         TEXT NOT NULL,
            notes_json          TEXT NOT NULL DEFAULT '[]',
            created_at          TEXT NOT NULL,
            started_at          TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_runs (
            run_id              TEXT PRIMARY KEY,
            experiment_id       TEXT NOT NULL,
            status              TEXT NOT NULL,
            -- Every source of randomness is seeded and the seed is
            -- stored (§63), so a rerun reproduces or the difference is
            -- real.
            seed                INTEGER NOT NULL DEFAULT 0,
            environment         TEXT NOT NULL DEFAULT '',
            dataset_snapshot_id TEXT NOT NULL DEFAULT '',
            code_version        TEXT NOT NULL DEFAULT '',
            -- The fingerprint this run actually executed. A mismatch
            -- with the experiment's current one means the definition
            -- was edited after the fact.
            fingerprint         TEXT NOT NULL DEFAULT '',
            started_at          TEXT,
            completed_at        TEXT,
            duration_seconds    REAL,
            rows_examined       INTEGER NOT NULL DEFAULT 0,
            -- Reuse is never silent (§61).
            cache_hit           INTEGER NOT NULL DEFAULT 0,
            cached_from_run     TEXT,
            error               TEXT NOT NULL DEFAULT '',
            cancelled_reason    TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_results (
            run_id              TEXT PRIMARY KEY,
            experiment_id       TEXT NOT NULL,
            metric              TEXT NOT NULL,

            baseline_is_json    TEXT NOT NULL DEFAULT '{}',
            candidate_is_json   TEXT NOT NULL DEFAULT '{}',
            baseline_oos_json   TEXT NOT NULL DEFAULT '{}',
            candidate_oos_json  TEXT NOT NULL DEFAULT '{}',

            -- The out-of-sample effect is THE number. `effect_in_sample`
            -- sits beside it so the gap between them — the signature of
            -- a candidate that fitted its training data — is visible
            -- without a second query.
            effect              REAL,
            effect_in_sample    REAL,
            effect_low          REAL,
            effect_high         REAL,
            interval_method     TEXT NOT NULL DEFAULT '',

            robust_slices       INTEGER NOT NULL DEFAULT 0,
            robust_slices_passing INTEGER NOT NULL DEFAULT 0,
            robustness_json     TEXT NOT NULL DEFAULT '{}',
            sensitivity_json    TEXT NOT NULL DEFAULT '{}',
            ablation_json       TEXT NOT NULL DEFAULT '{}',

            complexity_ratio    REAL,
            economically_significant INTEGER,

            -- Selection bias, carried with the verdict (§41, §42).
            family_experiment_count INTEGER NOT NULL DEFAULT 1,
            family_comparison_count INTEGER NOT NULL DEFAULT 1,

            -- 'pass' | 'fail' | 'inconclusive'. PASS means the
            -- predefined criteria were met — never "profitable" (§4).
            decision            TEXT NOT NULL,
            reasons_json        TEXT NOT NULL DEFAULT '[]',
            limitations_json    TEXT NOT NULL DEFAULT '[]',
            computed_at         TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_artifacts (
            artifact_id         TEXT PRIMARY KEY,
            run_id              TEXT NOT NULL,
            experiment_id       TEXT NOT NULL,
            -- 'backtest', 'model', 'predictions', 'metrics', 'config',
            -- 'log', 'plot'
            kind                TEXT NOT NULL,
            -- A reference, never the payload (§35).
            location            TEXT NOT NULL DEFAULT '',
            reference_id        TEXT,
            checksum            TEXT NOT NULL DEFAULT '',
            size_bytes          INTEGER,
            detail_json         TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_status
        ON experiments (status, experiment_type)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_family
        ON experiments (family_id, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_fingerprint
        ON experiments (fingerprint)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_run_experiment
        ON experiment_runs (experiment_id, started_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_run_fingerprint
        ON experiment_runs (fingerprint, status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_result_experiment
        ON experiment_results (experiment_id, decision)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_artifact_run
        ON experiment_artifacts (run_id, kind)
    """)
    _add_missing_columns(conn)
    conn.commit()
