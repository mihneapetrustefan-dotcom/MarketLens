"""
tests/data_access/test_model_evaluations_nullability.py
------------------------------------------------------------------
`model_evaluations.beats_all_baselines` must be able to hold NULL.

WHY THIS TABLE IS REBUILT AT ALL
-----------------------------------
`ModelEvaluation.beats_all_baselines` is `Optional[bool]`: True, False,
and None-because-no-baseline-comparison-was-usable are three different
facts. The column was `INTEGER NOT NULL DEFAULT 0`, so the third had to
be written as the second -- and `src/autoresearch/observations.py`
selects `WHERE beats_all_baselines = 0` and reports those rows as
models that LOST to their baselines. Storing a 0 there would have been
a fabricated measurement, so `scripts/train_models.py` has to be able
to write NULL.

`initialize_model_schema` builds the table with
`CREATE TABLE IF NOT EXISTS`, which leaves an existing table untouched,
and SQLite cannot drop a NOT NULL constraint with `ALTER TABLE`. The
tests below therefore exercise BOTH starting points: a database created
by the current schema, and a database still carrying the old
definition.

Everything here runs against temporary SQLite files. Nothing reads the
production database, and no model is trained, evaluated or promoted.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.model_schema import (
    initialize_model_schema, _relax_beats_all_baselines,
)

#: The table exactly as it was before the fix, NOT NULL and all. Copied
#: rather than imported on purpose: this is the historical shape, and it
#: must stay fixed here even when the live declaration changes again.
OLD_MODEL_EVALUATIONS_DDL = """
    CREATE TABLE model_evaluations (
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
"""

COLUMNS = ("evaluation_id", "trained_model_id", "model_qualified_id",
           "window_label", "sample_size", "cluster_count",
           "effective_sample_size", "small_sample", "beats_all_baselines",
           "abstention_rate", "metrics_json", "notes_json", "evaluated_at")

#: Two rows that must survive the rebuild byte for byte: one that beat
#: its baselines, one that lost. Every column is given a distinctive
#: value so a mis-ordered copy would be visible.
WON = ("ev-won", "tm-1", "ridge:v1", "single_chronological_split",
       120, 40, 40, 0, 1, 0.0,
       '{"mae": 0.011, "r_squared": 0.12}', '["clean"]',
       "2026-10-01T00:00:00+00:00")
LOST = ("ev-lost", "tm-2", "ridge:v1", "single_chronological_split",
        18, 4, 4, 1, 0, 0.25,
        '{"mae": 0.031, "r_squared": -0.197}', '["small sample"]',
        "2026-10-02T00:00:00+00:00")


def _table_info(conn, table="model_evaluations"):
    return {row[1]: row for row in
            conn.execute("PRAGMA table_info(%s)" % table)}


def _index_names(conn, table="model_evaluations"):
    return {row[1] for row in conn.execute("PRAGMA index_list(%s)" % table)}


class _Database(unittest.TestCase):
    """A temporary SQLite file, removed afterwards."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.remove(self.path)          # let sqlite create it

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def connect(self):
        return sqlite3.connect(self.path)

    def seed_old_schema(self, *rows):
        """A database as it was before the fix, with `rows` already in it."""
        conn = self.connect()
        conn.execute(OLD_MODEL_EVALUATIONS_DDL)
        conn.execute("CREATE INDEX idx_evals_model "
                     "ON model_evaluations(trained_model_id)")
        conn.execute("CREATE INDEX idx_evals_beats "
                     "ON model_evaluations(beats_all_baselines)")
        for row in rows:
            conn.execute("INSERT INTO model_evaluations (%s) VALUES (%s)"
                         % (", ".join(COLUMNS), ",".join("?" * len(COLUMNS))),
                         row)
        conn.commit()
        return conn


class TestCleanDatabase(_Database):
    """A database created by the current schema needs no migration."""

    def test_the_column_is_nullable_from_the_start(self):
        conn = self.connect()
        initialize_model_schema(conn)
        self.assertEqual(_table_info(conn)["beats_all_baselines"][3], 0,
                         "a freshly created column must not be NOT NULL")
        conn.close()

    def test_the_column_carries_no_default(self):
        # A DEFAULT 0 would make an insert that omits the column assert
        # a loss nobody measured.
        conn = self.connect()
        initialize_model_schema(conn)
        self.assertIsNone(_table_info(conn)["beats_all_baselines"][4])
        conn.close()

    def test_the_migration_reports_nothing_to_do(self):
        conn = self.connect()
        initialize_model_schema(conn)
        self.assertFalse(_relax_beats_all_baselines(conn),
                         "a clean database must not be rebuilt")
        conn.close()

    def test_all_three_states_can_be_stored(self):
        conn = self.connect()
        initialize_model_schema(conn)
        for evaluation_id, value in (("a", 1), ("b", 0), ("c", None)):
            conn.execute(
                "INSERT INTO model_evaluations "
                "(evaluation_id, trained_model_id, model_qualified_id, "
                " beats_all_baselines) VALUES (?, 'tm', 'm:v1', ?)",
                (evaluation_id, value))
        conn.commit()
        stored = dict(conn.execute(
            "SELECT evaluation_id, beats_all_baselines FROM model_evaluations"))
        self.assertEqual(stored, {"a": 1, "b": 0, "c": None})
        conn.close()


class TestOldDatabaseIsMigrated(_Database):
    """The case that matters: a database that predates the fix."""

    def test_the_old_schema_really_rejects_null(self):
        """The premise. Without this the migration proves nothing."""
        conn = self.seed_old_schema()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO model_evaluations "
                "(evaluation_id, trained_model_id, model_qualified_id, "
                " beats_all_baselines) VALUES ('x', 'tm', 'm:v1', NULL)")
        conn.close()

    def test_initialization_migrates_it(self):
        conn = self.seed_old_schema(WON, LOST)
        self.assertEqual(_table_info(conn)["beats_all_baselines"][3], 1)
        initialize_model_schema(conn)
        self.assertEqual(_table_info(conn)["beats_all_baselines"][3], 0)
        conn.close()

    def test_null_can_be_stored_afterwards(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        conn.execute(
            "INSERT INTO model_evaluations "
            "(evaluation_id, trained_model_id, model_qualified_id, "
            " beats_all_baselines) VALUES ('ev-unknown', 'tm', 'm:v1', NULL)")
        conn.commit()
        self.assertIsNone(conn.execute(
            "SELECT beats_all_baselines FROM model_evaluations "
            "WHERE evaluation_id = 'ev-unknown'").fetchone()[0])
        conn.close()

    def test_the_helper_reports_that_it_migrated(self):
        conn = self.seed_old_schema(WON)
        self.assertTrue(_relax_beats_all_baselines(conn))
        self.assertFalse(_relax_beats_all_baselines(conn),
                         "the second call must be a no-op")
        conn.close()


class TestExistingDataSurvives(_Database):

    def rows(self, conn):
        return conn.execute(
            "SELECT %s FROM model_evaluations ORDER BY evaluation_id"
            % ", ".join(COLUMNS)).fetchall()

    def test_every_column_of_every_row_is_preserved(self):
        conn = self.seed_old_schema(WON, LOST)
        before = self.rows(conn)
        initialize_model_schema(conn)
        self.assertEqual(self.rows(conn), before)
        conn.close()

    def test_the_zero_and_one_values_keep_their_meaning(self):
        conn = self.seed_old_schema(WON, LOST)
        initialize_model_schema(conn)
        stored = dict(conn.execute(
            "SELECT evaluation_id, beats_all_baselines FROM model_evaluations"))
        self.assertEqual(stored, {"ev-won": 1, "ev-lost": 0},
                         "a migrated 0 must stay 0, not become NULL")
        conn.close()

    def test_an_existing_zero_is_still_found_by_the_detector_query(self):
        # src/autoresearch/observations.py selects exactly this.
        conn = self.seed_old_schema(WON, LOST)
        initialize_model_schema(conn)
        found = [row[0] for row in conn.execute(
            "SELECT evaluation_id FROM model_evaluations "
            "WHERE beats_all_baselines = 0")]
        self.assertEqual(found, ["ev-lost"])
        conn.close()

    def test_a_null_is_not_counted_as_a_loss(self):
        """The whole point: unavailable must not read as "lost"."""
        conn = self.seed_old_schema(WON, LOST)
        initialize_model_schema(conn)
        conn.execute(
            "INSERT INTO model_evaluations "
            "(evaluation_id, trained_model_id, model_qualified_id, "
            " beats_all_baselines) VALUES ('ev-unknown', 'tm', 'm:v1', NULL)")
        conn.commit()
        losses = [row[0] for row in conn.execute(
            "SELECT evaluation_id FROM model_evaluations "
            "WHERE beats_all_baselines = 0")]
        self.assertEqual(losses, ["ev-lost"])
        self.assertNotIn("ev-unknown", losses)
        conn.close()

    def test_no_rows_are_lost_at_scale(self):
        extra = [("ev-%04d" % i, "tm-%d" % i, "ridge:v1", "w",
                  i, i, i, i % 2, i % 2, 0.1,
                  "{}", "[]", "2026-10-01T00:00:00+00:00")
                 for i in range(500)]
        conn = self.seed_old_schema(WON, LOST, *extra)
        before = conn.execute(
            "SELECT COUNT(*) FROM model_evaluations").fetchone()[0]
        initialize_model_schema(conn)
        after = conn.execute(
            "SELECT COUNT(*) FROM model_evaluations").fetchone()[0]
        self.assertEqual((before, after), (502, 502))
        conn.close()


class TestStructureSurvives(_Database):

    def test_only_the_one_constraint_changes(self):
        conn = self.seed_old_schema(WON)
        before = _table_info(conn)
        initialize_model_schema(conn)
        after = _table_info(conn)

        self.assertEqual(set(before), set(after), "column set changed")
        for name in before:
            if name == "beats_all_baselines":
                continue
            self.assertEqual(before[name], after[name],
                             "definition of %s changed" % name)
        # cid, name, type, notnull, dflt_value, pk
        self.assertEqual(after["beats_all_baselines"][2], "INTEGER")
        self.assertEqual(after["beats_all_baselines"][3], 0)
        self.assertEqual(after["beats_all_baselines"][5], 0)
        conn.close()

    def test_the_primary_key_survives(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        self.assertEqual(_table_info(conn)["evaluation_id"][5], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO model_evaluations "
                "(evaluation_id, trained_model_id, model_qualified_id) "
                "VALUES ('ev-won', 'tm', 'm:v1')")
        conn.close()

    def test_both_indexes_survive(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        names = _index_names(conn)
        self.assertIn("idx_evals_model", names)
        self.assertIn("idx_evals_beats", names)
        conn.close()

    def test_the_index_still_covers_the_migrated_column(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        covered = [row[2] for row in
                   conn.execute("PRAGMA index_info(idx_evals_beats)")]
        self.assertEqual(covered, ["beats_all_baselines"])
        conn.close()

    def test_no_temporary_table_is_left_behind(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("model_evaluations_rebuild", tables)
        self.assertIn("model_evaluations", tables)
        conn.close()

    def test_the_database_is_still_internally_consistent(self):
        conn = self.seed_old_schema(WON, LOST)
        initialize_model_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_foreign_key_enforcement_is_restored(self):
        # The rebuild has to switch enforcement off; it must put the
        # caller's setting back rather than a guessed default.
        conn = self.seed_old_schema(WON)
        conn.execute("PRAGMA foreign_keys=ON")
        initialize_model_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        conn.close()

    def test_enforcement_left_off_stays_off(self):
        conn = self.seed_old_schema(WON)
        conn.execute("PRAGMA foreign_keys=OFF")
        initialize_model_schema(conn)
        self.assertEqual(
            conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        conn.close()

    def test_the_other_phase_9_tables_are_created_alongside(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("trained_models", "model_evaluations",
                     "model_baseline_comparisons", "predictions"):
            self.assertIn(name, tables)
        conn.close()


class TestIdempotency(_Database):

    def test_initialization_can_be_run_repeatedly(self):
        conn = self.seed_old_schema(WON, LOST)
        for _ in range(3):
            initialize_model_schema(conn)
        self.assertEqual(_table_info(conn)["beats_all_baselines"][3], 0)
        self.assertEqual(dict(conn.execute(
            "SELECT evaluation_id, beats_all_baselines FROM model_evaluations")),
            {"ev-won": 1, "ev-lost": 0})
        conn.close()

    def test_repeated_initialization_does_not_touch_a_null(self):
        conn = self.seed_old_schema(WON)
        initialize_model_schema(conn)
        conn.execute(
            "INSERT INTO model_evaluations "
            "(evaluation_id, trained_model_id, model_qualified_id, "
            " beats_all_baselines) VALUES ('ev-unknown', 'tm', 'm:v1', NULL)")
        conn.commit()
        initialize_model_schema(conn)
        initialize_model_schema(conn)
        self.assertIsNone(conn.execute(
            "SELECT beats_all_baselines FROM model_evaluations "
            "WHERE evaluation_id = 'ev-unknown'").fetchone()[0])
        conn.close()

    def test_a_stale_rebuild_table_does_not_corrupt_the_copy(self):
        """A crash part-way through a previous attempt must not persist."""
        conn = self.seed_old_schema(WON, LOST)
        conn.execute("CREATE TABLE model_evaluations_rebuild "
                     "(evaluation_id TEXT, junk TEXT)")
        conn.execute("INSERT INTO model_evaluations_rebuild VALUES ('junk', 'x')")
        conn.commit()
        initialize_model_schema(conn)
        self.assertEqual(dict(conn.execute(
            "SELECT evaluation_id, beats_all_baselines FROM model_evaluations")),
            {"ev-won": 1, "ev-lost": 0})
        conn.close()

    def test_an_uncommitted_write_does_not_break_the_migration(self):
        # The rebuild needs its own transaction, so it has to cope with
        # a caller that already has one open.
        conn = self.seed_old_schema(WON)
        conn.execute(
            "INSERT INTO model_evaluations "
            "(evaluation_id, trained_model_id, model_qualified_id, "
            " beats_all_baselines) VALUES ('ev-pending', 'tm', 'm:v1', 1)")
        self.assertTrue(conn.in_transaction)
        initialize_model_schema(conn)
        self.assertEqual(_table_info(conn)["beats_all_baselines"][3], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM model_evaluations").fetchone()[0], 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
