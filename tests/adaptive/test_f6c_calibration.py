"""
tests/adaptive/test_f6c_calibration.py
--------------------------------------------------
Phase 25.9I-F.6C: calibration cannot create discrimination.

The required scenarios are the heart of this file. Each is a fixture
proving the gates behave correctly in a case the real data might not
happen to contain:

  * raw probabilities overconfident -> calibration FAIL
  * calibration genuinely helps     -> metrics improve, AUC preserved
  * calibration destroys value      -> collapses to base rate, still fails
  * relative win, terrible absolute -> FAIL on the no-skill floor
  * empty holdout                   -> cannot qualify
  * degenerate feature configs      -> one fingerprint, one piece of evidence
  * horizon disagreement            -> preferred horizon may be None

Everything else checks that the frozen plan is the plan that ran and
that F.6B was left alone.
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

from scripts import research_f6c as f6c
from scripts import research_model_quality as rmq
from src.adaptive import research_snapshot as rs
from src.adaptive.corpus_versioning import file_digest, load_snapshot
from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.domain.model_models import ModelEvaluation
from src.modeling import calibration as cal

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
V1_DB = os.path.join(ROOT, "data", "marketlens.db")
V2_DB = os.path.join(ROOT, "data", "marketlens_corpus_v2.db")
PREREG = os.path.join(ROOT, "docs", "PHASE_25_9I_F6C_PREREGISTRATION.md")
RESULTS = os.path.join(ROOT, "docs", "PHASE_25_9I_F6C_RESULTS.json")
F6B_RESULTS = os.path.join(ROOT, "docs", "PHASE_25_9I_F6B_RESULTS.json")

UTC = timezone.utc

#: The plan, written down so a change has to break a test.
FROZEN_HOLDOUT = "2026-08-18T00:00:00+00:00"
FROZEN_HORIZONS = ("d1", "d3")
FROZEN_CONFIGS = ("all_numeric", "dense_only")
FROZEN_METHODS = ("raw", "platt", "isotonic")
FROZEN_PURGE = {"d1": 3.0, "d3": 5.0}
FROZEN_EMBARGO = 6.0
FROZEN_BUDGET = 12
PINNED_PREREG = (
    "ba141fd9fdead1bc8b56d524a0596b03f7f8a97bde5b98bc8caaf9c8ede66ede")


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


# ======================================================================
# Required scenarios
# ======================================================================

class TestScenarioRawFailsCalibration(unittest.TestCase):
    """Section 45: discrimination exists, probabilities are overconfident."""

    @classmethod
    def setUpClass(cls):
        random.seed(101)
        cls.true_logit = [random.gauss(0.0, 1.0) for _ in range(400)]
        cls.outcomes = [1.0 if random.random() < 1 / (1 + math.exp(-z)) else -1.0
                        for z in cls.true_logit]
        # The model ranks correctly but states probabilities far too
        # extreme. The inflation factor has to be large: at 3x the model
        # still beats the base-rate Brier, because real discrimination
        # buys a lot of slack. 12x is squarely in the regime this
        # scenario is about -- confident to the point of being wrong.
        cls.raw = [1 / (1 + math.exp(-12.0 * z)) for z in cls.true_logit]
        cls.base_rate = cal.positive_rate(cls.outcomes)

    def test_discrimination_is_genuinely_present(self):
        self.assertGreater(cal.roc_auc(self.raw, self.outcomes), 0.6)
        self.assertGreater(cal.balanced_accuracy(self.raw, self.outcomes), 0.5)

    def test_the_curve_reads_overconfident(self):
        curve = cal.calibration_curve(self.raw, self.outcomes)
        self.assertEqual(curve["status"], "COMPUTED")
        self.assertLess(curve["slope"], 1.0)
        self.assertEqual(curve["reading"], "overconfident")

    def test_raw_brier_is_worse_than_the_base_rate(self):
        report = cal.probability_report(self.raw, self.outcomes,
                                        base_rate=self.base_rate)
        self.assertLess(report["calibration"]["brier_skill_score"], 0.0)

    def test_so_the_calibration_gate_fails_even_though_it_discriminates(self):
        folds = [_fold_result(self.raw, self.outcomes, self.base_rate)
                 for _ in range(3)]
        verdict = f6c.apply_gates(folds, "raw")
        self.assertFalse(verdict["survived"])
        self.assertIn("FAILED_CALIBRATION", verdict["failed"])
        self.assertEqual(verdict["gates"]["discrimination"]["status"], "PASS")
        self.assertEqual(verdict["gates"]["holdout"]["status"], "NOT_REACHED")


class TestScenarioCalibrationHelps(unittest.TestCase):
    """Section 46: fitted only on past data, and it improves later folds."""

    @classmethod
    def setUpClass(cls):
        random.seed(202)
        n = 600
        logits = [random.gauss(0.0, 1.2) for _ in range(n)]
        cls.outcomes = [1.0 if random.random() < 1 / (1 + math.exp(-z)) else -1.0
                        for z in logits]
        cls.raw = [1 / (1 + math.exp(-2.5 * z)) for z in logits]
        cls.half = n // 2
        cls.base_rate = cal.positive_rate(cls.outcomes[:cls.half])

    def test_the_calibrator_sees_only_the_earlier_window(self):
        fit = cal.fit_platt(self.raw[:self.half], self.outcomes[:self.half])
        self.assertEqual(fit.status, "FITTED")
        self.assertEqual(fit.fitted_rows, self.half)

    def test_calibration_improves_brier_on_the_later_window(self):
        later_outcomes = self.outcomes[self.half:]
        raw_brier = cal.brier_score(self.raw[self.half:], later_outcomes)
        for method in ("platt", "isotonic"):
            fit = cal.fit_calibrator(method, self.raw[:self.half],
                                     self.outcomes[:self.half])
            calibrated = fit.apply(self.raw[self.half:])
            self.assertLess(cal.brier_score(calibrated, later_outcomes),
                            raw_brier, method)

    def test_a_monotone_calibrator_cannot_change_roc_auc(self):
        """
        The clearest statement of why calibration is not a rescue: Platt
        is strictly monotone, so the ranking -- and therefore the AUC --
        is exactly what it was.
        """
        later_outcomes = self.outcomes[self.half:]
        before = cal.roc_auc(self.raw[self.half:], later_outcomes)
        fit = cal.fit_platt(self.raw[:self.half], self.outcomes[:self.half])
        after = cal.roc_auc(fit.apply(self.raw[self.half:]), later_outcomes)
        self.assertAlmostEqual(before, after, places=6)

    def test_every_calibrated_probability_is_inside_the_unit_interval(self):
        for method in FROZEN_METHODS:
            fit = cal.fit_calibrator(method, self.raw[:self.half],
                                     self.outcomes[:self.half])
            for value in fit.apply(self.raw[self.half:]):
                self.assertIsNotNone(value)
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)


class TestScenarioCalibrationDestroysValue(unittest.TestCase):
    """
    Section 47: a model with no discrimination calibrates beautifully by
    collapsing onto the base rate -- and must not qualify for it.
    """

    @classmethod
    def setUpClass(cls):
        random.seed(303)
        n = 400
        cls.outcomes = [1.0 if random.random() < 0.55 else -1.0
                        for _ in range(n)]
        # Noise, stated confidently: no ranking information at all.
        cls.raw = [random.uniform(0.05, 0.95) for _ in range(n)]
        cls.base_rate = 0.55

    def test_there_is_no_discrimination_to_begin_with(self):
        self.assertLess(abs(cal.roc_auc(cls_auc := cal.roc_auc(
            self.raw, self.outcomes) and self.raw, self.outcomes) - 0.5), 0.08)

    def test_calibration_improves_the_probability_metric(self):
        fit = cal.fit_platt(self.raw[:200], self.outcomes[:200])
        if fit.status != "FITTED":
            self.skipTest("the fixture's calibration window was degenerate")
        before = cal.brier_score(self.raw[200:], self.outcomes[200:])
        after = cal.brier_score(fit.apply(self.raw[200:]), self.outcomes[200:])
        self.assertLessEqual(after, before)

    def test_but_discrimination_is_still_absent_so_it_cannot_qualify(self):
        folds = [_fold_result(self.raw, self.outcomes, self.base_rate,
                              beats_baseline=True)
                 for _ in range(3)]
        verdict = f6c.apply_gates(folds, "raw")
        self.assertFalse(verdict["survived"])
        self.assertIn("FAILED_ABSOLUTE_QUALITY", verdict["failed"])

    def test_a_base_rate_predictor_is_perfectly_calibrated_and_useless(self):
        flat = [self.base_rate] * len(self.outcomes)
        self.assertEqual(cal.roc_auc(flat, self.outcomes), 0.5)
        self.assertEqual(cal.balanced_accuracy(flat, self.outcomes), 0.5)
        self.assertEqual(
            cal.brier_skill_score(cal.brier_score(flat, self.outcomes),
                                  cal.brier_score(flat, self.outcomes)), 0.0)


class TestScenarioBadAbsoluteAccuracy(unittest.TestCase):
    """
    Section 48: the F.6B d3 case. A relative win over a terrible baseline
    must not pass.
    """

    def test_an_always_up_predictor_scores_exactly_the_no_skill_point(self):
        outcomes = [-1.0] * 13 + [1.0] * 4          # 23.5% positive
        always_up = [0.8] * 17
        self.assertAlmostEqual(cal.accuracy(always_up, outcomes), 0.235294,
                               places=5)
        self.assertEqual(cal.balanced_accuracy(always_up, outcomes), 0.5)
        self.assertEqual(cal.roc_auc(always_up, outcomes), 0.5)

    def test_raw_accuracy_is_not_a_gate_and_balanced_accuracy_is(self):
        """
        31.2% beating 26.0% is the exact shape F.6B credited as a win.
        The absolute-quality gate refuses it because balanced accuracy
        does not clear its no-skill point.
        """
        outcomes = [-1.0] * 13 + [1.0] * 4
        always_up = [0.8] * 17
        folds = [_fold_result(always_up, outcomes, 0.235,
                              beats_baseline=True) for _ in range(3)]
        verdict = f6c.apply_gates(folds, "raw")
        self.assertFalse(verdict["survived"])
        self.assertIn("FAILED_ABSOLUTE_QUALITY", verdict["failed"])
        self.assertEqual(verdict["gates"]["discrimination"]["status"], "PASS")

    def test_the_no_skill_points_are_the_metrics_own_zero_points(self):
        self.assertEqual(f6c.NO_SKILL_BALANCED_ACCURACY, 0.5)
        self.assertEqual(f6c.NO_SKILL_AUC, 0.5)
        self.assertEqual(f6c.NO_SKILL_BRIER_SKILL, 0.0)

    def test_balanced_accuracy_is_immune_to_class_balance(self):
        for positive_share in (0.1, 0.3, 0.5, 0.7, 0.9):
            count = 200
            positives = int(count * positive_share)
            outcomes = [1.0] * positives + [-1.0] * (count - positives)
            self.assertEqual(cal.balanced_accuracy([0.9] * count, outcomes),
                             0.5, positive_share)


class TestScenarioEmptyHoldout(unittest.TestCase):
    """Section 49: fold existence is not adequacy."""

    def test_a_horizon_with_no_holdout_rows_has_an_invalid_split(self):
        boundary = datetime(2026, 8, 18, tzinfo=UTC)
        cutoffs = [boundary - timedelta(days=40) + timedelta(hours=6 * i)
                   for i in range(120)]           # all strictly before
        split = f6c.holdout_split(cutoffs, 3.0, holdout_start=boundary,
                                  embargo_days=6.0)
        self.assertEqual(split["test_idx"], [])
        self.assertFalse(split["valid"])

    def test_d10_is_deferred_for_exactly_that_reason(self):
        self.assertEqual(f6c.DEFERRED["d10"], "DEFERRED_INSUFFICIENT_HOLDOUT")

    def test_d5_is_deferred_on_folds_not_on_its_result(self):
        self.assertEqual(f6c.DEFERRED["d5"], "DEFERRED_INSUFFICIENT_FOLDS")

    def test_neither_deferred_horizon_is_researched(self):
        for horizon in ("d5", "d10"):
            self.assertNotIn(horizon, f6c.HORIZONS)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_both_researched_horizons_have_a_usable_holdout(self):
        plan = f6c.build_plan(V2_DB)
        for item in plan["per_horizon"]:
            self.assertTrue(item["holdout_valid"], item["horizon"])
            self.assertGreater(item["holdout_rows"], 0, item["horizon"])


class TestScenarioDegenerateFeatureConfig(unittest.TestCase):
    """Section 50: two aliases of one design matrix are one piece of evidence."""

    def test_identical_column_sets_fingerprint_identically(self):
        names = ["a", "b", "c"]
        self.assertEqual(f6c.feature_fingerprint(names, [0, 1, 2]),
                         f6c.feature_fingerprint(names, [2, 1, 0]))

    def test_different_column_sets_fingerprint_differently(self):
        names = ["a", "b", "c"]
        self.assertNotEqual(f6c.feature_fingerprint(names, [0, 1, 2]),
                            f6c.feature_fingerprint(names, [0, 1]))

    def test_the_fingerprint_is_deterministic(self):
        names = ["x", "y"]
        self.assertEqual(f6c.feature_fingerprint(names, [0, 1]),
                         f6c.feature_fingerprint(names, [0, 1]))

    def test_the_fingerprint_uses_no_label_value(self):
        source = _code_only(os.path.join("scripts", "research_f6c.py"))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "feature_fingerprint"):
                body = ast.unparse(node)
                for forbidden in ("Y", "outcome", "label", "value_json"):
                    self.assertNotIn(forbidden, body, forbidden)
                return
        self.fail("feature_fingerprint not found")

    @unittest.skipUnless(os.path.exists(RESULTS), "results absent")
    def test_the_two_positive_skill_results_are_one_observation(self):
        """
        Exactly two fold-evaluations had positive Brier skill, and they
        are the same fold under two configurations whose fingerprints
        are identical there. Counting them as two would be counting one
        thing twice.
        """
        with open(RESULTS, encoding="utf-8") as handle:
            result = json.load(handle)
        positive = []
        for candidate in result["candidates"]:
            for fold in candidate["folds"]:
                skill = fold["report"]["calibration"]["brier_skill_score"]
                if skill is not None and skill > 0:
                    positive.append((candidate["cell_id"], fold["fold"],
                                     fold["feature_fingerprint"], skill))
        self.assertEqual(len(positive), 2, positive)
        self.assertEqual({p[1] for p in positive}, {"wf4"})
        self.assertEqual(len({p[2] for p in positive}), 1,
                         "the two results should share one fingerprint")
        self.assertEqual(len({p[3] for p in positive}), 1,
                         "and therefore one identical skill value")


class TestScenarioHorizonDisagreement(unittest.TestCase):
    """Section 51: a majority vote never overrides directional disagreement."""

    def test_opposite_directions_do_not_average_into_a_preference(self):
        d1_probability, d5_probability = 0.25, 0.75
        self.assertLess(d1_probability, 0.5)
        self.assertGreater(d5_probability, 0.5)
        directions = {"d1": d1_probability > 0.5, "d5": d5_probability > 0.5}
        self.assertEqual(len(set(directions.values())), 2)
        preferred = None if len(set(directions.values())) > 1 else "d1"
        self.assertIsNone(preferred)

    def test_the_horizon_contract_refuses_a_preference_under_disagreement(self):
        """
        Stronger than checking an enum member exists: the F.5 contract
        RAISES if a preferred horizon is set while the horizons disagree,
        so a majority vote cannot overwrite the disagreement.
        """
        from src.domain.signal_availability_models import (
            HorizonAgreement, HorizonIntelligenceResult)
        with self.assertRaises(ValueError):
            HorizonIntelligenceResult(
                instrument_id="x", as_of=datetime(2026, 10, 4, tzinfo=UTC),
                preferred_horizon="d1",
                agreement=HorizonAgreement.DISAGREEMENT)
        allowed = HorizonIntelligenceResult(
            instrument_id="x", as_of=datetime(2026, 10, 4, tzinfo=UTC),
            preferred_horizon=None,
            agreement=HorizonAgreement.DISAGREEMENT)
        self.assertIsNone(allowed.preferred_horizon)


def _fold_result(probabilities, outcomes, base_rate, *,
                 beats_baseline=True, test_clusters=40) -> dict:
    """A fold record shaped like `evaluate_window` returns, for fixtures."""
    report = cal.probability_report(probabilities, outcomes,
                                    base_rate=base_rate)
    return {
        "fold": "wfX", "feature_fingerprint": "fixture", "features": 26,
        "train_rows": 100, "calibration_rows": 60,
        "test_rows": report["n"], "test_clusters": test_clusters,
        "base_rate": base_rate,
        "canonical": {"beats_all_baselines": beats_baseline,
                      "is_deployable": beats_baseline,
                      "small_sample": False,
                      "effective_sample_size": test_clusters,
                      "metrics": {}, "baselines": {}},
        "raw_discrimination": {
            "roc_auc": report["discrimination"]["roc_auc"],
            "balanced_accuracy": report["discrimination"]["balanced_accuracy"]},
        "arms": {"raw": {"calibrator": {"method": "raw"}, "report": report}},
    }


# ======================================================================
# Gate order and the two-verdict rule
# ======================================================================

class TestGateOrder(unittest.TestCase):

    def test_discrimination_is_evaluated_before_calibration(self):
        outcomes = [1.0 if i % 2 else -1.0 for i in range(100)]
        noise = [0.5] * 100                      # AUC exactly 0.5
        folds = [_fold_result(noise, outcomes, 0.5, beats_baseline=True)
                 for _ in range(3)]
        verdict = f6c.apply_gates(folds, "raw")
        self.assertEqual(verdict["gates"]["absolute_quality"]["status"], "FAIL")
        self.assertEqual(verdict["gates"]["calibration"]["status"],
                         "NOT_REACHED")

    def test_a_sample_failure_stops_everything_downstream(self):
        outcomes = [1.0, -1.0] * 15
        folds = [_fold_result([0.6] * 30, outcomes, 0.5, test_clusters=2)]
        verdict = f6c.apply_gates(folds, "raw")
        self.assertEqual(verdict["failed"], ["FAILED_SAMPLE"])
        for gate in ("discrimination", "absolute_quality", "robustness",
                     "calibration", "holdout"):
            self.assertEqual(verdict["gates"][gate]["status"], "NOT_REACHED")

    def test_not_reached_is_distinct_from_fail(self):
        outcomes = [1.0, -1.0] * 15
        folds = [_fold_result([0.6] * 30, outcomes, 0.5, test_clusters=2)]
        statuses = {g["status"] for g in f6c.apply_gates(
            folds, "raw")["gates"].values()}
        self.assertIn("NOT_REACHED", statuses)
        self.assertNotIn("PASS", statuses - {"FAIL", "NOT_REACHED"})

    def test_the_floors_are_the_canonical_constants(self):
        self.assertIs(MIN_EFFECTIVE_SAMPLE,
                      ModelEvaluation.MIN_EFFECTIVE_SAMPLE)
        self.assertEqual(MIN_VALID_FOLDS, 3)

    def test_f6c_defines_no_threshold_of_its_own_beyond_no_skill_points(self):
        source = _code_only(os.path.join("scripts", "research_f6c.py"))
        import re
        self.assertIsNone(re.search(r"MIN_EFFECTIVE_SAMPLE\s*=\s*\d", source))
        self.assertIsNone(re.search(r"MIN_VALID_FOLDS\s*=\s*\d", source))
        self.assertIn("rs.MIN_EFFECTIVE_SAMPLE", source)
        self.assertIn("rs.MIN_VALID_FOLDS", source)

    def test_f6c_only_ever_adds_to_the_canonical_gate(self):
        """
        `f6c_qualified` requires the canonical `is_deployable` as well as
        every F.6C gate, so it cannot qualify anything canonical rejects.
        """
        source = _code_only(os.path.join("scripts", "research_f6c.py"))
        self.assertIn("canonically_qualified", source)
        self.assertIn("f6c_qualified", source)
        self.assertIn("is_deployable", source)


# ======================================================================
# The frozen plan is the plan that ran
# ======================================================================

@unittest.skipUnless(os.path.exists(PREREG), "pre-registration absent")
class TestFrozenPlan(unittest.TestCase):

    def test_the_preregistration_still_hashes_to_the_pinned_value(self):
        self.assertEqual(file_digest(PREREG), PINNED_PREREG)
        self.assertEqual(f6c.PREREGISTRATION_SHA256, PINNED_PREREG)

    def test_the_harness_refuses_to_run_against_an_edited_plan(self):
        source = _code_only(os.path.join("scripts", "research_f6c.py"))
        self.assertIn("PREREGISTRATION_SHA256", source)
        self.assertIn("FrozenPlanViolation", source)

    def test_the_frozen_lists_are_unchanged(self):
        self.assertEqual(f6c.HORIZONS, FROZEN_HORIZONS)
        self.assertEqual(f6c.FEATURE_CONFIGS, FROZEN_CONFIGS)
        self.assertEqual(f6c.METHODS, FROZEN_METHODS)
        self.assertEqual(f6c.CANDIDATE, "C1")

    def test_the_windows_and_minimums_are_unchanged(self):
        self.assertEqual(f6c.CALIBRATION_DAYS, 7)
        self.assertEqual(f6c.TEST_DAYS, 7)
        self.assertEqual(f6c.STEP_DAYS, 7)
        self.assertEqual(f6c.MIN_TRAIN_ROWS, 30)
        self.assertEqual(f6c.MIN_CALIBRATION_ROWS, 30)
        self.assertEqual(f6c.MIN_TEST_ROWS, 20)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_holdout_is_inherited_and_unchanged(self):
        context = f6c.preflight(V2_DB)
        self.assertEqual(context["holdout_boundary"], FROZEN_HOLDOUT)
        self.assertIn("inherited", context["holdout_origin"])
        self.assertEqual(
            {k: float(v) for k, v in context["purge_days"].items()},
            FROZEN_PURGE)
        self.assertEqual(context["embargo_days"], FROZEN_EMBARGO)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_exactly_twelve_cells_are_enumerated(self):
        context = f6c.preflight(V2_DB)
        cells = f6c.enumerate_cells(context)
        self.assertEqual(len(cells), FROZEN_BUDGET)
        expected = {f"{h}:{c}:{m}" for h in FROZEN_HORIZONS
                    for c in FROZEN_CONFIGS for m in FROZEN_METHODS}
        self.assertEqual({c["cell_id"] for c in cells}, expected)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_adding_a_horizon_or_method_breaks_the_budget_check(self):
        context = f6c.preflight(V2_DB)
        for key, extra in (("horizons", "d5"),
                           ("calibration_methods", "beta"),
                           ("feature_configs", "sparse_only")):
            tampered = dict(context)
            tampered[key] = list(context[key]) + [extra]
            if key == "horizons":
                tampered["purge_days"] = {**context["purge_days"], "d5": 7.0}
            with self.assertRaises(f6c.FrozenPlanViolation, msg=key):
                f6c.enumerate_cells(tampered)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_the_cell_list_hash_is_stable(self):
        context = f6c.preflight(V2_DB)
        cells = f6c.enumerate_cells(context)
        self.assertEqual(f6c.cell_list_hash(cells),
                         f6c.cell_list_hash(f6c.enumerate_cells(context)))

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_a_completed_execution_refuses_a_second_run(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT status FROM research_executions WHERE phase = ? "
                "AND status = 'complete'", (f6c.PHASE,)).fetchone()
        finally:
            conn.close()
        if not row:
            self.skipTest("no complete F.6C execution recorded yet")
        with self.assertRaises(f6c.FrozenPlanViolation):
            f6c.run(V2_DB, verbose=False)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_an_invalidated_run_is_kept_on_the_record_not_deleted(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT execution_id, status, note FROM research_executions "
                "WHERE phase = ? ORDER BY started_at", (f6c.PHASE,)).fetchall()
        finally:
            conn.close()
        if len(rows) < 2:
            self.skipTest("no invalidated run recorded")
        invalidated = [r for r in rows if r[1] == "invalidated"]
        self.assertTrue(invalidated, "the defective run should still be visible")
        self.assertIn("INVALIDATED", invalidated[0][2])

    def test_an_invalidation_must_state_a_reason(self):
        with self.assertRaises(f6c.FrozenPlanViolation):
            f6c.invalidate(V2_DB, reason="   ")


# ======================================================================
# F.6B is immutable
# ======================================================================

@unittest.skipUnless(os.path.exists(F6B_RESULTS), "F.6B results absent")
class TestF6BPreserved(unittest.TestCase):

    def test_f6bs_result_is_still_none_qualified(self):
        with open(F6B_RESULTS, encoding="utf-8") as handle:
            f6b = json.load(handle)
        self.assertEqual(f6b["status"], "complete")
        self.assertEqual(f6b["qualified_total"], 0)
        self.assertEqual(len(f6b["cells"]), 16)
        self.assertEqual(len(f6b["cells_evaluated"]), 16)
        self.assertFalse(f6b.get("qualified_cells"))

    def test_f6bs_holdout_boundary_is_untouched(self):
        with open(F6B_RESULTS, encoding="utf-8") as handle:
            f6b = json.load(handle)
        self.assertEqual(f6b["frozen_plan"]["holdout_boundary"],
                         FROZEN_HOLDOUT)

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_f6bs_candidate_rows_are_unchanged(self):
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            row = conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(qualified), 0)
                FROM research_candidate_results
                WHERE execution_id LIKE 'f6b-%'""").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (16, 0))

    @unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
    def test_preflight_refuses_if_f6b_changed(self):
        context = f6c.preflight(V2_DB)
        self.assertEqual(context["f6b_preserved"]["status"], "complete")
        self.assertEqual(context["f6b_preserved"]["qualified_count"], 0)
        self.assertEqual(context["f6b_preserved"]["candidate_rows"], 16)
        self.assertEqual(context["f6b_preserved"]["qualified_rows"], 0)


