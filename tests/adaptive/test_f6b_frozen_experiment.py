"""
tests/adaptive/test_f6b_frozen_experiment.py
--------------------------------------------------
Phase 25.9I-F.6B: the experiment was frozen before the result.

The tests that matter most are the ones that would fail if the finish
line had moved: the holdout boundary, the purge and embargo policy, the
candidate budget, and the rule that the same frozen plan cannot be run
twice and counted as independent evidence.

Everything here reads the committed artifacts. If the handoff, the
pre-registration and the snapshot ever disagree, these fail rather than
reconcile them.
"""

import ast
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import research_f6b as f6b
from scripts import research_model_quality as rmq
from scripts.research_multi_horizon import build_folds
from src.adaptive import research_snapshot as rs
from src.adaptive.corpus_versioning import file_digest, load_snapshot
from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.domain.model_models import ModelEvaluation

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
V1_DB = os.path.join(ROOT, "data", "marketlens.db")
HANDOFF = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_HANDOFF.json")
PREREG = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_PREREGISTRATION.md")
RESULTS = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_RESULTS.json")

UTC = timezone.utc

#: The values F.7 froze. Written down here so a change to either the
#: handoff or the code has to break a test rather than pass quietly.
FROZEN_HOLDOUT = "2026-08-18T00:00:00+00:00"
FROZEN_HORIZONS = ("d1", "d3", "d5", "d10")
FROZEN_DEFERRED = ("intraday_5m", "intraday_15m", "intraday_30m", "intraday_60m")
FROZEN_PURGE = {"d1": 3.0, "d3": 5.0, "d5": 7.0, "d10": 14.0}
FROZEN_EMBARGO = 6.0
FROZEN_BUDGET = 16
#: F.6's boundary, which belongs to snapshot v1 and must never move.
OLD_HOLDOUT = "2026-08-15T01:23:31+00:00"


