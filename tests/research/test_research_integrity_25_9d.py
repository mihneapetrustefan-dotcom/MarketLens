"""
tests/research/test_research_integrity_25_9d.py
-----------------------------------------------------------
Phase 25.9D — research infrastructure integrity.

Every class here reproduces a defect found by an adversarial probe
before it was fixed, and asserts the fixed behaviour. The probes were
run against the unfixed code first; the numbers quoted in docstrings
are what they printed.

NOTHING HERE TOUCHES THE PROTECTED D20 TEST. The real ledger is
asserted byte-identical after every test that could reach it.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.autoresearch import cycle, governance
from src.autoresearch import hypotheses as hypothesis_layer
from src.autoresearch import queue as queue_layer
from src.challengers import evaluation
from src.data_access.autoresearch_schema import initialize_autoresearch_schema
from src.data_access.experiment_schema import initialize_experiment_schema
from src.domain.autoresearch_models import ResearchBudget
from src.domain.experiment_models import DatasetSnapshot, Hypothesis, HypothesisSource
from src.experiments import engine
from src.research import protected_ledger as L
from tests.autoresearch.test_research_loop import a_database as research_database
from tests.autoresearch.test_research_loop import a_hypothesis
from tests.challengers.test_challenger_lifecycle import BASE as CH_BASE
from tests.challengers.test_challenger_lifecycle import a_challenger
from tests.challengers.test_challenger_lifecycle import a_database as challenger_database
from tests.experiments.test_engine_and_safety import an_experiment, build_conn

import scripts.audit_research_integrity as audit

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _real_ledger_bytes():
    with open(L.DEFAULT_LEDGER, "rb") as handle:
        return handle.read()


class LedgerSafeCase(unittest.TestCase):
    def setUp(self):
        self.ledger_before = _real_ledger_bytes()

    def tearDown(self):
        self.assertEqual(_real_ledger_bytes(), self.ledger_before,
                         "a test modified the REAL protected-test ledger")


def insert_experience(conn, **fields):
    fields.setdefault("experience_class",
                      "correct_call" if fields.get("direction_result") == "hit"
                      else "wrong_call")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(trading_experiences)")]
    usable = {k: v for k, v in fields.items() if k in columns}
    conn.execute("INSERT INTO trading_experiences (%s) VALUES (%s)"
                 % (", ".join(usable), ", ".join("?" * len(usable))),
                 tuple(usable.values()))
    conn.commit()


def pinned(conn):
    """An experiment whose dataset is stamped with the current cutoff."""
    return an_experiment(dataset=DatasetSnapshot(
        data_cutoff=engine.current_data_cutoff(conn)))


def run_and_save(conn, experiment, **kwargs):
    engine.save_experiment(conn, experiment)
    record, result = engine.run(conn, experiment, **kwargs)
    engine.save_run(conn, record, result)
    return record, result


# ======================================================================
# F1 — the cache served stale results for changes behind the frontier
# ======================================================================

class TestResearchCacheCases(LedgerSafeCase):
    """
    §87's four cases. Reproduced before the fix: a revised
    `direction_result` moved the true effect from +0.236 to -0.097 and
    the cache still served +0.236 -- same cutoff, same fingerprint.
    """

    def test_case_a_unchanged_exact_dataset_may_hit(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        first, _ = run_and_save(conn, experiment)
        second, result = engine.run(conn, experiment)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.cached_from_run, first.run_id)
        self.assertEqual(second.cohort_digest, first.cohort_digest)
        self.assertTrue(any("cohort contents" in text for text in result.limitations))

    def test_case_b_new_eligible_records_behind_the_frontier_miss(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        early = (CH_BASE.replace(year=2026, month=6, day=3)).isoformat()
        for i in range(60):
            insert_experience(
                conn, experience_id="late-%03d" % i, memory_version="v1",
                kind="signal", created_at=early, subject_kind="signal",
                subject_id="late-sig-%03d" % i, horizon="5d", quality="validated",
                direction_result="hit", signal_strength=0.3, signal_confidence=0.4,
                expected_direction="long", asset_class="equity",
                available_at=early, information_cutoff=early)
        again = pinned(conn)
        self.assertEqual(again.fingerprint, experiment.fingerprint,
                         "the precondition of the defect: identity did not move")
        record, _ = engine.run(conn, again)
        self.assertFalse(record.cache_hit, "a stale result was served as current")
        self.assertEqual(record.rows_examined, 300)

    def test_case_c_revised_contents_miss_and_report_the_true_effect(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        _, stale = run_and_save(conn, experiment)
        conn.execute("UPDATE trading_experiences SET direction_result='hit' "
                     "WHERE signal_strength < 0.5")
        conn.commit()
        served, result = engine.run(conn, experiment)
        _, fresh = engine.run(conn, experiment, allow_cache=False)
        self.assertFalse(served.cache_hit)
        self.assertEqual(result.effect, fresh.effect)
        self.assertNotEqual(result.effect, stale.effect)

    def test_case_c_a_quality_reclassification_misses(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        conn.execute("UPDATE trading_experiences SET quality='rejected' "
                     "WHERE experience_id IN ('exp-0001','exp-0002')")
        conn.commit()
        record, _ = engine.run(conn, experiment)
        self.assertFalse(record.cache_hit)
        self.assertEqual(record.rows_examined, 238)

    def test_an_edit_outside_the_cohort_keeps_the_cache(self):
        """Invalidation is exact: a row the cohort excludes changes nothing."""
        conn = build_conn(240)
        conn.execute("UPDATE trading_experiences SET quality='rejected' "
                     "WHERE experience_id='exp-0005'")
        conn.commit()
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        conn.execute("UPDATE trading_experiences SET actual_return=9.9 "
                     "WHERE experience_id='exp-0005'")
        conn.commit()
        self.assertTrue(engine.run(conn, experiment)[0].cache_hit)

    def test_case_d_changed_definition_is_a_new_fingerprint(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        from src.domain.experiment_models import ArmSpec
        moved = an_experiment(
            experiment_id="exp-engine-0002",
            dataset=DatasetSnapshot(data_cutoff=engine.current_data_cutoff(conn)),
            candidate=ArmSpec(name="strength >= 0.8",
                              evaluator="signal_strength_threshold",
                              parameters={"threshold": 0.8}, complexity=2))
        self.assertNotEqual(moved.fingerprint, experiment.fingerprint)
        engine.save_experiment(conn, moved)
        self.assertFalse(engine.run(conn, moved)[0].cache_hit)

    def test_a_run_from_before_the_digest_is_never_a_cache_source(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        conn.execute("UPDATE experiment_runs SET cohort_digest=''")
        conn.commit()
        self.assertFalse(engine.run(conn, experiment)[0].cache_hit)

    def test_an_old_runs_table_gains_the_column(self):
        conn = sqlite3.connect(":memory:")
        # The exact pre-25.9D shape: every column but the digest.
        conn.execute("""CREATE TABLE experiment_runs (
            run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
            status TEXT NOT NULL, seed INTEGER NOT NULL DEFAULT 0,
            environment TEXT NOT NULL DEFAULT '',
            dataset_snapshot_id TEXT NOT NULL DEFAULT '',
            code_version TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '', started_at TEXT,
            completed_at TEXT, duration_seconds REAL,
            rows_examined INTEGER NOT NULL DEFAULT 0,
            cache_hit INTEGER NOT NULL DEFAULT 0, cached_from_run TEXT,
            error TEXT NOT NULL DEFAULT '',
            cancelled_reason TEXT NOT NULL DEFAULT '')""")
        conn.execute("INSERT INTO experiment_runs (run_id, experiment_id, status) "
                     "VALUES ('r','e','completed')")
        initialize_experiment_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(experiment_runs)")}
        self.assertIn("cohort_digest", columns)
        self.assertEqual(conn.execute("SELECT cohort_digest FROM experiment_runs"
                                      ).fetchone()[0], "")

    def test_the_digest_is_deterministic(self):
        conn = build_conn(120)
        rows = engine.load_cohort(conn, an_experiment())
        self.assertEqual(engine.cohort_digest(rows),
                         engine.cohort_digest(engine.load_cohort(conn, an_experiment())))
        self.assertNotEqual(engine.cohort_digest(rows), engine.cohort_digest(rows[1:]))


class TestChallengerCacheContents(LedgerSafeCase):
    """The challenger cache had the same key: fingerprint (with cutoff) and seed."""

    def test_revised_contents_are_not_served_from_the_cache(self):
        conn = challenger_database()
        challenger = a_challenger(conn)
        _run, first = evaluation.evaluate(conn, challenger)
        conn.execute("UPDATE trading_experiences SET direction_result='miss', "
                     "experience_class='wrong_call' WHERE horizon='3d'")
        conn.commit()
        run, second = evaluation.evaluate(conn, challenger)
        self.assertEqual(run["cache_hit"], 0)
        self.assertNotEqual(second.effect, first.effect)

    def test_an_unchanged_record_still_reuses(self):
        conn = challenger_database()
        challenger = a_challenger(conn)
        first, _ = evaluation.evaluate(conn, challenger)
        again, _ = evaluation.evaluate(conn, challenger)
        self.assertEqual(again["cache_hit"], 1)
        self.assertEqual(again["cohort_digest"], first["cohort_digest"])


class TestTheRecordedCutoffIsABound(LedgerSafeCase):
    """
    A challenger stamped with cutoff C was evaluated on 360 rows, 60 of
    them after C. A stored experiment re-run later did the same.
    """

    def test_an_experiment_never_reads_past_its_cutoff(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        later = "2099-01-01T00:00:00+00:00"
        insert_experience(conn, experience_id="future", memory_version="v1",
                          kind="signal", created_at=later, subject_kind="signal",
                          subject_id="future", horizon="5d", quality="validated",
                          direction_result="hit", available_at=later)
        rows = engine.load_cohort(conn, experiment)
        self.assertEqual(len(rows), 240)
        self.assertTrue(all(r["available_at"] <= experiment.dataset.data_cutoff
                            for r in rows))

    def test_a_challenger_never_reads_past_its_cutoff(self):
        conn = challenger_database(300)
        challenger = a_challenger(conn)
        later = (CH_BASE + timedelta(days=500)).isoformat()
        insert_experience(conn, experience_id="exp-late", memory_version="v1",
                          kind="signal", subject_kind="signal", subject_id="late",
                          horizon="3d", quality="validated", direction_result="miss",
                          created_at=later, available_at=later)
        rows = evaluation.load_cohort(conn, challenger)
        self.assertEqual(len(rows), 300)


# ======================================================================
# F2 — reclaim could requeue an item completed under it
# ======================================================================

class TestReclaimRace(LedgerSafeCase):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "queue.db")
        self.worker = sqlite3.connect(self.path)
        initialize_autoresearch_schema(self.worker)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.worker.execute(
            "INSERT INTO autoresearch_queue (queue_id, method_version, hypothesis_id, "
            "state, priority, reason, queued_at, started_at) "
            "VALUES ('qi-1','v1','h1','running',1,'',?,?)", (old, old))
        self.worker.commit()
        self.reclaimer = sqlite3.connect(self.path)

    def tearDown(self):
        self.worker.close()
        self.reclaimer.close()
        shutil.rmtree(self.dir)
        super().tearDown()

    def test_an_item_completed_between_select_and_update_stays_completed(self):
        worker = self.worker

        class Interleaved:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if sql.lstrip().startswith("UPDATE autoresearch_queue"):
                    worker.execute("UPDATE autoresearch_queue SET state='completed' "
                                   "WHERE queue_id='qi-1'")
                    worker.commit()
                return self.conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        reclaimed = queue_layer.reclaim_stale(Interleaved(self.reclaimer))
        self.assertEqual(reclaimed, [])
        state = self.reclaimer.execute("SELECT state FROM autoresearch_queue").fetchone()[0]
        self.assertEqual(state, "completed", "a finished item was queued to run again")

    def test_a_genuinely_abandoned_item_is_still_reclaimed(self):
        self.assertEqual(len(queue_layer.reclaim_stale(self.reclaimer)), 1)

    def test_two_workers_cannot_both_claim(self):
        self.worker.execute("UPDATE autoresearch_queue SET state='queued'")
        self.worker.commit()
        outcomes = [queue_layer.claim(self.worker, "qi-1"),
                    queue_layer.claim(self.reclaimer, "qi-1")]
        self.assertEqual(sorted(outcomes), [False, True])


# ======================================================================
# F3 — cache hits and retries inflated the comparison count
# ======================================================================

class TestComparisonsAreDistinctLooks(LedgerSafeCase):
    """Before the fix: one measurement read four times reported five."""

    def family_experiment(self, **overrides):
        return an_experiment(hypothesis=Hypothesis(
            statement="s", mechanism="m", expected_effect="e", population="p",
            metric="directional_accuracy", source=HypothesisSource.RESEARCHER,
            family_id="fam-1"), **overrides)

    def test_cache_hits_do_not_add_comparisons(self):
        conn = build_conn(240)
        experiment = self.family_experiment()
        for _ in range(4):
            run_and_save(conn, experiment)
        self.assertEqual(engine.family_statistics(conn, experiment)["comparisons"], 2)

    def test_a_forced_rerun_on_identical_rows_is_not_a_new_comparison(self):
        conn = build_conn(240)
        experiment = self.family_experiment()
        run_and_save(conn, experiment)
        _, result = run_and_save(conn, experiment, allow_cache=False)
        self.assertEqual(result.family_comparison_count, 1)

    def test_a_new_seed_is_a_new_comparison(self):
        """A seed can be shopped for, so it counts."""
        conn = build_conn(240)
        experiment = self.family_experiment()
        run_and_save(conn, experiment)
        _, result = run_and_save(conn, experiment, seed=99)
        self.assertEqual(result.family_comparison_count, 2)

    def test_challenger_run_count_ignores_cache_hits(self):
        conn = challenger_database()
        challenger = a_challenger(conn)
        for _ in range(3):
            evaluation.evaluate(conn, challenger)
        _, result = evaluation.evaluate(conn, challenger, allow_cache=False)
        self.assertEqual(result.family_run_count, 1)


# ======================================================================
# F6, F7 — protected windows
# ======================================================================

class TestProtectedWindows(LedgerSafeCase):

    def test_an_autoresearch_cycle_cannot_evaluate_inside_a_window(self):
        """
        Before the fix: 400 rows inside a window over the whole record,
        concluded "supported, promising".
        """
        conn = research_database()
        hypothesis = a_hypothesis(conn)
        hypothesis_layer.save(conn, [hypothesis])
        queue_layer.enqueue(conn, hypothesis, priority=1.0)
        governance.declare_window(conn, label="final", starts_at="2000-01-01",
                                  ends_at="2099-01-01")
        report = cycle.run_cycle(conn, apply=True,
                                 budget=ResearchBudget(max_experiments_per_cycle=1))
        self.assertEqual(report["conclusions"], [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM experiment_results"
                                      ).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM autoresearch_candidates"
                                      ).fetchone()[0], 0)
        error = conn.execute("SELECT error FROM experiment_runs").fetchone()[0]
        self.assertIn("protected", error.lower())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM autoresearch_window_usage"
                                      ).fetchone()[0], 0,
                         "a refused run was charged to the snooping ledger")

    def test_a_window_declared_after_a_run_blocks_the_cached_result(self):
        conn = build_conn(240)
        experiment = pinned(conn)
        run_and_save(conn, experiment)
        governance.declare_window(conn, label="final", starts_at="2026-07-01",
                                  ends_at="2026-07-10")
        record, result = engine.run(conn, experiment)
        self.assertIsNone(result)
        self.assertFalse(record.cache_hit)

    def test_the_challenger_cache_cannot_bypass_a_window(self):
        conn = challenger_database()
        challenger = a_challenger(conn)
        evaluation.evaluate(conn, challenger)
        governance.declare_window(conn, label="final", starts_at="2020-01-01",
                                  ends_at="2099-01-01")
        run, result = evaluation.evaluate(conn, challenger)
        self.assertIsNone(result)
        self.assertEqual(run["cache_hit"], 0)

    def test_training_rows_inside_a_window_are_refused_too(self):
        """Training on a reserved region is tuning against it."""
        conn = build_conn(240)
        governance.declare_window(conn, label="early", starts_at="2026-06-02",
                                  ends_at="2026-06-03")
        record, result = engine.run(conn, pinned(conn))
        self.assertIsNone(result)
        self.assertIn("protected", record.error.lower())

    def test_a_record_with_no_windows_is_unaffected(self):
        conn = build_conn(240)
        record, result = engine.run(conn, pinned(conn))
        self.assertIsNotNone(result)
        self.assertFalse(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='autoresearch_protected_windows'"
        ).fetchone(), "the guard created schema in the database it guards")

    def assertRefused(self, conn, start, end):
        with self.assertRaises(governance.ProtectedWindowRefused):
            governance.assert_window_allowed(conn, starts_at=start, ends_at=end)

    def test_bound_formats_no_longer_hide_an_overlap(self):
        """Raw string comparison let a same-day timestamp past a date bound."""
        conn = sqlite3.connect(":memory:")
        governance.declare_window(conn, label="d", starts_at="2026-08-15",
                                  ends_at="2026-08-27")
        self.assertRefused(conn, "2026-08-27T10:00:00+00:00", "2026-09-01T00:00:00+00:00")
        self.assertRefused(conn, "2026-08-01T00:00:00Z", "2026-08-15T00:00:00Z")
        self.assertRefused(conn, "2026-08-10", "2026-08-15")
        # 23:00 at UTC-5 is the next UTC day: genuinely outside.
        governance.assert_window_allowed(conn, starts_at="2026-08-27T23:00:00-05:00",
                                         ends_at="2026-08-30")
        governance.assert_window_allowed(conn, starts_at="2026-08-28",
                                         ends_at="2026-08-30")

    def test_an_unreadable_bound_fails_closed(self):
        conn = sqlite3.connect(":memory:")
        governance.declare_window(conn, label="d", starts_at="2026-08-15",
                                  ends_at="not a date")
        self.assertRefused(conn, "2020-01-01", "2020-01-02")

    def test_a_monitored_window_is_not_refused(self):
        conn = sqlite3.connect(":memory:")
        governance.declare_window(conn, label="m", starts_at="2026-08-15",
                                  ends_at="2026-08-27", policy="monitored")
        governance.assert_window_allowed(conn, starts_at="2026-08-20",
                                         ends_at="2026-08-21")


# ======================================================================
# F8 — a crash retry was charged as a second look
# ======================================================================

class TestRetryIsNotNewEvidence(LedgerSafeCase):

    def test_a_crashed_cycle_retried_adds_no_evidence(self):
        """Before the fix the retry left two snooping-ledger uses for one measurement."""
        conn = research_database()
        hypothesis = a_hypothesis(conn)
        hypothesis_layer.save(conn, [hypothesis])
        queue_layer.enqueue(conn, hypothesis, priority=1.0)
        with mock.patch.object(cycle, "interpret", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                cycle.run_cycle(conn, apply=True,
                                budget=ResearchBudget(max_experiments_per_cycle=1))
        conn.execute("UPDATE autoresearch_queue SET started_at='2000-01-01T00:00:00+00:00'")
        conn.commit()
        report = cycle.run_cycle(conn, apply=True,
                                 budget=ResearchBudget(max_experiments_per_cycle=1))
        self.assertEqual(len(report["reclaimed"]), 1)

        def count(table):
            return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

        self.assertEqual(count("autoresearch_window_usage"), 1)
        self.assertEqual(count("autoresearch_conclusions"), 1)
        self.assertLessEqual(count("autoresearch_candidates"), 1)
        hit = conn.execute("SELECT cache_hit FROM experiment_runs ORDER BY started_at DESC"
                           ).fetchone()[0]
        self.assertEqual(hit, 1, "the retry recomputed instead of reusing")


# ======================================================================
# F9 — leakage guard spelling
# ======================================================================

class TestLeakageGuardSpelling(unittest.TestCase):

    def test_case_space_and_prefix_do_not_hide_an_outcome_field(self):
        for key in ("Primary_Error", " actual_return", "DIRECTION_RESULT",
                    "outcome.primary_error"):
            with self.assertRaises(governance.LeakageRefused, msg=key):
                governance.assert_decision_time({key: 1})

    def test_unclassified_outcome_columns_are_now_refused(self):
        for key in ("actual_direction", "time_to_mfe_seconds",
                    "attribution_confidence", "attribution_severity",
                    "evidence_count"):
            with self.assertRaises(governance.LeakageRefused, msg=key):
                governance.assert_decision_time({key: 1})

    def test_decision_time_fields_still_pass(self):
        governance.assert_decision_time({"Event_Type": "earnings", "horizon": "3d"})


# ======================================================================
# The measured write boundary
# ======================================================================

def content_hashes(conn):
    hashes = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'"):
        digest = hashlib.sha256()
        for row in conn.execute('SELECT * FROM "%s" ORDER BY 1' % name):
            digest.update(repr(row).encode())
        hashes[name] = digest.hexdigest()
    return hashes


class TestContentBoundary(LedgerSafeCase):
    """
    The Phase 23 boundary test counts rows, so an UPDATE to a trading
    table is invisible to it. This one hashes contents.
    """

    def research(self, name):
        return audit.is_research_table(name)

    def test_a_cycle_changes_no_content_outside_research_scope(self):
        conn = research_database()
        before = content_hashes(conn)
        cycle.run_cycle(conn, apply=True,
                        budget=ResearchBudget(max_experiments_per_cycle=3))
        after = content_hashes(conn)
        moved = {n for n in set(before) | set(after) if before.get(n) != after.get(n)}
        self.assertTrue(moved, "nothing moved: the cycle did not run")
        self.assertEqual({n for n in moved if not self.research(n)}, set())

    def test_the_content_diff_sees_an_update_row_counts_cannot(self):
        conn = research_database()
        before = content_hashes(conn)
        counts = conn.execute("SELECT COUNT(*) FROM trading_experiences").fetchone()[0]
        conn.execute("UPDATE trading_experiences SET actual_return = actual_return + 1 "
                     "WHERE rowid = 1")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM trading_experiences"
                                      ).fetchone()[0], counts)
        self.assertNotEqual(content_hashes(conn)["trading_experiences"],
                            before["trading_experiences"])


# ======================================================================
# The integrity command
# ======================================================================

class TestIntegrityCommand(LedgerSafeCase):

    def test_every_negative_control_passes(self):
        outcomes = audit.negative_controls(datetime.now(timezone.utc))
        failed = [o["control"] for o in outcomes if not o["passed"]]
        self.assertEqual(failed, [])
        self.assertGreaterEqual(len(outcomes), 10)

    def test_the_audited_file_is_not_modified(self):
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "db.sqlite")
            conn = build_conn(50)
            disk = sqlite3.connect(path)
            conn.backup(disk)
            disk.close()
            conn.close()
            with open(path, "rb") as handle:
                before = handle.read()
            report = audit.audit(audit.open_copy(path), now=datetime.now(timezone.utc))
            self.assertIn("phase_25_9d", report)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), before)
            # Nor did the copy's schema creation reach the file.
            check = sqlite3.connect(path)
            self.assertFalse(check.execute("SELECT 1 FROM sqlite_master WHERE "
                                           "name='challenger_runs'").fetchone())
            check.close()
        finally:
            shutil.rmtree(folder)

    def test_the_real_research_code_writes_only_research_tables(self):
        self.assertEqual(audit.research_writes_outside_scope(), [])

    def test_the_real_ledger_reads_d20_unconsumed(self):
        state = audit.ledger_state(L.DEFAULT_LEDGER)
        self.assertTrue(state["intact"])
        self.assertEqual(state["d20_state"], "NOT_CONSUMED")


# ======================================================================
# D20 isolation
# ======================================================================

class TestD20IsUnreachableFromResearch(unittest.TestCase):

    def test_no_research_package_touches_labels_or_the_protected_test(self):
        needles = ("research_labels", "anchor-v2", "validate_d20", "check_d20",
                   "protected_ledger", "protected_tests")
        for package in audit.RESEARCH_PACKAGES:
            folder = os.path.join(ROOT, "src", package)
            for name in os.listdir(folder):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    source = handle.read()
                for needle in needles:
                    self.assertNotIn(needle, source, "src/%s/%s" % (package, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
