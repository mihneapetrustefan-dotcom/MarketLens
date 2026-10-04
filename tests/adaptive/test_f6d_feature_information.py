"""
tests/adaptive/test_f6d_feature_information.py
--------------------------------------------------
Phase 25.9I-F.6D: does a feature carry information, and would we notice
if it did not?

The required scenarios are fixtures, because the real data may not
contain the case a gate is built for. Each one proves a gate fires:

  * a feature independent of the outcome        -> NO_INFORMATION
  * a stable monotone relationship              -> STABLE_INFORMATION_CANDIDATE
  * a sign that flips across windows            -> UNSTABLE
  * two names, one vector                       -> one fingerprint, one source
  * an effect carried by one instrument         -> CONCENTRATION_WARNING
  * a feature timestamped after prediction time -> TEMPORAL_INTEGRITY_FAILED
  * many weak nominal p-values                  -> correction holds
  * weak features, real group information       -> group candidate, not a model
  * a candidate that fails the holdout          -> rejected, no rescue
"""

import ast
import json
import math
import os
import random
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import research_f6d as f6d
from src.adaptive import research_snapshot as rs
from src.adaptive.corpus_versioning import file_digest
from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.modeling import feature_information as fi

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
V1_DB = os.path.join(ROOT, "data", "marketlens.db")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
PREREG = os.path.join(ROOT, "docs", "PHASE_25_9I_F6D_PREREGISTRATION.md")
RESULTS = os.path.join(ROOT, "docs", "PHASE_25_9I_F6D_RESULTS.json")

UTC = timezone.utc

PINNED_PREREG = (
    "4563b3ef1087f5e2a79597e6438b4ec525759d2d235242fdd2e84b11c0cce137")
FROZEN_HORIZONS = ("d1", "d3", "d5")
FROZEN_FAMILY_SIZE = 78
FROZEN_INVENTORY = (
    "45dddece5017387d5c6590ad14da701dfcca372e2548dcb8576d4460af671147")
FROZEN_FAMILY = (
    "293060c829933584b9983855df5f31d1aa49a099805e9278cd798314e72d414c")
FROZEN_HOLDOUT = "2026-08-18T00:00:00+00:00"


def _code_only(relative: str) -> str:
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


def _result(per_window, *, clusters=200, distinct=50, timing="OK",
            redundancy=fi.Redundancy.UNIQUE.value, rejected=False,
            concentration=None, constant=None) -> fi.InformationResult:
    return fi.InformationResult(
        feature="fixture.feature", group="fixture", horizon="d1",
        windows=len(per_window), pooled_pairs=400, pooled_clusters=clusters,
        availability=100.0, distinct_values=distinct, constant_value=constant,
        per_window_rho=tuple(per_window),
        stability=fi.sign_consistency(tuple(per_window)),
        null={"p_value": 0.001 if rejected else 0.4},
        redundancy=redundancy, timing=timing, fdr_rejected=rejected,
        concentration=concentration or {})


# ======================================================================
# Required scenarios
# ======================================================================

class TestScenarioNoInformation(unittest.TestCase):
    """Section 51: independent feature, and one lucky window is not enough."""

    def test_an_independent_feature_is_no_information(self):
        random.seed(5)
        windows = [([random.gauss(0, 1) for _ in range(120)],
                    [random.gauss(0, 1) for _ in range(120)])
                   for _ in range(4)]
        rhos = [fi.spearman(f, o) for f, o in windows]
        null = fi.block_permutation_pvalue(windows, permutations=300, seed=1)
        verdict, _notes = fi.classify(
            _result(rhos, rejected=(null["p_value"] or 1) <= 0.05),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict, fi.FeatureVerdict.NO_INFORMATION.value)

    def test_one_favourable_window_does_not_qualify(self):
        """Three windows near zero and one strong is not stable information."""
        verdict, _ = fi.classify(
            _result([0.52, 0.01, -0.03, 0.02], rejected=True),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict, fi.FeatureVerdict.UNSTABLE.value)

    def test_a_constant_feature_is_no_information_not_insufficient_sample(self):
        """
        Zero variance is not a thin sample. A feature with five windows,
        237 clusters and one single value has an adequate design and
        nothing to measure, and the label has to say which.
        """
        verdict, notes = fi.classify(
            _result([None, None, None, None, None], distinct=1, constant=0.0,
                    redundancy=fi.Redundancy.ZERO_VARIANCE.value),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict, fi.FeatureVerdict.NO_INFORMATION.value)
        self.assertIn("constant", notes[0])
        self.assertIn("zero variance", notes[0])