def handoff() -> dict:
    with open(HANDOFF, encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================
# 1-8. The frozen plan is the plan that ran
# ======================================================================

@unittest.skipUnless(os.path.exists(HANDOFF), "handoff absent")
class TestFrozenPlanUnchanged(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.handoff = handoff()

    def test_the_preregistration_hash_matches_the_document(self):
        self.assertEqual(self.handoff["preregistration_hash"],
                         file_digest(PREREG))

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_snapshot_hash_matches_f7(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            snapshot = load_snapshot(conn, "research_snapshot_v2")
        finally:
            conn.close()
        self.assertEqual(snapshot.content_hash,
                         self.handoff["snapshot"]["content_hash"])

    def test_the_holdout_boundary_is_unchanged(self):
        self.assertEqual(self.handoff["holdout"]["boundary"], FROZEN_HOLDOUT)

    def test_the_old_f6_holdout_is_not_this_one(self):
        self.assertNotEqual(self.handoff["holdout"]["boundary"], OLD_HOLDOUT)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_holdout_rule_still_derives_the_frozen_boundary(self):
        """
        The boundary is not merely recorded, it is reproducible: the
        inherited 20% rule applied to the frozen snapshot still lands on
        the same instant.
        """
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            derived = rs.deterministic_holdout(
                conn, snapshot_id="research_snapshot_v2")
        finally:
            conn.close()
        self.assertEqual(derived.boundary.isoformat(), FROZEN_HOLDOUT)
        self.assertFalse(derived.observed_model_outcome)

    def test_the_purge_policy_is_unchanged(self):
        self.assertEqual({k: float(v) for k, v
                          in self.handoff["purge_days"].items()},
                         FROZEN_PURGE)
        self.assertEqual(rs.PURGE_DAYS, FROZEN_PURGE)

    def test_the_embargo_is_unchanged(self):
        self.assertEqual(float(self.handoff["embargo_days"]), FROZEN_EMBARGO)
        self.assertEqual(rs.EMBARGO_DAYS, FROZEN_EMBARGO)

    def test_the_candidate_budget_is_unchanged(self):
        self.assertEqual(self.handoff["candidate_budget"], FROZEN_BUDGET)

    def test_the_eligible_horizons_are_unchanged(self):
        self.assertEqual(tuple(self.handoff["eligible_horizons"]),
                         FROZEN_HORIZONS)

    def test_the_deferred_horizons_remain_deferred(self):
        deferred = tuple(self.handoff["deferred_on_policy"]["horizons"])
        self.assertEqual(set(deferred), set(FROZEN_DEFERRED))
        for horizon in deferred:
            self.assertNotIn(horizon, self.handoff["eligible_horizons"])

    def test_an_intraday_horizon_is_not_quietly_added(self):
        """
        Intraday clears every data threshold and is still deferred. The
        temptation this guards against is adding it because the corpus
        turned out large enough.
        """
        for horizon in FROZEN_DEFERRED:
            self.assertNotIn(horizon, FROZEN_HORIZONS)
            self.assertNotIn(horizon, rs.PURGE_DAYS,
                             "a purge horizon for intraday would mean a policy "
                             "was invented for it")


# ======================================================================
# 9-10. The protected horizon
# ======================================================================

class TestProtectedHorizonSealed(unittest.TestCase):

    def test_the_handoff_excludes_it(self):
        payload = handoff()
        self.assertTrue(payload["d20_excluded"])
        self.assertNotIn("d20", payload["eligible_horizons"])
        self.assertNotIn("d20", payload["effective_sample"])
        self.assertNotIn("d20", payload["purge_days"])

    def test_researching_it_raises(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(f6b.ProtectedHorizonRequested):
                f6b.research_horizon(conn, "d20",
                                     context={"eligible_horizons": ["d20"]},
                                     verbose=False)
        finally:
            conn.close()

    def test_a_horizon_outside_the_frozen_list_is_refused(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(f6b.FrozenPlanViolation):
                f6b.research_horizon(
                    conn, "intraday_5m",
                    context={"eligible_horizons": ["d1", "d3", "d5", "d10"]},
                    verbose=False)
        finally:
            conn.close()

    def test_no_protected_label_value_is_ever_queried(self):
        """
        The harness names the horizon only to exclude it -- it reads the
        handoff's `d20_excluded` flag and asserts it. What must be absent
        is any query for a protected LABEL or any route to the protected
        validator, so those are what is checked.
        """
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        for forbidden in ("d20.abnormal_return", "d20.raw_return",
                          "validate_d20_reversal", "anchor_v2",
                          "build_anchor_v2_labels", "return_60d",
                          "check_d20_readiness"):
            self.assertNotIn(forbidden, source, forbidden)
        # The only permitted appearance is the exclusion flag itself.
        self.assertIn("d20_excluded", source)

    def test_the_harness_never_reads_a_label_value_column(self):
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        self.assertNotIn("value_json", source)


def _without_prose(relative: str) -> str:
    """Source with docstrings blanked; other strings kept so SQL is checkable."""
    with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""
    return ast.unparse(tree)


# ======================================================================
# 11-13. The cell list is frozen, and runs once
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
class TestCellListFrozen(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.context = f6b.preflight(V2_DB)
        cls.cells = f6b.enumerate_cells(cls.context)

    def test_exactly_the_budgeted_number_of_cells_is_enumerated(self):
        self.assertEqual(len(self.cells), FROZEN_BUDGET)
        self.assertEqual(len(self.cells), self.context["candidate_budget"])

    def test_the_cells_are_the_cross_product_of_horizons_and_candidates(self):
        expected = {f"{h}:{c}" for h in FROZEN_HORIZONS for c in rmq.CANDIDATES}
        self.assertEqual({c["cell_id"] for c in self.cells}, expected)

    def test_enumeration_is_deterministic(self):
        again = f6b.enumerate_cells(self.context)
        self.assertEqual([c["cell_id"] for c in self.cells],
                         [c["cell_id"] for c in again])

    def test_a_budget_mismatch_is_refused_rather_than_reconciled(self):
        tampered = dict(self.context)
        tampered["candidate_budget"] = 20
        with self.assertRaises(f6b.FrozenPlanViolation):
            f6b.enumerate_cells(tampered)

    def test_adding_a_horizon_would_break_the_budget_check(self):
        """No cell N+1 can be appended because the first N failed."""
        tampered = dict(self.context)
        tampered["eligible_horizons"] = list(FROZEN_HORIZONS) + ["intraday_5m"]
        tampered["purge_days"] = {**self.context["purge_days"],
                                  "intraday_5m": 14.0}
        with self.assertRaises(f6b.FrozenPlanViolation):
            f6b.enumerate_cells(tampered)

    def test_the_candidate_set_is_phase_25_9s_four(self):
        self.assertEqual(rmq.CANDIDATES, ("C0", "C1", "C2", "C3"))

    def test_c0_and_c2_are_one_family_and_not_independent_evidence(self):
        c0, c2 = rmq.spec_for("C0"), rmq.spec_for("C2")
        self.assertIs(c0.family, c2.family)
        self.assertNotEqual(c0.hyperparameters, c2.hyperparameters)

    def test_c1_and_c3_are_one_family_on_two_feature_sets(self):
        c1, c3 = rmq.spec_for("C1"), rmq.spec_for("C3")
        self.assertIs(c1.family, c3.family)
        cells = {c["cell_id"]: c for c in self.cells}
        self.assertEqual(cells["d5:C1"]["feature_config"], "all_numeric")
        self.assertEqual(cells["d5:C3"]["feature_config"], "dense_only")


@unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
class TestRunsOnlyOnce(unittest.TestCase):

    def test_a_complete_execution_blocks_a_second_run(self):
        """
        Re-running a frozen experiment and keeping the better outcome is
        the failure this phase exists to prevent.
        """
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone()
            if not has_table:
                self.skipTest("the experiment has not been run yet")
            existing = f6b.find_execution(
                conn, preregistration_hash=handoff()["preregistration_hash"],
                snapshot_hash=handoff()["snapshot"]["content_hash"])
        finally:
            conn.close()
        if not existing or existing["status"] != "complete":
            self.skipTest("no complete execution recorded yet")
        with self.assertRaises(f6b.FrozenPlanViolation):
            f6b.run(V2_DB, verbose=False)

    def test_the_execution_marker_records_the_frozen_identity(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='research_executions'").fetchone():
                self.skipTest("the experiment has not been run yet")
            row = conn.execute("""
                SELECT preregistration_hash, snapshot_hash, holdout_boundary,
                       code_commit, status, cells_json
                FROM research_executions WHERE phase = '25.9I-F.6B'
                ORDER BY started_at DESC LIMIT 1""").fetchone()
        finally:
            conn.close()
        if not row:
            self.skipTest("no execution recorded")
        self.assertEqual(row[0], handoff()["preregistration_hash"])
        self.assertEqual(row[1], handoff()["snapshot"]["content_hash"])
        self.assertEqual(row[2], FROZEN_HOLDOUT)
        self.assertTrue(row[3])
        self.assertEqual(len(json.loads(row[5])), FROZEN_BUDGET)

    def test_a_partial_execution_cannot_read_as_complete(self):
        conn = sqlite3.connect(":memory:")
        try:
            f6b.initialize_execution_schema(conn)
            conn.execute("""
                INSERT INTO research_executions
                    (execution_id, phase, preregistration_path,
                     preregistration_hash, snapshot_id, snapshot_hash,
                     corpus_version_id, holdout_boundary, code_commit,
                     started_at, status, cells_json, completed_cells_json, note)
                VALUES ('x','25.9I-F.6B','p','ph','s','sh','c','b','commit',
                        '2026-10-04T00:00:00+00:00','failed','["a","b"]',
                        '["a"]','blew up')""")
            found = f6b.find_execution(conn, preregistration_hash="ph",
                                       snapshot_hash="sh")
        finally:
            conn.close()
        self.assertEqual(found["status"], "failed")
        self.assertNotEqual(found["status"], "complete")
        self.assertEqual(found["completed_cells"], ["a"])
        self.assertNotEqual(len(found["completed_cells"]), len(found["cells"]))


# ======================================================================
# 18-22. Temporal discipline
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
class TestTemporalDiscipline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from scripts.train_models import load_dataset
        cls.boundary = datetime.fromisoformat(FROZEN_HOLDOUT)
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            cls.data = {}
            for horizon in FROZEN_HORIZONS:
                names, X, Y, cutoffs, clusters, obs, quality = load_dataset(
                    conn, f"{horizon}.abnormal_return")
                cls.data[horizon] = {"cutoffs": cutoffs, "clusters": clusters,
                                     "rows": len(X)}
        finally:
            conn.close()

    def test_every_fold_tests_strictly_after_it_trains(self):
        for horizon in FROZEN_HORIZONS:
            cutoffs = self.data[horizon]["cutoffs"]
            folds = build_folds(cutoffs, FROZEN_PURGE[horizon],
                                holdout_start=self.boundary,
                                embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                if not fold["train_idx"]:
                    continue
                self.assertLess(max(cutoffs[i] for i in fold["train_idx"]),
                                fold["test_start"], f"{horizon} {fold['label']}")

    def test_no_fold_reaches_into_the_holdout(self):
        for horizon in FROZEN_HORIZONS:
            cutoffs = self.data[horizon]["cutoffs"]
            folds = build_folds(cutoffs, FROZEN_PURGE[horizon],
                                holdout_start=self.boundary,
                                embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                self.assertLessEqual(fold["test_end"], self.boundary,
                                     f"{horizon} {fold['label']}")
                for i in fold["train_idx"] + fold["test_idx"]:
                    self.assertLess(cutoffs[i], self.boundary)

    def test_purge_removes_rows_within_the_label_span(self):
        for horizon in FROZEN_HORIZONS:
            cutoffs = self.data[horizon]["cutoffs"]
            purge_days = FROZEN_PURGE[horizon]
            folds = build_folds(cutoffs, purge_days,
                                holdout_start=self.boundary,
                                embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                horizon_start = fold["test_start"] - timedelta(days=purge_days)
                for i in fold["train_idx"]:
                    # `<=`, not `<`. `splits.purge` removes a row only when
                    # T + horizon > test_start, strictly. A label that
                    # resolves exactly AT the boundary was determined
                    # entirely before the test period and leaks nothing, so
                    # keeping it is correct -- d5 wf1 has such a row at
                    # 2026-07-15T07:00Z.
                    self.assertLessEqual(
                        cutoffs[i], horizon_start,
                        f"{horizon} {fold['label']}: a training row's label "
                        f"window reaches into the test period")

    def test_embargo_widens_the_gap_beyond_purge_alone(self):
        horizon = "d1"
        cutoffs = self.data[horizon]["cutoffs"]
        with_embargo = build_folds(cutoffs, FROZEN_PURGE[horizon],
                                   holdout_start=self.boundary,
                                   embargo_days=FROZEN_EMBARGO)
        without = build_folds(cutoffs, FROZEN_PURGE[horizon],
                              holdout_start=self.boundary, embargo_days=0.0)
        self.assertLessEqual(
            sum(len(f["train_idx"]) for f in with_embargo),
            sum(len(f["train_idx"]) for f in without))
        self.assertTrue(any(f["embargoed"] > 0 for f in with_embargo))

    def test_the_fold_counts_are_the_ones_f7_projected(self):
        expected = {"d1": 5, "d3": 5, "d5": 4, "d10": 3}
        for horizon, count in expected.items():
            folds = build_folds(self.data[horizon]["cutoffs"],
                                FROZEN_PURGE[horizon],
                                holdout_start=self.boundary,
                                embargo_days=FROZEN_EMBARGO)
            valid = [f for f in folds if f["valid"]]
            self.assertEqual(len(valid), count, horizon)
            self.assertGreaterEqual(len(valid), MIN_VALID_FOLDS, horizon)

    def test_fold_construction_is_deterministic(self):
        for horizon in FROZEN_HORIZONS:
            first = build_folds(self.data[horizon]["cutoffs"],
                                FROZEN_PURGE[horizon],
                                holdout_start=self.boundary,
                                embargo_days=FROZEN_EMBARGO)
            second = build_folds(self.data[horizon]["cutoffs"],
                                 FROZEN_PURGE[horizon],
                                 holdout_start=self.boundary,
                                 embargo_days=FROZEN_EMBARGO)
            self.assertEqual([f["train_idx"] for f in first],
                             [f["train_idx"] for f in second], horizon)

    def test_d10_has_no_holdout_rows_and_that_was_known_before_any_result(self):
        """
        Found at plan time, not after a disappointing score: d10's latest
        matured label is 2026-08-15, three days before the boundary, so
        its holdout region is empty. The pre-registered protocol reserves
        one holdout evaluation for survivors, so no d10 candidate can
        reach qualification.

        The boundary is NOT moved to fix this. It is reported.
        """
        cutoffs = self.data["d10"]["cutoffs"]
        self.assertEqual(sum(1 for c in cutoffs if c >= self.boundary), 0)
        for horizon in ("d1", "d3", "d5"):
            self.assertGreater(
                sum(1 for c in self.data[horizon]["cutoffs"]
                    if c >= self.boundary), 0, horizon)


# ======================================================================
# 23-32. The gate is the canonical one and is not weakened
# ======================================================================

class TestQualificationGate(unittest.TestCase):

    def test_the_handoff_names_the_canonical_gate(self):
        self.assertEqual(handoff()["qualification_gate_reference"],
                         "src.domain.model_models.ModelEvaluation.is_deployable")

    def test_f6b_defines_no_gate_or_threshold_of_its_own(self):
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        import re
        self.assertIsNone(re.search(r"MIN_EFFECTIVE_SAMPLE\s*=\s*\d", source))
        self.assertIsNone(re.search(r"MIN_VALID_FOLDS\s*=\s*\d", source))
        self.assertIn("rs.MIN_EFFECTIVE_SAMPLE", source)
        self.assertIn("rs.MIN_VALID_FOLDS", source)

    def test_the_floors_are_the_canonical_constants(self):
        self.assertIs(MIN_EFFECTIVE_SAMPLE,
                      ModelEvaluation.MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(MIN_EFFECTIVE_SAMPLE, 30)
        self.assertEqual(MIN_VALID_FOLDS, 3)

    def test_a_small_sample_cannot_be_deployable(self):
        from src.domain.model_models import BaselineComparison
        evaluation = ModelEvaluation(
            evaluation_id="ev-test", trained_model_id="t",
            model_qualified_id="m:v1",
            window_label="w", sample_size=5, cluster_count=4,
            baseline_comparisons=[BaselineComparison(
                baseline_name="b", metric_name="mae",
                baseline_score=-0.05, model_score=-0.04)])
        self.assertTrue(evaluation.small_sample)
        self.assertIs(evaluation.is_deployable, False)

    def test_losing_a_required_baseline_cannot_be_deployable(self):
        from src.domain.model_models import BaselineComparison
        evaluation = ModelEvaluation(
            evaluation_id="ev-test", trained_model_id="t",
            model_qualified_id="m:v1",
            window_label="w", sample_size=500, cluster_count=200,
            baseline_comparisons=[
                BaselineComparison(baseline_name="a", metric_name="mae",
                                   baseline_score=-0.04, model_score=-0.03),
                BaselineComparison(baseline_name="b", metric_name="mae",
                                   baseline_score=-0.02, model_score=-0.03)])
        self.assertIs(evaluation.beats_all_baselines, False)
        self.assertIs(evaluation.is_deployable, False)

    def test_an_unjudgeable_evaluation_is_none_not_false(self):
        evaluation = ModelEvaluation(
            evaluation_id="ev-test", trained_model_id="t",
            model_qualified_id="m:v1",
            window_label="w", sample_size=500, cluster_count=200,
            baseline_comparisons=[])
        self.assertIsNone(evaluation.is_deployable)

    def test_none_does_not_qualify_a_candidate(self):
        gates = f6b.failed_gates({"status": f6b.SURVIVES, "reasons": []},
                                 {"evaluated": True, "is_deployable": None})
        self.assertIn("FAILED_OTHER_CANONICAL_GATE", gates)

    def test_an_unevaluated_holdout_does_not_qualify_a_candidate(self):
        gates = f6b.failed_gates({"status": f6b.SURVIVES, "reasons": []},
                                 {"evaluated": False,
                                  "reason": "no holdout rows"})
        self.assertIn("FAILED_OTHER_CANONICAL_GATE", gates)

    def test_survival_reasons_map_to_canonical_gate_names(self):
        baseline = f6b.failed_gates(
            {"status": "NOT QUALIFIED",
             "reasons": ["beats baselines in 1/5 folds; a strict majority is "
                         "required"]}, None)
        self.assertEqual(baseline, ["FAILED_BASELINE"])

        sample = f6b.failed_gates(
            {"status": "NOT QUALIFIED",
             "reasons": ["pooled effective sample 12 < 30"]}, None)
        self.assertEqual(sample, ["FAILED_SAMPLE"])

        calibration = f6b.failed_gates(
            {"status": "NOT QUALIFIED",
             "reasons": ["Brier not below base rate in 3/5 folds"]}, None)
        self.assertEqual(calibration, ["FAILED_CALIBRATION"])

        insufficient = f6b.failed_gates({"status": "INSUFFICIENT DATA"}, None)
        self.assertEqual(insufficient, ["FAILED_SAMPLE"])

    def test_surviving_the_research_region_is_not_qualification(self):
        """
        The two-stage contract: a survivor still has to pass one holdout
        evaluation through the canonical gate.
        """
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        self.assertIn("is_deployable", source)
        self.assertIn("holdout_evaluation", source)

    def test_the_harness_never_promotes(self):
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        for forbidden in ("promote_model", "PRODUCTION_ACTIVE",
                          "production_active", "ModelStatus.ACTIVE"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_a_refit_of_the_same_specification_is_not_a_second_model(self):
        """
        Independence is a property of the specification, not of the fit.
        C0 and C2 are one family at two regularisation strengths and
        cannot corroborate each other.
        """
        self.assertIs(rmq.spec_for("C0").family, rmq.spec_for("C2").family)
        self.assertIs(rmq.spec_for("C1").family, rmq.spec_for("C3").family)
        families = {rmq.spec_for(c).family for c in rmq.CANDIDATES}
        self.assertEqual(len(families), 2,
                         "four candidates, two genuinely distinct families")


# ======================================================================
# 33-48. Downstream stays shut
# ======================================================================

class TestDownstreamBoundary(unittest.TestCase):

    def test_the_harness_touches_no_broker_or_execution_path(self):
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        for forbidden in ("OrderIntent", "place_order", "submit_order",
                          "ExecutionEngine", "BrokerClient", "PaperBroker",
                          "IBKRClient", "market_snapshot"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_harness_imports_no_execution_or_broker_module(self):
        with open(os.path.join(ROOT, "scripts", "research_f6b.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for prefix in ("src.execution", "src.brokers", "src.paper",
                       "src.trading", "src.ibkr", "ib_insync", "ibapi",
                       "src.portfolio", "src.risk", "src.capture"):
            for name in imported:
                self.assertFalse(name == prefix or name.startswith(prefix + "."),
                                 f"imports {name}")

    def test_the_harness_makes_no_market_data_request(self):
        """
        Checked against imports rather than substrings: the harness
        legitimately reports `market_data_requests: 0`, and a blunt
        search for "requests" flags its own accounting.
        """
        with open(os.path.join(ROOT, "scripts", "research_f6b.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("requests", "urllib", "httpx", "aiohttp", "socket",
                          "polygon", "finnhub", "openai", "anthropic"):
            self.assertNotIn(forbidden, imported, forbidden)

    def test_signal_and_confidence_thresholds_are_untouched(self):
        source = _without_prose(os.path.join("scripts", "research_f6b.py"))
        for forbidden in ("min_signal_confidence", "min_edge",
                          "confidence_floor"):
            self.assertNotIn(forbidden, source, forbidden)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_no_model_was_activated_in_the_snapshot(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM trained_models "
                "WHERE status = 'active'").fetchone()[0]
            orders = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("order_intents", "execution_orders",
                                   "paper_orders", "simulated_orders"))
        finally:
            conn.close()
        self.assertEqual(active, 0)
        self.assertEqual(orders, 0)

    @unittest.skipUnless(os.path.exists(V1_DB), "production snapshot absent")
    def test_the_production_snapshot_was_not_touched_by_research(self):
        conn = sqlite3.connect(f"file:{V1_DB}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM trained_models").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM model_evaluations").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM research_observations"
                             ).fetchone()[0], 581)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone())
        finally:
            conn.close()


# ======================================================================
# Failed candidates are data
# ======================================================================

@unittest.skipUnless(os.path.exists(RESULTS), "results absent")
class TestNegativeKnowledgePersisted(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(RESULTS, encoding="utf-8") as handle:
            cls.result = json.load(handle)

    def test_every_frozen_cell_appears_in_the_result(self):
        listed = {c["cell_id"] for c in self.result["cells"]}
        self.assertEqual(len(listed), FROZEN_BUDGET)
        reported = {c["cell_id"] for h in self.result["horizons"]
                    for c in h.get("candidates", [])}
        unevaluated = set(self.result["cells_unevaluated"])
        self.assertEqual(listed, reported | unevaluated)

    def test_no_failed_cell_is_omitted(self):
        for horizon in self.result["horizons"]:
            for candidate in horizon.get("candidates", []):
                self.assertIn("survival_status", candidate)
                self.assertIn("failed_gates", candidate)
                self.assertIn("folds", candidate)
                self.assertIn("stability", candidate)

    def test_fold_level_evidence_is_retained_for_every_candidate(self):
        for horizon in self.result["horizons"]:
            for candidate in horizon.get("candidates", []):
                self.assertTrue(candidate["folds"],
                                f"{candidate['cell_id']} kept no fold evidence")
                for fold in candidate["folds"]:
                    self.assertIn("fold", fold)

    def test_the_result_records_zero_promotion_and_zero_broker_writes(self):
        self.assertEqual(self.result["models_promoted"], 0)
        self.assertEqual(self.result["thresholds_changed"], 0)
        self.assertEqual(self.result["broker_writes"], 0)
        self.assertEqual(self.result["market_data_requests"], 0)

    def test_the_result_carries_full_dataset_lineage(self):
        plan = self.result["frozen_plan"]
        self.assertEqual(plan["snapshot_id"], "research_snapshot_v2")
        self.assertEqual(plan["corpus_version_id"], "canonical_corpus_v2")
        self.assertEqual(plan["holdout_boundary"], FROZEN_HOLDOUT)
        self.assertEqual(plan["candidate_budget"], FROZEN_BUDGET)
        self.assertTrue(plan["code_commit"])
        self.assertEqual(plan["preregistration_hash"],
                         handoff()["preregistration_hash"])

    def test_every_candidate_carries_snapshot_and_prereg_lineage(self):
        for horizon in self.result["horizons"]:
            self.assertEqual(horizon["snapshot_id"], "research_snapshot_v2")
            self.assertEqual(horizon["snapshot_hash"],
                             handoff()["snapshot"]["content_hash"])
            self.assertEqual(horizon["preregistration_hash"],
                             handoff()["preregistration_hash"])

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_rejected_candidates_are_queryable_for_future_phases(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute("""
                SELECT COUNT(*), SUM(qualified)
                FROM research_candidate_results""").fetchone()
        finally:
            conn.close()
        self.assertGreater(rows[0], 0, "no candidate result was persisted")


if __name__ == "__main__":
    unittest.main()
