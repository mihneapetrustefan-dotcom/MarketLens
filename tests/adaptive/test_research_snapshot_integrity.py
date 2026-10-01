"""
tests/adaptive/test_research_snapshot_integrity.py
--------------------------------------------------
Phase 25.9I-F.7: the new snapshot's identity, and the discipline around
the new holdout.

Two failures would make the recovery worthless even though the data is
sound, and both are tested here:

  * a new snapshot that can be mistaken for the old one, which destroys
    the old experiment's reproducibility as well as the new one's;
  * a holdout chosen after a fold count or a score was seen, which is
    the exact mistake Phase 25.9I-F.6 refused to make.
"""

import ast
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import research_snapshot as rs
from src.adaptive.corpus_versioning import (
    CORPUS_V1, SNAPSHOT_V1, file_digest, load_corpus_version, load_snapshot,
    snapshot_manifest, verify_corpus_hash, verify_snapshot_hash,
)
from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.domain.corpus_recovery_models import (
    ArtifactState, FoldFeasibility, HoldoutDecision, ResearchHandoff,
    ResearchSnapshot, manifest_hash,
)
from src.domain.model_models import ModelEvaluation
from src.domain.research_models import DatasetVersion

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SOURCE_DB = os.path.join(ROOT, "data", "marketlens.db")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
PREREG = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_PREREGISTRATION.md")
HANDOFF = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")
UTC = timezone.utc

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
#: The boundary F.6 froze, for snapshot v1. It must never move.
OLD_HOLDOUT = datetime(2026, 8, 15, 1, 23, 31, tzinfo=UTC)


def a_snapshot(**kw) -> ResearchSnapshot:
    base = dict(snapshot_id="research_snapshot_v2",
                corpus_version_id="canonical_corpus_v2",
                dataset_version=DatasetVersion(version="v2", created_at=NOW),
                created_at=NOW, code_commit="abc",
                as_of_cutoff=datetime(2026, 8, 30, tzinfo=UTC),
                observation_count=1056, cluster_count=301)
    base.update(kw)
    return ResearchSnapshot(**base)


def a_holdout(**kw) -> HoldoutDecision:
    base = dict(snapshot_id="research_snapshot_v2", rule="the final 20%",
                boundary=datetime(2026, 8, 18, tzinfo=UTC), decided_at=NOW,
                earliest_information_time=datetime(2026, 7, 2, 7, tzinfo=UTC),
                latest_information_time=datetime(2026, 8, 30, 14, 35, tzinfo=UTC))
    base.update(kw)
    return HoldoutDecision(**base)


# ======================================================================
# 21-23. The new snapshot has its own identity
# ======================================================================

