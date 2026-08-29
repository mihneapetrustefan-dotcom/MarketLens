"""
src/data_access/model_schema.py
-------------------------------------
SQL persistence for Phase 9 (Quantitative Modeling) — trained models,
their evaluations, and the baseline comparisons that must always
accompany them.

BASELINE COMPARISONS GET THEIR OWN TABLE, NOT A JSON BLOB
------------------------------------------------------------
ModelingEngine refuses to produce a metric without its baselines
(MANDATORY_BASELINES is deliberately not configurable). Storing those
comparisons as a queryable table rather than a serialized blob keeps
that discipline alive at read time: "show me every evaluation that
beat both baselines" stays a SQL query, not an application-layer
unpacking step someone can skip.

WHAT IS PERSISTED, AND WHAT IS NOT
-------------------------------------
Trained model PARAMETERS are stored as JSON — the fitted coefficients,
which is what makes a model reproducible without refitting.

PREDICTIONS ARE PERSISTED — A CORRECTED DECISION
---------------------------------------------------
An earlier version of this schema deliberately did NOT store
predictions, reasoning that they are re-derivable from the stored
parameters plus the feature rows, so storing them would duplicate a
derivable artifact.

That reasoning was wrong once Phase 10 arrived. A Signal references
its originating prediction_id as part of its provenance chain; if
predictions exist only transiently in memory, that reference points
at nothing and the audit trail breaks precisely where it matters
most. Historical replay has the same problem: replaying a signal
means knowing what the model actually said at the time, not what a
refit would say today.

The volume argument was also weaker than it looked — a few hundred
rows per model run is negligible next to the price cache. Being
re-derivable is not the same as being reproducible: refitting could
differ after any library or data change, while a stored prediction
is what the system actually claimed.

NOTHING IN THIS SCHEMA REPRESENTS A DECISION
-----------------------------------------------
No position, no signal, no action — matching the engine's own
docstring (spec §7, §51). A prediction is an estimate; turning one
into a trade needs risk limits and portfolio context that belong to a
later phase and are not modelled here.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_model_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 9 tables and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trained_models (
            trained_model_id     TEXT PRIMARY KEY,
            model_id             TEXT NOT NULL,
            model_qualified_id   TEXT NOT NULL,
            name                 TEXT,
            task                 TEXT NOT NULL,
            family               TEXT NOT NULL,
            model_version        TEXT NOT NULL,
            label_name           TEXT NOT NULL,
            label_version        TEXT NOT NULL DEFAULT 'v1',
            feature_set_id       TEXT,
            feature_set_version  TEXT,
            dataset_version      TEXT,
            hyperparameters_json TEXT NOT NULL DEFAULT '{}',
            parameters_json      TEXT NOT NULL DEFAULT '{}',
            feature_names_json   TEXT NOT NULL DEFAULT '[]',
            train_start          TEXT,
            train_end            TEXT,
            train_sample_size    INTEGER NOT NULL DEFAULT 0,
            train_cluster_count  INTEGER,
            status               TEXT NOT NULL,
            training_notes_json  TEXT NOT NULL DEFAULT '[]',
            trained_at           TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_evaluations (
            evaluation_id        TEXT PRIMARY KEY,
            trained_model_id     TEXT NOT NULL,
            model_qualified_id   TEXT NOT NULL,
            window_label         TEXT NOT NULL DEFAULT '',
            sample_size          INTEGER NOT NULL DEFAULT 0,
            cluster_count        INTEGER,
            effective_sample_size INTEGER,
            small_sample         INTEGER NOT NULL DEFAULT 0,
            beats_all_baselines  INTEGER NOT NULL DEFAULT 0,
            abstention_rate      REAL,
            metrics_json         TEXT NOT NULL DEFAULT '{}',
            notes_json           TEXT NOT NULL DEFAULT '[]',
            evaluated_at         TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_baseline_comparisons (
            evaluation_id   TEXT NOT NULL,
            baseline_name   TEXT NOT NULL,
            metric_name     TEXT NOT NULL,
            baseline_score  REAL,
            model_score     REAL,
            PRIMARY KEY (evaluation_id, baseline_name, metric_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            prediction_id           TEXT PRIMARY KEY,
            trained_model_id        TEXT NOT NULL,
            model_qualified_id      TEXT NOT NULL,
            observation_id          TEXT NOT NULL,
            predicted_value         REAL,
            predicted_class         TEXT,
            class_probabilities_json TEXT,
            confidence              REAL,
            prediction_interval_low  REAL,
            prediction_interval_high REAL,
            uncertainty_basis       TEXT,
            information_cutoff      TEXT,
            feature_set_version     TEXT,
            predicted_at            TEXT,
            is_abstention           INTEGER NOT NULL DEFAULT 0,
            abstention_reason       TEXT
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_models_qualified ON trained_models(model_qualified_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_models_label ON trained_models(label_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_model ON model_evaluations(trained_model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_beats ON model_evaluations(beats_all_baselines)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_eval ON model_baseline_comparisons(evaluation_id)")
    # A Signal looks its prediction up by id; replay and evaluation
    # scan by model or by observation.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_model ON predictions(trained_model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_observation ON predictions(observation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_cutoff ON predictions(information_cutoff)")

    conn.commit()