# ======================================================================
# Leakage, the protected horizon, and the production boundary
# ======================================================================

@unittest.skipUnless(os.path.exists(V2_DB), "snapshot absent")
class TestTemporalSeparation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from scripts.train_models import load_dataset
        cls.boundary = datetime.fromisoformat(FROZEN_HOLDOUT)
        conn = sqlite3.connect(f"file:{V2_DB}?mode=ro", uri=True)
        try:
            cls.data = {}
            for horizon in FROZEN_HORIZONS:
                names, X, Y, cutoffs, clusters, _, _ = load_dataset(
                    conn, f"{horizon}.abnormal_return")
                cls.data[horizon] = cutoffs
        finally:
            conn.close()

    def test_train_precedes_calibration_precedes_test_in_every_fold(self):
        for horizon, cutoffs in self.data.items():
            folds = f6c.build_f6c_folds(
                cutoffs, FROZEN_PURGE[horizon],
                holdout_start=self.boundary, embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                if not (fold["train_idx"] and fold["calibration_idx"]):
                    continue
                self.assertLess(max(cutoffs[i] for i in fold["train_idx"]),
                                min(cutoffs[i] for i in fold["calibration_idx"]),
                                f"{horizon} {fold['label']}")
                if fold["test_idx"]:
                    self.assertLess(
                        max(cutoffs[i] for i in fold["calibration_idx"]),
                        min(cutoffs[i] for i in fold["test_idx"]),
                        f"{horizon} {fold['label']}")

    def test_no_window_reaches_into_the_holdout(self):
        for horizon, cutoffs in self.data.items():
            folds = f6c.build_f6c_folds(
                cutoffs, FROZEN_PURGE[horizon],
                holdout_start=self.boundary, embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                for index in (fold["train_idx"] + fold["calibration_idx"]
                              + fold["test_idx"]):
                    self.assertLess(cutoffs[index], self.boundary,
                                    f"{horizon} {fold['label']}")

    def test_the_holdout_split_trains_and_calibrates_only_before_it(self):
        for horizon, cutoffs in self.data.items():
            split = f6c.holdout_split(
                cutoffs, FROZEN_PURGE[horizon],
                holdout_start=self.boundary, embargo_days=FROZEN_EMBARGO)
            for index in split["train_idx"] + split["calibration_idx"]:
                self.assertLess(cutoffs[index], self.boundary, horizon)
            for index in split["test_idx"]:
                self.assertGreaterEqual(cutoffs[index], self.boundary, horizon)

    def test_purge_is_applied_at_both_internal_boundaries(self):
        for horizon, cutoffs in self.data.items():
            purge_days = FROZEN_PURGE[horizon]
            folds = f6c.build_f6c_folds(
                cutoffs, purge_days, holdout_start=self.boundary,
                embargo_days=FROZEN_EMBARGO)
            for fold in folds:
                for index in fold["train_idx"]:
                    self.assertLessEqual(
                        cutoffs[index],
                        fold["calibration_start"] - timedelta(days=purge_days),
                        f"{horizon} {fold['label']} train/calibration purge")

    def test_fold_construction_is_deterministic(self):
        for horizon, cutoffs in self.data.items():
            first = f6c.build_f6c_folds(
                cutoffs, FROZEN_PURGE[horizon], holdout_start=self.boundary,
                embargo_days=FROZEN_EMBARGO)
            second = f6c.build_f6c_folds(
                cutoffs, FROZEN_PURGE[horizon], holdout_start=self.boundary,
                embargo_days=FROZEN_EMBARGO)
            self.assertEqual([f["train_idx"] for f in first],
                             [f["train_idx"] for f in second], horizon)


class TestProtectedAndProduction(unittest.TestCase):

    def test_the_protected_horizon_is_marked_and_not_researched(self):
        self.assertEqual(f6c.DEFERRED["d20"], "PROTECTED")
        self.assertNotIn("d20", f6c.HORIZONS)
        self.assertTrue(rs.is_protected("d20"))

    def test_no_protected_label_or_validator_is_referenced(self):
        for relative in (os.path.join("scripts", "research_f6c.py"),
                         os.path.join("src", "modeling", "calibration.py")):
            source = _code_only(relative)
            for forbidden in ("d20.abnormal_return", "d20.raw_return",
                              "validate_d20_reversal", "anchor_v2",
                              "return_60d", "check_d20_readiness",
                              "value_json"):
                self.assertNotIn(forbidden, source, f"{relative}: {forbidden}")

    def test_the_harness_touches_no_broker_or_execution_path(self):
        source = _code_only(os.path.join("scripts", "research_f6c.py"))
        for forbidden in ("OrderIntent", "place_order", "submit_order",
                          "ExecutionEngine", "BrokerClient", "PaperBroker",
                          "IBKRClient", "market_snapshot", "promote_model",
                          "PRODUCTION_ACTIVE"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_harness_imports_no_production_module(self):
        with open(os.path.join(ROOT, "scripts", "research_f6c.py"),
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

    def test_calibration_module_adds_no_dependency(self):
        with open(os.path.join(ROOT, "src", "modeling", "calibration.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported <= {"math", "dataclasses", "typing",
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
# The recorded result
# ======================================================================

@unittest.skipUnless(os.path.exists(RESULTS), "results absent")
class TestRecordedResult(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(RESULTS, encoding="utf-8") as handle:
            cls.result = json.load(handle)

    def test_every_frozen_cell_was_evaluated(self):
        self.assertEqual(len(self.result["cells"]), FROZEN_BUDGET)
        self.assertEqual(len(self.result["cells_evaluated"]), FROZEN_BUDGET)
        self.assertEqual(self.result["cells_unevaluated"], [])

    def test_the_holdout_was_not_opened_because_nothing_survived(self):
        self.assertEqual(self.result["pre_holdout_survivors"], [])
        self.assertFalse(self.result["holdout_opened"])

    def test_the_survivor_list_hash_is_recorded_even_when_empty(self):
        self.assertTrue(self.result["survivor_list_hash"])

    def test_no_candidate_carries_a_holdout_result(self):
        for candidate in self.result["candidates"]:
            self.assertIsNone(candidate["holdout"])
            self.assertFalse(candidate["f6c_qualified"])
            self.assertFalse(candidate["canonically_qualified"])

    def test_directional_probability_did_not_become_expected_return(self):
        self.assertFalse(self.result["expected_return_capable"])
        self.assertFalse(self.result["target_capable"])

    def test_abstention_is_reported_as_not_tested(self):
        self.assertEqual(self.result["abstention"], "NOT TESTED")

    def test_nothing_was_promoted_and_no_threshold_moved(self):
        self.assertEqual(self.result["models_promoted"], 0)
        self.assertEqual(self.result["thresholds_changed"], 0)
        self.assertEqual(self.result["broker_writes"], 0)
        self.assertEqual(self.result["market_data_requests"], 0)

    def test_every_candidate_retains_fold_level_evidence(self):
        for candidate in self.result["candidates"]:
            self.assertEqual(len(candidate["folds"]), 3)
            for fold in candidate["folds"]:
                self.assertIn("report", fold)
                self.assertIn("discrimination", fold["report"])
                self.assertIn("calibration", fold["report"])

    def test_ece_is_computed_with_support_or_says_why_not(self):
        for candidate in self.result["candidates"]:
            for fold in candidate["folds"]:
                status = fold["report"]["calibration"]["ece_status"]
                value = fold["report"]["calibration"][
                    "expected_calibration_error"]
                if status == "COMPUTED":
                    self.assertIsNotNone(value)
                else:
                    self.assertIsNone(value)
                    self.assertIn("NOT_COMPUTED", status)

    def test_the_result_carries_full_lineage(self):
        plan = self.result["frozen_plan"]
        self.assertEqual(plan["snapshot_id"], "research_snapshot_v2")
        self.assertEqual(plan["holdout_boundary"], FROZEN_HOLDOUT)
        self.assertEqual(plan["preregistration_hash"], PINNED_PREREG)
        self.assertTrue(plan["code_commit"])

    def test_discrimination_was_absent_and_the_record_shows_it(self):
        """
        The headline finding, asserted so it cannot be softened later:
        ROC-AUC sits on its no-skill point.
        """
        aucs = [fold["report"]["discrimination"]["roc_auc"]
                for candidate in self.result["candidates"]
                for fold in candidate["folds"]
                if fold["report"]["discrimination"]["roc_auc"] is not None]
        self.assertEqual(len(aucs), 36)
        self.assertLess(abs(sum(aucs) / len(aucs) - 0.5), 0.05)
        above = sum(1 for a in aucs if a > 0.5)
        self.assertEqual(above, 18, "a coin flip, exactly")


if __name__ == "__main__":
    unittest.main()