class TestSnapshotIdentity(unittest.TestCase):

    def test_a_snapshot_must_name_the_corpus_it_came_from(self):
        with self.assertRaises(ValueError):
            a_snapshot(corpus_version_id="")

    def test_two_snapshots_from_different_corpora_do_not_collide(self):
        new = a_snapshot()
        old = a_snapshot(snapshot_id=SNAPSHOT_V1, corpus_version_id=CORPUS_V1,
                         dataset_version=DatasetVersion(version="v1",
                                                        created_at=NOW),
                         observation_count=581, cluster_count=244)
        self.assertFalse(new.masquerades_as(old))
        self.assertNotEqual(new.fingerprint, old.fingerprint)

    def test_a_new_snapshot_reusing_the_old_id_masquerades_as_it(self):
        old = a_snapshot(snapshot_id=SNAPSHOT_V1, corpus_version_id=CORPUS_V1)
        impostor = a_snapshot(snapshot_id=SNAPSHOT_V1,
                              corpus_version_id="canonical_corpus_v2")
        self.assertTrue(impostor.masquerades_as(old))

    def test_the_fingerprint_carries_every_component_version(self):
        snapshot = a_snapshot()
        self.assertIn("research_snapshot_v2", snapshot.fingerprint)
        self.assertIn("canonical_corpus_v2", snapshot.fingerprint)
        self.assertIn(snapshot.dataset_version.fingerprint(),
                      snapshot.fingerprint)

    def test_more_clusters_than_observations_is_refused(self):
        with self.assertRaises(ValueError):
            a_snapshot(observation_count=10, cluster_count=20)

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_the_real_snapshots_are_distinct_and_both_recorded(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            old = load_snapshot(conn, SNAPSHOT_V1)
            new = load_snapshot(conn, "research_snapshot_v2")
        finally:
            conn.close()
        self.assertIsNotNone(old)
        self.assertIsNotNone(new)
        self.assertFalse(new.masquerades_as(old))
        self.assertNotEqual(new.content_hash, old.content_hash)

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_feature_and_label_rows_belong_to_observations_of_this_snapshot(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            orphan_features = conn.execute("""
                SELECT COUNT(*) FROM research_features f
                WHERE NOT EXISTS (SELECT 1 FROM research_observations o
                                  WHERE o.observation_id = f.observation_id)
            """).fetchone()[0]
            orphan_labels = conn.execute("""
                SELECT COUNT(*) FROM research_labels l
                WHERE NOT EXISTS (SELECT 1 FROM research_observations o
                                  WHERE o.observation_id = l.observation_id)
            """).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(orphan_features, 0)
        self.assertEqual(orphan_labels, 0)


# ======================================================================
# 14-15, 19. The old snapshot survives intact
# ======================================================================

@unittest.skipUnless(os.path.exists(SOURCE_DB), "source corpus absent")
class TestOldSnapshotPreserved(unittest.TestCase):

    def test_the_old_snapshot_is_still_readable(self):
        conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM research_observations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 581, "snapshot v1 changed size")

    def test_the_old_snapshot_still_holds_exactly_its_own_clusters(self):
        conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            clusters = conn.execute(
                "SELECT COUNT(DISTINCT event_cluster_id) "
                "FROM research_observations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(clusters, 244)

    def test_the_old_experiment_still_references_the_old_data(self):
        """
        F.6's recorded evaluation must still point at the five-row test
        window it actually used -- that record is how its root cause was
        proved, and losing it would lose the proof.
        """
        conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT sample_size, cluster_count, effective_sample_size "
                "FROM model_evaluations").fetchall()
        finally:
            conn.close()
        self.assertTrue(rows, "the old evaluation disappeared")
        for sample, clusters, effective in rows:
            self.assertEqual((sample, clusters, effective), (5, 4, 4))

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered corpus absent")
    def test_the_recorded_v1_hashes_still_match_the_untouched_database(self):
        """
        The registry lives in the new file and describes the old one, so
        this comparison needs nothing to have been written to the
        protected artifact in order to prove it was not written to.
        """
        registry = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        try:
            recorded_corpus = load_corpus_version(registry, CORPUS_V1)
            recorded_snapshot = load_snapshot(registry, SNAPSHOT_V1)
            self.assertTrue(verify_corpus_hash(source, recorded_corpus))
            self.assertTrue(verify_snapshot_hash(source, recorded_snapshot))
        finally:
            registry.close()
            source.close()

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered corpus absent")
    def test_the_two_snapshots_live_in_different_files(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            old = load_snapshot(conn, SNAPSHOT_V1)
            new = load_snapshot(conn, "research_snapshot_v2")
        finally:
            conn.close()
        self.assertNotEqual(old.storage_location, new.storage_location)


# ======================================================================
# 24, 27-28. Maturity and effective sample, recomputed
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
class TestEffectiveSampleRecomputed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        cls.holdout = rs.deterministic_holdout(
            cls.conn, snapshot_id="research_snapshot_v2")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_open_horizon_maturity_is_computable(self):
        for horizon in ("d1", "d3", "d5", "d10"):
            counts = rs.effective_sample(self.conn, horizon)
            self.assertGreater(counts["rows"], 0, horizon)

    def test_effective_sample_is_distinct_clusters(self):
        counts = rs.effective_sample(self.conn, "d5")
        self.assertEqual(counts["effective_sample"], counts["clusters"])
        self.assertLessEqual(counts["clusters"], counts["rows"])

    def test_every_daily_horizon_clears_the_floor_in_the_research_region(self):
        for horizon in ("d1", "d3", "d5", "d10"):
            counts = rs.effective_sample(self.conn, horizon,
                                         before=self.holdout.boundary)
            self.assertGreater(counts["effective_sample"], MIN_EFFECTIVE_SAMPLE,
                               horizon)

    def test_the_floor_is_the_canonical_constant(self):
        self.assertIs(MIN_EFFECTIVE_SAMPLE,
                      ModelEvaluation.MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(MIN_EFFECTIVE_SAMPLE, 30)

    def test_a_stale_stored_evaluation_cannot_define_the_new_capacity(self):
        """
        Two defences. The new database holds no evaluation to misread,
        and the function that computes capacity never consults one.
        """
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM model_evaluations").fetchone()[0]
        self.assertEqual(stale, 0)

        path = os.path.join(ROOT, "src", "adaptive", "research_snapshot.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body[0].value.value = ""
        code = ast.unparse(tree)
        self.assertNotIn("model_evaluations", code)
        self.assertNotIn("effective_sample_size", code)

    def test_the_new_effective_sample_is_far_above_the_stale_four(self):
        counts = rs.effective_sample(self.conn, "d5",
                                     before=self.holdout.boundary)
        self.assertGreater(counts["effective_sample"], 4 * 10)

    def test_an_earlier_boundary_cannot_see_later_rows(self):
        early = rs.effective_sample(self.conn, "d5",
                                    before=datetime(2026, 7, 15, tzinfo=UTC))
        late = rs.effective_sample(self.conn, "d5")
        self.assertLess(early["rows"], late["rows"])
        self.assertLessEqual(early["clusters"], late["clusters"])

    def test_the_snapshot_manifest_reads_no_label_value(self):
        manifest = snapshot_manifest(self.conn)
        blob = json.dumps(manifest)
        self.assertNotIn("value_json", blob)
        for observation in manifest["observations"][:50]:
            self.assertEqual(set(observation),
                             {"id", "instrument", "cluster",
                              "information_cutoff", "quality"})


# ======================================================================
# 25-26. The protected horizon
# ======================================================================

class TestProtectedHorizonSealed(unittest.TestCase):

    def test_the_protected_horizon_is_never_eligible(self):
        self.assertTrue(rs.is_protected("d20"))
        self.assertNotIn("d20", rs.PROTECTED_HORIZONS[1:] + ("",))

    def test_asking_for_its_effective_sample_raises(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                rs.effective_sample(conn, "d20")
        finally:
            conn.close()

    def test_a_handoff_including_it_is_refused(self):
        with self.assertRaises(ValueError):
            ResearchHandoff(
                snapshot=a_snapshot(), holdout=a_holdout(),
                preregistration_path="p", preregistration_hash="h",
                eligible_horizons=("d5",),
                feasibility=(), effective_sample={"d5": 200},
                qualification_gate_reference="g", candidate_budget=16,
                purge_days={"d5": 7.0}, embargo_days=6.0,
                d20_excluded=False)

    def test_the_horizon_inventory_excludes_it(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE research_labels (name TEXT)")
        conn.executemany("INSERT INTO research_labels VALUES (?)",
                         [("d5.abnormal_return",), ("d20.abnormal_return",)])
        try:
            self.assertEqual(rs.horizon_ids(conn), ["d5"])
            self.assertEqual(rs.horizon_ids(conn, include_protected=True),
                             ["d20", "d5"])
        finally:
            conn.close()

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_recovery_quality_was_not_assessed_using_the_protected_horizon(self):
        """
        The modules that judge the recovery contain no reference to the
        protected label, the protected validator or anchor-v2.
        """
        for relative in (("src", "adaptive", "corpus_funnel.py"),
                         ("src", "adaptive", "corpus_versioning.py"),
                         ("src", "adaptive", "research_snapshot.py"),
                         ("src", "domain", "corpus_recovery_models.py"),
                         ("scripts", "recover_historical_corpus.py"),
                         ("scripts", "audit_historical_corpus.py"),
                         ("scripts", "freeze_research_handoff.py")):
            path = os.path.join(ROOT, *relative)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("validate_d20_reversal", source, path)
            self.assertNotIn("anchor_v2", source, path)
            self.assertNotIn("d20.abnormal_return", source, path)

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_the_frozen_handoff_excludes_it(self):
        with open(HANDOFF, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertTrue(payload["d20_excluded"])
        self.assertNotIn("d20", payload["eligible_horizons"])
        self.assertNotIn("d20", payload["effective_sample"])


# ======================================================================
# 29-33. Purge, embargo, folds, and the holdout
# ======================================================================

class TestFoldDesign(unittest.TestCase):

    def test_purge_and_embargo_are_both_represented(self):
        item = FoldFeasibility(horizon_id="d5", research_days=46,
                               purge_days=7, embargo_days=6, test_days=7,
                               step_days=7, min_folds=3)
        self.assertEqual(item.days_for_first_fold, 20)
        self.assertEqual(item.days_for_min_folds, 34)

    def test_a_zero_fold_design_is_rejected(self):
        thin = FoldFeasibility(horizon_id="d5", research_days=15,
                               purge_days=7, embargo_days=6, test_days=7,
                               step_days=7, min_folds=3)
        self.assertEqual(thin.max_folds, 0)
        self.assertFalse(thin.is_viable)
        self.assertEqual(thin.shortfall_days, 19)

    def test_the_f6_shortfalls_are_reproduced_exactly(self):
        """
        The same arithmetic that ended F.6, recomputed here. If this ever
        changed, the two phases would be describing different contracts.
        """
        expected = {"d1": (3.0, 15.0), "d3": (5.0, 17.0),
                    "d5": (7.0, 19.0), "d10": (14.0, 26.0)}
        for horizon, (purge, shortfall) in expected.items():
            item = FoldFeasibility(horizon_id=horizon, research_days=15,
                                   purge_days=purge, embargo_days=6,
                                   test_days=7, step_days=7, min_folds=3)
            self.assertEqual(item.max_folds, 0, horizon)
            self.assertEqual(item.shortfall_days, shortfall, horizon)

    def test_a_viable_design_reports_how_many_folds_fit(self):
        item = FoldFeasibility(horizon_id="d1", research_days=46,
                               purge_days=3, embargo_days=6, test_days=7,
                               step_days=7, min_folds=3)
        self.assertEqual(item.max_folds, 5)
        self.assertTrue(item.is_viable)

    def test_the_minimum_is_three_folds(self):
        self.assertEqual(MIN_VALID_FOLDS, 3)
        self.assertEqual(rs.PURGE_DAYS, {"d1": 3.0, "d3": 5.0,
                                         "d5": 7.0, "d10": 14.0})
        self.assertEqual(rs.EMBARGO_DAYS, 6.0)
        self.assertEqual(rs.TEST_DAYS, 7)
        self.assertEqual(rs.STEP_DAYS, 7)


class TestHoldoutDiscipline(unittest.TestCase):

    def test_a_boundary_chosen_after_a_model_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            a_holdout(observed_model_outcome=True)

    def test_a_boundary_without_a_stated_rule_is_refused(self):
        with self.assertRaises(ValueError):
            a_holdout(rule="   ")

    def test_a_boundary_outside_the_information_range_is_refused(self):
        with self.assertRaises(ValueError):
            a_holdout(boundary=datetime(2026, 9, 30, tzinfo=UTC))
        with self.assertRaises(ValueError):
            a_holdout(boundary=datetime(2026, 6, 1, tzinfo=UTC))

    def test_the_fraction_is_the_one_phase_25_9_used(self):
        self.assertEqual(rs.HOLDOUT_FRACTION, 0.20)

    def test_the_rule_text_says_the_fraction_predates_the_dataset(self):
        self.assertIn("Phase 25.9", rs.HOLDOUT_RULE_TEXT)
        self.assertIn("20%", rs.HOLDOUT_RULE_TEXT)

    def test_the_boundary_is_a_pure_function_of_the_information_range(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE research_observations "
                     "(information_cutoff TEXT, quality_level TEXT)")
        conn.executemany(
            "INSERT INTO research_observations VALUES (?, 'high')",
            [(f"2026-07-{day:02d}T00:00:00+00:00",) for day in range(1, 31)])
        try:
            first = rs.deterministic_holdout(conn, snapshot_id="s")
            second = rs.deterministic_holdout(conn, snapshot_id="s")
            self.assertEqual(first.boundary, second.boundary)
        finally:
            conn.close()

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_the_old_holdout_boundary_is_not_the_new_one(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            holdout = rs.deterministic_holdout(conn,
                                               snapshot_id="research_snapshot_v2")
        finally:
            conn.close()
        self.assertNotEqual(holdout.boundary, OLD_HOLDOUT)
        self.assertGreater(holdout.boundary, OLD_HOLDOUT)

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_the_new_design_is_adequate_and_says_so_explicitly(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            holdout = rs.deterministic_holdout(conn,
                                               snapshot_id="research_snapshot_v2")
            feasibility = rs.assess_feasibility(conn, holdout=holdout)
            adequacy = rs.holdout_adequacy(conn, holdout=holdout,
                                           feasibility=feasibility)
        finally:
            conn.close()
        self.assertEqual(adequacy["verdict"], "ADEQUATE")
        self.assertTrue(adequacy["non_empty_fold_design"])
        self.assertEqual(adequacy["horizons_not_viable"], [])

    @unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
    def test_a_horizon_that_fails_its_threshold_cannot_be_pre_registered(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            holdout = rs.deterministic_holdout(conn,
                                               snapshot_id="research_snapshot_v2")
            snapshot = load_snapshot(conn, "research_snapshot_v2")
            with self.assertRaises(ValueError):
                rs.build_handoff(conn, snapshot=snapshot, holdout=holdout,
                                 preregistration_path="p",
                                 preregistration_hash="h",
                                 candidate_budget=16,
                                 pre_registered_horizons=("d1", "d20"))
        finally:
            conn.close()


# ======================================================================
# 31, 34-35. The frozen handoff
# ======================================================================

@unittest.skipUnless(os.path.exists(HANDOFF), "handoff absent")
class TestFrozenHandoff(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(HANDOFF, encoding="utf-8") as handle:
            cls.payload = json.load(handle)

    def test_the_preregistration_hash_is_stable_and_matches_the_document(self):
        self.assertEqual(self.payload["preregistration_hash"],
                         file_digest(PREREG))
        self.assertEqual(file_digest(PREREG), file_digest(PREREG))

    def test_the_handoff_names_the_new_snapshot_not_the_old_one(self):
        self.assertEqual(self.payload["snapshot"]["snapshot_id"],
                         "research_snapshot_v2")
        self.assertEqual(self.payload["snapshot"]["corpus_version_id"],
                         "canonical_corpus_v2")
        self.assertNotEqual(self.payload["snapshot"]["snapshot_id"], SNAPSHOT_V1)

    def test_the_holdout_records_that_no_outcome_was_observed(self):
        self.assertFalse(self.payload["holdout"]["observed_model_outcome"])
        self.assertEqual(self.payload["holdout"]["snapshot_id"],
                         "research_snapshot_v2")

    def test_the_budget_is_fixed_at_sixteen_cells(self):
        self.assertEqual(self.payload["candidate_budget"], 16)

    def test_the_handoff_names_the_canonical_gate_rather_than_a_new_one(self):
        self.assertEqual(self.payload["qualification_gate_reference"],
                         "src.domain.model_models.ModelEvaluation.is_deployable")

    def test_every_eligible_horizon_has_a_declared_purge(self):
        for horizon in self.payload["eligible_horizons"]:
            self.assertIn(horizon, self.payload["purge_days"])

    def test_the_handoff_states_what_f6b_must_not_do(self):
        forbidden = " ".join(self.payload["f6b_must_not"]).lower()
        for phrase in ("holdout", "budget", "pool", "promote", "d20"):
            self.assertIn(phrase, forbidden)

    def test_intraday_is_deferred_with_a_stated_reason(self):
        deferred = self.payload["deferred_on_policy"]
        self.assertEqual(len(deferred["horizons"]), 4)
        self.assertIn("purge", deferred["reason"])

    def test_the_snapshot_is_eligible_for_research(self):
        self.assertEqual(self.payload["snapshot"]["state"],
                         ArtifactState.ELIGIBLE_FOR_RESEARCH.value)

    def test_a_handoff_whose_snapshot_is_not_eligible_is_not_research_ready(self):
        handoff = ResearchHandoff(
            snapshot=a_snapshot(state=ArtifactState.BUILDING),
            holdout=a_holdout(), preregistration_path="p",
            preregistration_hash="h", eligible_horizons=("d5",),
            feasibility=(FoldFeasibility(horizon_id="d5", research_days=46,
                                         purge_days=7, embargo_days=6,
                                         test_days=7, step_days=7,
                                         min_folds=3),),
            effective_sample={"d5": 227},
            qualification_gate_reference="g", candidate_budget=16,
            purge_days={"d5": 7.0}, embargo_days=6.0)
        self.assertFalse(handoff.is_research_ready)

    def test_a_handoff_with_an_unviable_horizon_is_not_research_ready(self):
        handoff = ResearchHandoff(
            snapshot=a_snapshot(state=ArtifactState.ELIGIBLE_FOR_RESEARCH,
                                content_hash="aa", frozen_at=NOW),
            holdout=a_holdout(), preregistration_path="p",
            preregistration_hash="h", eligible_horizons=("d5",),
            feasibility=(FoldFeasibility(horizon_id="d5", research_days=15,
                                         purge_days=7, embargo_days=6,
                                         test_days=7, step_days=7,
                                         min_folds=3),),
            effective_sample={"d5": 227},
            qualification_gate_reference="g", candidate_budget=16,
            purge_days={"d5": 7.0}, embargo_days=6.0)
        self.assertFalse(handoff.is_research_ready)

    def test_the_real_handoff_is_research_ready(self):
        self.assertTrue(self.payload["is_research_ready"])


# ======================================================================
# F.6B has not been run
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "recovered snapshot absent")
class TestF6BNotRun(unittest.TestCase):
    """F.7 freezes the environment. It does not do the research."""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_no_model_was_trained_against_the_new_snapshot(self):
        for table in ("trained_models", "model_evaluations",
                      "model_baseline_comparisons"):
            self.assertEqual(
                self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0, table)

    def test_no_prediction_or_signal_exists_against_the_new_snapshot(self):
        for table in ("predictions", "signals", "signal_contributions",
                      "recommendations"):
            self.assertEqual(
                self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0, table)

    def test_the_handoff_scripts_fit_nothing(self):
        for relative in (("scripts", "freeze_research_handoff.py"),
                         ("scripts", "recover_historical_corpus.py"),
                         ("src", "adaptive", "research_snapshot.py")):
            path = os.path.join(ROOT, *relative)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in ("train_and_evaluate", "ModelingEngine",
                              "evaluate_fold", "promote_model"):
                self.assertNotIn(forbidden, source, f"{path}: {forbidden}")


if __name__ == "__main__":
    unittest.main()
