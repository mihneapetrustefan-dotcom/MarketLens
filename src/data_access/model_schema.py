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
IF NOT EXISTS, plus one guarded migration that is a no-op once applied
(see `_relax_beats_all_baselines`).
"""

import sqlite3

#: The one definition of `model_evaluations`, used both to create the
#: table and to rebuild it in the migration below, so a rebuilt table
#: cannot drift from the declared one. `%s` is the table name: the
#: migration builds a copy under a temporary name before renaming it
#: into place.
#:
#: `beats_all_baselines` is deliberately NULLABLE and carries no
#: default. `ModelEvaluation.beats_all_baselines` is `Optional[bool]`
#: and is None when no baseline comparison was usable -- which is not
#: the same as having lost to the baselines, and
#: `src/autoresearch/observations.py` reads `beats_all_baselines = 0`
#: as exactly that loss. A default of 0 would make an insert that
#: omits the column assert a loss nobody measured, so there is none.
_MODEL_EVALUATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS %s (
        evaluation_id        TEXT PRIMARY KEY,
        trained_model_id     TEXT NOT NULL,
        model_qualified_id   TEXT NOT NULL,
        window_label         TEXT NOT NULL DEFAULT '',
        sample_size          INTEGER NOT NULL DEFAULT 0,
        cluster_count        INTEGER,
        effective_sample_size INTEGER,
        small_sample         INTEGER NOT NULL DEFAULT 0,
        beats_all_baselines  INTEGER,
        abstention_rate      REAL,
        metrics_json         TEXT NOT NULL DEFAULT '{}',
        notes_json           TEXT NOT NULL DEFAULT '[]',
        evaluated_at         TEXT
    )
"""

#: The name the migration rebuilds under. Dropped first, so a crash
#: part-way through a previous attempt cannot leave stale rows behind
#: to be copied forward.
_REBUILD_TABLE = "model_evaluations_rebuild"


def _create_model_evaluation_indexes(conn: sqlite3.Connection) -> None:
    """
    The indexes on `model_evaluations`, in one place.

    Dropping a table drops its indexes with it, so the migration has to
    recreate exactly these -- and calling the same function from both
    places is what guarantees "exactly".
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_model ON model_evaluations(trained_model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_beats ON model_evaluations(beats_all_baselines)")


def _relax_beats_all_baselines(conn: sqlite3.Connection) -> bool:
    """
    Drop the NOT NULL on `model_evaluations.beats_all_baselines`.

    WHY A REBUILD. The column was `INTEGER NOT NULL DEFAULT 0`, which
    forced a three-state value into two: an evaluation with no usable
    baseline comparison could only be recorded as having LOST. The
    declaration above fixes databases created from now on, but
    `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it
    was, and SQLite cannot drop a NOT NULL constraint with
    `ALTER TABLE`. An existing database therefore needs the documented
    table rebuild.

    Returns True when it migrated, False when there was nothing to do.
    Safe to call repeatedly: once the column is nullable it returns at
    the first check without touching the database.
    """
    columns = {row[1]: row for row in
               conn.execute("PRAGMA table_info(model_evaluations)")}
    target = columns.get("beats_all_baselines")
    if target is None:
        # No table, or a shape this migration does not know. Either way
        # there is nothing here to relax.
        return False
    if not target[3]:                       # row[3] is `notnull`
        return False

    # `PRAGMA foreign_keys` is a no-op inside a transaction, so it has
    # to be set before the rebuild opens one -- and restored afterwards
    # to whatever the caller had, not to a guessed default.
    foreign_keys_were_on = bool(
        conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    if foreign_keys_were_on:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS %s" % _REBUILD_TABLE)
        conn.execute(_MODEL_EVALUATIONS_DDL % _REBUILD_TABLE)

        # The copied column list is read back from the two tables rather
        # than written out here, so it cannot disagree with either of
        # them. A column the old table never had keeps its new default.
        shared = [name for name in
                  (row[1] for row in conn.execute(
                      "PRAGMA table_info(%s)" % _REBUILD_TABLE))
                  if name in columns]
        joined = ", ".join(shared)
        before = conn.execute(
            "SELECT COUNT(*) FROM model_evaluations").fetchone()[0]
        conn.execute("INSERT INTO %s (%s) SELECT %s FROM model_evaluations"
                     % (_REBUILD_TABLE, joined, joined))
        copied = conn.execute(
            "SELECT COUNT(*) FROM %s" % _REBUILD_TABLE).fetchone()[0]
        if copied != before:
            raise sqlite3.IntegrityError(
                "model_evaluations rebuild copied %d of %d rows; rolling back"
                % (copied, before))

        conn.execute("DROP TABLE model_evaluations")
        conn.execute("ALTER TABLE %s RENAME TO model_evaluations"
                     % _REBUILD_TABLE)
        _create_model_evaluation_indexes(conn)

        # Scoped to this table on purpose: an unrelated pre-existing
        # violation elsewhere in the database must not abort a rebuild
        # that is itself correct.
        problems = conn.execute(
            "PRAGMA foreign_key_check(model_evaluations)").fetchall()
        if problems:
            raise sqlite3.IntegrityError(
                "foreign key check failed after rebuild: %r" % (problems,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        if foreign_keys_were_on:
            conn.execute("PRAGMA foreign_keys=ON")
    return True


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

    conn.execute(_MODEL_EVALUATIONS_DDL % "model_evaluations")
    # Only does anything on a database that predates the nullable
    # column; see `_relax_beats_all_baselines`.
    _relax_beats_all_baselines(conn)

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
    _create_model_evaluation_indexes(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_eval ON model_baseline_comparisons(evaluation_id)")
    # A Signal looks its prediction up by id; replay and evaluation
    # scan by model or by observation.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_model ON predictions(trained_model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_observation ON predictions(observation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_cutoff ON predictions(information_cutoff)")

    conn.commit()