class TestScenarioStableInformation(unittest.TestCase):
    """Section 52: a consistent monotone relationship is detected."""

    def test_a_stable_feature_becomes_a_candidate(self):
        random.seed(6)
        windows = []
        for _ in range(4):
            feature = [random.gauss(0, 1) for _ in range(120)]
            outcome = [0.6 * v + random.gauss(0, 0.8) for v in feature]
            windows.append((feature, outcome))
        rhos = [fi.spearman(f, o) for f, o in windows]
        null = fi.block_permutation_pvalue(windows, permutations=300, seed=1)
        self.assertLessEqual(null["p_value"], 0.05)
        self.assertGreater(null["observed_mean_rho"], null["null_p95"])
        verdict, _ = fi.classify(
            _result(rhos, rejected=True,
                    concentration={"survives": True}),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(
            verdict, fi.FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value)

    def test_every_preregistered_gate_is_required(self):
        """Dropping any one gate must stop the candidate."""
        rhos = [0.3, 0.28, 0.31, 0.29]
        self.assertEqual(
            fi.classify(_result(rhos, rejected=True,
                                concentration={"survives": True}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE)[0],
            fi.FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value)
        # not significant
        self.assertEqual(
            fi.classify(_result(rhos, rejected=False,
                                concentration={"survives": True}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE)[0],
            fi.FeatureVerdict.WEAK_INFORMATION.value)
        # too few clusters
        self.assertEqual(
            fi.classify(_result(rhos, rejected=True, clusters=5,
                                concentration={"survives": True}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE)[0],
            fi.FeatureVerdict.INSUFFICIENT_SAMPLE.value)
        # bad timing
        self.assertEqual(
            fi.classify(_result(rhos, rejected=True, timing="late",
                                concentration={"survives": True}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE)[0],
            fi.FeatureVerdict.TEMPORAL_INTEGRITY_FAILED.value)
        # concentration
        self.assertEqual(
            fi.classify(_result(rhos, rejected=True,
                                concentration={"survives": False}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE)[0],
            fi.FeatureVerdict.WEAK_INFORMATION.value)
        # redundant partner already a candidate
        self.assertEqual(
            fi.classify(_result(rhos, rejected=True,
                                concentration={"survives": True}),
                        min_windows=MIN_VALID_FOLDS,
                        min_clusters=MIN_EFFECTIVE_SAMPLE,
                        redundant_partner_is_candidate=True)[0],
            fi.FeatureVerdict.REDUNDANT.value)


class TestScenarioSignFlip(unittest.TestCase):
    """Section 53: a sign that flips is not information."""

    def test_a_flipping_feature_is_unstable(self):
        verdict, notes = fi.classify(
            _result([0.55, 0.64, -0.53, -0.45], rejected=True),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict, fi.FeatureVerdict.UNSTABLE.value)
        self.assertIn("sign flips", " ".join(notes))

    def test_sign_consistency_counts_both_directions(self):
        stability = fi.sign_consistency((0.5, 0.6, -0.5, -0.4))
        self.assertEqual((stability["positive"], stability["negative"]), (2, 2))
        self.assertFalse(stability["all_same_sign"])

    def test_a_consistently_negative_feature_is_consistent(self):
        stability = fi.sign_consistency((-0.09, -0.24, -0.16, -0.16, -0.16))
        self.assertTrue(stability["all_same_sign"])
        self.assertEqual(stability["negative"], 5)


class TestScenarioDuplicateFeatures(unittest.TestCase):
    """Section 54: two names, one vector, one source of evidence."""

    def test_identical_vectors_share_a_fingerprint(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(fi.vector_fingerprint(values),
                         fi.vector_fingerprint(list(values)))

    def test_none_is_encoded_distinctly_from_zero(self):
        self.assertNotEqual(fi.vector_fingerprint([None, 1.0]),
                            fi.vector_fingerprint([0.0, 1.0]))

    def test_two_constant_features_are_duplicates_not_two_sources(self):
        """
        The case that caught a real gap: two CONSTANT features correlate
        to None, so a correlation-only rule called them independent. The
        fingerprint catches what the correlation cannot.
        """
        columns = {"const_a": [0.0] * 50, "const_b": [0.0] * 50,
                   "varies": [float(i) for i in range(50)]}
        redundancy = fi.redundancy_map(columns)
        self.assertEqual(redundancy["identical_fingerprints"],
                         [("const_a", "const_b")])
        self.assertEqual(redundancy["classification"]["const_a"],
                         fi.Redundancy.ZERO_VARIANCE.value)
        self.assertEqual(redundancy["effective_unique_count"], 2)

    def test_a_monotone_transform_is_rank_identical(self):
        base = [float(i) for i in range(60)]
        columns = {"x": base, "scaled": [3 * v + 7 for v in base]}
        redundancy = fi.redundancy_map(columns)
        self.assertTrue(redundancy["exact_or_near_duplicates"])
        self.assertEqual(redundancy["effective_unique_count"], 1)

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_the_real_snapshot_has_two_constant_features(self):
        with open(RESULTS, encoding="utf-8") as handle:
            result = json.load(handle)
        constant = sorted({row["feature"] for row in result["univariate"]
                           if (row.get("distinct_values") or 9) <= 1})
        self.assertEqual(constant, ["event.days_since_last",
                                    "event.independent_source_count"])
        self.assertIn(("event.days_since_last",
                       "event.independent_source_count"),
                      [tuple(p) for p in
                       result["redundancy"]["identical_fingerprints"]])


class TestScenarioConcentration(unittest.TestCase):
    """Section 55: one instrument must not produce a corpus-wide finding."""

    def test_an_effect_carried_by_one_instrument_does_not_survive(self):
        random.seed(9)
        windows = []
        clusters = []
        X, Y = [], []
        for w in range(4):
            idx = []
            for row in range(100):
                dominant = row < 50
                feature = random.gauss(0, 1)
                outcome = (0.9 * feature if dominant
                           else random.gauss(0, 1))
                X.append([feature])
                Y.append(outcome)
                clusters.append("DOMINANT" if dominant else f"other-{w}-{row}")
                idx.append(len(X) - 1)
            windows.append({"label": f"wf{w}", "test_idx": idx,
                            "train_idx": idx})
        full = [fi.spearman([X[i][0] for i in w["test_idx"]],
                            [Y[i] for i in w["test_idx"]]) for w in windows]
        self.assertTrue(fi.sign_consistency(tuple(full))["all_same_sign"])
        check = f6d.concentration_check(0, X, Y, clusters, windows)
        self.assertEqual(check["dominant_instrument"], "DOMINANT")
        self.assertFalse(check["survives"])

    def test_a_broad_effect_does_survive_removing_its_largest_instrument(self):
        random.seed(10)
        X, Y, clusters, windows = [], [], [], []
        for w in range(4):
            idx = []
            for row in range(100):
                feature = random.gauss(0, 1)
                X.append([feature])
                Y.append(0.7 * feature + random.gauss(0, 0.7))
                clusters.append(f"inst-{row % 20}")
                idx.append(len(X) - 1)
            windows.append({"label": f"wf{w}", "test_idx": idx,
                            "train_idx": idx})
        check = f6d.concentration_check(0, X, Y, clusters, windows)
        self.assertTrue(check["survives"])

    def test_the_dominant_instrument_is_chosen_by_row_count_not_outcome(self):
        source = _code_only(os.path.join("scripts", "research_f6d.py"))
        self.assertIn("most_common", source)


class TestScenarioLeakage(unittest.TestCase):
    """Section 56: a feature dated after prediction time is excluded."""

    def test_a_late_timestamp_disqualifies_outright(self):
        verdict, notes = fi.classify(
            _result([0.4, 0.42, 0.39], rejected=True,
                    timing="TEMPORAL_INTEGRITY_FAILED: 12 value(s) dated after "
                           "their observation's information cutoff",
                    concentration={"survives": True}),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict,
                         fi.FeatureVerdict.TEMPORAL_INTEGRITY_FAILED.value)
        self.assertIn("after", notes[0])

    def test_timing_is_checked_before_any_evidence(self):
        """A leaky feature is rejected even with a perfect relationship."""
        verdict, _ = fi.classify(
            _result([0.95, 0.96, 0.94], rejected=True, timing="late",
                    concentration={"survives": True}),
            min_windows=MIN_VALID_FOLDS, min_clusters=MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(verdict,
                         fi.FeatureVerdict.TEMPORAL_INTEGRITY_FAILED.value)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_no_feature_in_the_snapshot_is_dated_after_its_cutoff(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            late = conn.execute("""
                SELECT COUNT(*) FROM research_features f
                JOIN research_observations o
                  ON o.observation_id = f.observation_id
                WHERE f.as_of IS NOT NULL
                  AND f.as_of > o.information_cutoff""").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(late, 0)


class TestScenarioMultipleTesting(unittest.TestCase):
    """Section 57: no winner from uncorrected p-value mining."""

    def test_a_few_weak_p_values_among_many_nulls_are_not_rejected(self):
        p_values = {f"null{i}": 0.5 for i in range(18)}
        p_values.update({"weak1": 0.04, "weak2": 0.045})
        bh = fi.benjamini_hochberg(p_values, q=0.05)
        self.assertEqual(bh["nominal_below_q"], 2)
        self.assertEqual(bh["rejected_count"], 0)

    def test_a_genuinely_strong_result_is_still_rejected(self):
        p_values = {f"null{i}": 0.5 for i in range(19)}
        p_values["real"] = 0.0001
        bh = fi.benjamini_hochberg(p_values, q=0.05)
        self.assertEqual(bh["rejected"], ["real"])

    def test_uncomputable_hypotheses_stay_in_the_family_count(self):
        """
        Dropping them would shrink the denominator and weaken the
        correction below the family that was actually declared.
        """
        p_values = {"a": 0.001, "b": None, "c": None}
        bh = fi.benjamini_hochberg(p_values, q=0.05)
        self.assertEqual(bh["family_size"], 3)
        self.assertEqual(bh["tested"], 2 - 1)
        self.assertEqual(bh["uncomputable"], 2)

    def test_adjusted_values_are_monotone(self):
        p_values = {f"h{i}": p for i, p in
                    enumerate([0.001, 0.01, 0.02, 0.2, 0.5])}
        bh = fi.benjamini_hochberg(p_values, q=0.05)
        adjusted = [bh["adjusted"][f"h{i}"] for i in range(5)]
        self.assertEqual(adjusted, sorted(adjusted))

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_the_real_correction_removed_every_nominal_winner(self):
        with open(RESULTS, encoding="utf-8") as handle:
            bh = json.load(handle)["multiple_testing"]
        self.assertEqual(bh["family_size"], FROZEN_FAMILY_SIZE)
        self.assertGreater(bh["nominal_below_q"], 5)
        self.assertEqual(bh["rejected_count"], 0)


class TestScenarioGroupWithoutUnivariate(unittest.TestCase):
    """Section 58: group information without univariate information."""

    def test_a_probe_can_find_what_single_features_miss(self):
        """
        Two features, each individually uninformative, whose product
        drives the outcome. The probe is given the interaction column,
        which is what an interaction probe is for.
        """
        random.seed(12)
        a = [random.gauss(0, 1) for _ in range(400)]
        b = [random.gauss(0, 1) for _ in range(400)]
        outcome = [0.9 * x * y + random.gauss(0, 0.5)
                   for x, y in zip(a, b)]
        self.assertLess(abs(fi.spearman(a, outcome)), 0.15)
        self.assertLess(abs(fi.spearman(b, outcome)), 0.15)
        product = [x * y for x, y in zip(a, b)]
        self.assertGreater(abs(fi.spearman(product, outcome)), 0.5)

    def test_a_group_result_is_not_a_model_qualification(self):
        self.assertIn("PROMISING_INFORMATION_GROUP",
                      [v.value for v in fi.GroupVerdict])
        self.assertNotIn("QUALIFIED",
                         " ".join(v.value for v in fi.GroupVerdict))

    def test_the_probe_is_fixed_and_low_capacity(self):
        self.assertEqual(f6d.PROBE_ALPHA, 1.0)
        source = _code_only(os.path.join("scripts", "research_f6d.py"))
        for forbidden in ("GradientBoosting", "RandomForest", "XGB",
                          "MLP", "neural", "hyperparameter"):
            self.assertNotIn(forbidden, source, forbidden)


class TestScenarioHoldoutFailure(unittest.TestCase):
    """Section 59: failing the holdout is a rejection, never a rescue."""

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_a_candidate_whose_sign_flips_at_holdout_is_rejected(self):
        with open(RESULTS, encoding="utf-8") as handle:
            result = json.load(handle)
        rejected = [h for h in result["holdout"]["results"]
                    if h["verdict"] == "REJECTED_AT_HOLDOUT"]
        self.assertTrue(rejected, "the run should contain a holdout rejection")
        for item in rejected:
            self.assertFalse(item["same_direction"])
            self.assertNotEqual((item["holdout_rho"] or 0) > 0,
                                (item["research_mean_rho"] or 0) > 0)

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_the_holdout_was_opened_only_for_the_frozen_survivor_list(self):
        with open(RESULTS, encoding="utf-8") as handle:
            result = json.load(handle)
        expected = set(result["pre_holdout_survivors"]) | set(
            result["promising_groups"])
        evaluated = {h["candidate"] for h in result["holdout"]["results"]}
        self.assertEqual(evaluated, expected)

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_opening_for_more_candidates_than_were_evaluated_is_an_error(self):
        """
        The defect an earlier build had: holdout opened on the strength
        of two groups, and only the empty feature list evaluated.
        """
        source = _code_only(os.path.join("scripts", "research_f6d.py"))
        self.assertIn("evaluated != expected", source)
        with open(RESULTS, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["errors"], [])


# ======================================================================
# The frozen plan
# ======================================================================

@unittest.skipUnless(os.path.exists(PREREG), "pre-registration absent")
class TestFrozenPlan(unittest.TestCase):

    def test_the_preregistration_still_hashes_to_the_pinned_value(self):
        self.assertEqual(file_digest(PREREG), PINNED_PREREG)
        self.assertEqual(f6d.PREREGISTRATION_SHA256, PINNED_PREREG)

    def test_the_frozen_lists_are_unchanged(self):
        self.assertEqual(f6d.HORIZONS, FROZEN_HORIZONS)
        self.assertEqual(f6d.FEATURE_INVENTORY_SHA256, FROZEN_INVENTORY)
        self.assertEqual(f6d.HYPOTHESIS_FAMILY_SHA256, FROZEN_FAMILY)
        self.assertEqual(f6d.EXPECTED_NUMERIC_FEATURES, 26)
        self.assertEqual(f6d.EXPECTED_REGISTRY_FEATURES, 33)

    def test_the_statistical_policy_is_unchanged(self):
        self.assertEqual(f6d.PERMUTATIONS, 500)
        self.assertEqual(f6d.PERMUTATION_SEED, 20261004)
        self.assertEqual(f6d.FDR_Q, 0.05)
        self.assertEqual(len(f6d.INTERACTIONS), 3)

    def test_d10_remains_deferred_and_d20_protected(self):
        self.assertEqual(f6d.DEFERRED["d10"],
                         "DEFERRED_INSUFFICIENT_HOLDOUT")
        self.assertEqual(f6d.DEFERRED["d20"], "PROTECTED")
        self.assertNotIn("d10", f6d.HORIZONS)
        self.assertNotIn("d20", f6d.HORIZONS)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_preflight_verifies_both_predecessors(self):
        context = f6d.preflight(V2_DB)
        for phase, rows in (("25.9I-F.6B", 16), ("25.9I-F.6C", 12)):
            record = context["predecessors_preserved"][phase]
            self.assertEqual(record["status"], "complete", phase)
            self.assertEqual(record["qualified_count"], 0, phase)
            self.assertEqual(record["candidate_rows"], rows, phase)
            self.assertEqual(record["qualified_rows"], 0, phase)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_holdout_is_inherited_and_unchanged(self):
        context = f6d.preflight(V2_DB)
        self.assertEqual(context["holdout_boundary"], FROZEN_HOLDOUT)
        self.assertIn("inherited", context["holdout_origin"])

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_family_is_exactly_the_declared_size(self):
        context = f6d.preflight(V2_DB)
        self.assertEqual(context["hypothesis_family_size"],
                         FROZEN_FAMILY_SIZE)
        self.assertEqual(len(context["numeric_features"]) * len(FROZEN_HORIZONS),
                         FROZEN_FAMILY_SIZE)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_a_completed_execution_refuses_a_second_run(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM research_executions WHERE phase = ? "
                "AND status = 'complete'", (f6d.PHASE,)).fetchone()
        finally:
            conn.close()
        if not row:
            self.skipTest("no complete F.6D execution recorded yet")
        with self.assertRaises(f6d.FrozenPlanViolation):
            f6d.run(V2_DB, verbose=False)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_invalidated_run_is_kept_on_the_record(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT status, note FROM research_executions WHERE phase = ?",
                (f6d.PHASE,)).fetchall()
        finally:
            conn.close()
        invalidated = [r for r in rows if r[0] == "invalidated"]
        if not invalidated:
            self.skipTest("no invalidated run recorded")
        self.assertIn("INVALIDATED", invalidated[0][1])

    def test_an_invalidation_must_state_a_reason(self):
        with self.assertRaises(f6d.FrozenPlanViolation):
            f6d.invalidate(V2_DB, reason="  ")


# ======================================================================
# D20 and the production boundary
# ======================================================================

class TestProtectedAndBoundary(unittest.TestCase):

    def test_pairing_any_feature_with_the_protected_horizon_raises(self):
        with self.assertRaises(f6d.ProtectedHorizonRequested):
            f6d.guard_no_protected_horizon("d20")

    def test_the_eligible_horizons_all_pass_the_guard(self):
        for horizon in f6d.HORIZONS:
            f6d.guard_no_protected_horizon(horizon)

    def test_the_protected_feature_is_audited_but_never_against_d20(self):
        """
        `market.return_60d` is a legitimate feature. What is sealed is
        the hypothesis relating it to the d20 abnormal return, so the
        HORIZON is refused rather than the feature.
        """
        self.assertNotIn("d20", f6d.HORIZONS)
        self.assertTrue(rs.is_protected("d20"))

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_no_result_cell_pairs_anything_with_d20(self):
        with open(RESULTS, encoding="utf-8") as handle:
            result = json.load(handle)
        for row in result["univariate"]:
            self.assertNotEqual(row["horizon"], "d20")
        for collection in ("groups", "ablation", "interactions"):
            for row in result[collection]:
                self.assertNotEqual(row["horizon"], "d20")
        self.assertIn("market.return_60d",
                      {row["feature"] for row in result["univariate"]})

    def test_no_protected_label_or_validator_is_referenced(self):
        for relative in (os.path.join("scripts", "research_f6d.py"),
                         os.path.join("src", "modeling",
                                      "feature_information.py")):
            source = _code_only(relative)
            for forbidden in ("d20.abnormal_return", "d20.raw_return",
                              "validate_d20_reversal", "anchor_v2",
                              "return_60d", "check_d20_readiness"):
                self.assertNotIn(forbidden, source, f"{relative}: {forbidden}")

    def test_the_harness_reads_feature_values_but_never_a_label_table(self):
        """
        A feature audit has to read feature values, so a blanket ban on
        `value_json` was the wrong test -- it would forbid the phase's
        own subject. What must not happen is the harness reaching into
        `research_labels` itself; outcomes arrive only through the
        canonical loader.
        """
        source = _code_only(os.path.join("scripts", "research_f6d.py"))
        self.assertNotIn("research_labels", source)
        self.assertIn("research_features", source)
        self.assertNotIn("value_json",
                         _code_only(os.path.join("src", "modeling",
                                                 "feature_information.py")))

    def test_the_harness_touches_no_broker_or_production_path(self):
        source = _code_only(os.path.join("scripts", "research_f6d.py"))
        for forbidden in ("OrderIntent", "place_order", "ExecutionEngine",
                          "BrokerClient", "PaperBroker", "IBKRClient",
                          "market_snapshot", "promote_model",
                          "PRODUCTION_ACTIVE", "min_signal_confidence"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_harness_imports_no_production_module(self):
        with open(os.path.join(ROOT, "scripts", "research_f6d.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for prefix in ("src.execution", "src.brokers", "src.paper",
                       "src.trading", "src.ibkr", "src.portfolio", "src.risk",
                       "src.capture", "requests", "urllib"):
            for name in imported:
                self.assertFalse(name == prefix or name.startswith(prefix + "."),
                                 f"imports {name}")

    def test_the_statistics_module_adds_no_dependency(self):
        with open(os.path.join(ROOT, "src", "modeling",
                               "feature_information.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported <= {"hashlib", "json", "math", "random",
                                     "dataclasses", "enum", "typing",
                                     "__future__"}, imported)

    @unittest.skipUnless(os.path.exists(V1_DB), "production snapshot absent")
    def test_the_production_snapshot_is_untouched(self):
        conn = sqlite3.connect(f"file:{V1_DB}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM research_observations").fetchone()[0], 581)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_executions'").fetchone())
        finally:
            conn.close()


# ======================================================================
# The null must stay block-aware
# ======================================================================

class TestBlockAwareNull(unittest.TestCase):

    def test_permutation_happens_within_windows_not_across_them(self):
        source = _code_only(os.path.join("src", "modeling",
                                         "feature_information.py"))
        self.assertIn("for feature, outcome in windows", source)
        self.assertIn("generator.shuffle", source)

    def test_the_null_is_reproducible_from_its_seed(self):
        random.seed(3)
        windows = [([random.gauss(0, 1) for _ in range(80)],
                    [random.gauss(0, 1) for _ in range(80)])
                   for _ in range(3)]
        first = fi.block_permutation_pvalue(windows, permutations=200, seed=7)
        second = fi.block_permutation_pvalue(windows, permutations=200, seed=7)
        self.assertEqual(first, second)

    def test_a_different_seed_gives_a_different_draw(self):
        random.seed(4)
        windows = [([random.gauss(0, 1) for _ in range(80)],
                    [random.gauss(0, 1) for _ in range(80)])
                   for _ in range(3)]
        self.assertNotEqual(
            fi.block_permutation_pvalue(windows, permutations=200, seed=1),
            fi.block_permutation_pvalue(windows, permutations=200, seed=2))

    def test_the_p_value_can_never_be_zero(self):
        windows = [([float(i) for i in range(60)],
                    [float(i) for i in range(60)]) for _ in range(3)]
        null = fi.block_permutation_pvalue(windows, permutations=100, seed=1)
        self.assertGreater(null["p_value"], 0.0)
        self.assertAlmostEqual(null["p_value"], 1 / 101, places=6)

    def test_an_uncomputable_statistic_gives_none_not_one(self):
        null = fi.block_permutation_pvalue(
            [([1.0] * 20, [2.0] * 20)], permutations=50, seed=1)
        self.assertIsNone(null["p_value"])
        self.assertIn("NOT_COMPUTED", null["status"])


# ======================================================================
# The recorded result
# ======================================================================

@unittest.skipUnless(os.path.exists(RESULTS), "results absent")
class TestRecordedResult(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(RESULTS, encoding="utf-8") as handle:
            cls.result = json.load(handle)

    def test_all_seventy_eight_cells_were_audited(self):
        self.assertEqual(len(self.result["univariate"]), FROZEN_FAMILY_SIZE)

    def test_no_stable_univariate_candidate_was_found(self):
        verdicts = {row["verdict"] for row in self.result["univariate"]}
        self.assertNotIn(
            fi.FeatureVerdict.STABLE_INFORMATION_CANDIDATE.value, verdicts)
        self.assertEqual(self.result["pre_holdout_survivors"], [])

    def test_the_inventory_is_the_real_one(self):
        inventory = self.result["inventory"]
        self.assertEqual(inventory["registry_count"], 33)
        self.assertEqual(inventory["numeric_count"], 26)
        self.assertEqual(inventory["categorical_count"], 5)
        self.assertEqual(inventory["no_usable_value_count"], 2)
        self.assertEqual(inventory["timing_failures"], [])

    def test_the_funnel_reconciles_downwards(self):
        """
        Read in PIPELINE order, not alphabetical: the artifact is written
        with sorted keys, so `stage_order` is what preserves the shape.
        """
        funnel = self.result["information_funnel"]
        counts = [stage["count"] for stage in funnel["stages"]]
        self.assertEqual(counts, sorted(counts, reverse=True), funnel["stages"])
        self.assertEqual(funnel["stage_order"][0], "inventoried")
        self.assertEqual(funnel["stage_order"][-1], "stable_candidates")
        self.assertEqual(funnel["inventoried"], FROZEN_FAMILY_SIZE)
        self.assertEqual(funnel["stable_candidates"], 0)

    def test_nothing_was_qualified_promoted_or_fabricated(self):
        self.assertEqual(self.result["models_qualified"], 0)
        self.assertEqual(self.result["models_promoted"], 0)
        self.assertEqual(self.result["thresholds_changed"], 0)
        self.assertFalse(self.result["expected_return_capable"])
        self.assertFalse(self.result["target_capable"])
        self.assertEqual(self.result["broker_writes"], 0)
        self.assertEqual(self.result["market_data_requests"], 0)

    def test_every_cell_retains_its_window_level_evidence(self):
        for row in self.result["univariate"]:
            self.assertEqual(len(row["per_window_rho"]), row["windows"])
            self.assertIn("stability", row)
            self.assertIn("null", row)

    def test_the_budget_was_respected(self):
        cells = self.result["cells"]
        self.assertEqual(cells["univariate"], 78)
        self.assertEqual(cells["group_probes"], 21)
        self.assertEqual(cells["ablation"], 24)
        self.assertEqual(cells["interactions"], 9)
        self.assertEqual(cells["total"], 132)

    def test_lineage_is_complete(self):
        plan = self.result["frozen_plan"]
        self.assertEqual(plan["snapshot_id"], "research_snapshot_v2")
        self.assertEqual(plan["preregistration_hash"], PINNED_PREREG)
        self.assertEqual(plan["feature_inventory_hash"], FROZEN_INVENTORY)
        self.assertEqual(plan["hypothesis_family_hash"], FROZEN_FAMILY)
        self.assertTrue(plan["code_commit"])


if __name__ == "__main__":
    unittest.main()
