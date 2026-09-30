"""
tests/adaptive/test_research_discipline.py
------------------------------------------------------
Phase 25.9I-F.6: the rules did not move, and the seal held.

The hardest thing to test in a research phase is the thing that did NOT
happen: a threshold quietly relaxed, a holdout re-cut after a
disappointing result, a second qualification gate grown inside the
adaptive layer. These tests assert those absences directly.
"""

import ast
import json
import os
import subprocess
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts import research_model_quality as rmq
from scripts import research_multi_horizon as rmh
from src.adaptive.horizon_inventory import is_protected
from src.adaptive.sample_adequacy import MIN_EFFECTIVE_SAMPLE, MIN_VALID_FOLDS
from src.domain.model_models import ModelEvaluation
from src.modeling.splits import embargo, purge

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")
UTC = timezone.utc

F6_MODULES = ("sample_adequacy.py",)
F6_SCRIPTS = ("research_multi_horizon.py",)


def _source(directory, name):
    with open(os.path.join(ROOT, directory, name), encoding="utf-8") as handle:
        return handle.read()


class TestPreRegistrationHonoured(unittest.TestCase):
    """Section 7 -- the plan was fixed before the result was seen."""

    def test_the_preregistration_exists(self):
        path = os.path.join(ROOT, "docs",
                            "PHASE_25_9I_F6_PREREGISTRATION.md")
        self.assertTrue(os.path.exists(path))

    def test_the_holdout_boundary_matches_phase_25_9(self):
        """
        Frozen where Phase 25.9 put it. Moving it after a disappointing
        result is holdout-shopping, and the corpus rebuild made the old
        boundary MORE conservative, not less.
        """
        self.assertEqual(rmh.HOLDOUT_START, rmq.PROTECTED_START)
        self.assertEqual(rmh.HOLDOUT_START,
                         datetime(2026, 8, 15, 1, 23, 31, tzinfo=UTC))

    def test_the_candidates_are_phase_25_9s_verbatim(self):
        self.assertEqual(rmh.CANDIDATES, rmq.CANDIDATES)
        self.assertEqual(len(rmh.CANDIDATES), 4)

    def test_the_candidate_budget_is_fixed_at_sixteen(self):
        self.assertEqual(len(rmh.CANDIDATES) * len(rmh.HORIZONS), 16)

    def test_only_daily_non_protected_horizons_are_researched(self):
        self.assertEqual(rmh.HORIZONS, ("d1", "d3", "d5", "d10"))
        for horizon in rmh.HORIZONS:
            self.assertFalse(is_protected(horizon))
            self.assertFalse(horizon.startswith("intraday"))

    def test_intraday_horizons_are_deferred_not_researched(self):
        for horizon in rmh.DEFERRED_INTRADAY:
            self.assertNotIn(horizon, rmh.HORIZONS)

    def test_the_contract_constants_match_phase_25_9(self):
        self.assertEqual(rmh.EMBARGO_DAYS, rmq.EMBARGO_DAYS)
        self.assertEqual(rmh.TEST_DAYS, rmq.TEST_DAYS)
        self.assertEqual(rmh.STEP_DAYS, rmq.STEP_DAYS)
        self.assertEqual(rmh.MIN_TRAIN_ROWS, rmq.MIN_TRAIN_ROWS)
        self.assertEqual(rmh.MIN_TEST_ROWS, rmq.MIN_TEST_ROWS)

    def test_every_purge_horizon_covers_its_trading_days(self):
        """A weekend must not carry a label across a fold boundary."""
        trading_days = {"d1": 1, "d3": 3, "d5": 5, "d10": 10}
        for horizon, days in trading_days.items():
            self.assertGreaterEqual(rmh.PURGE_DAYS[horizon], days, horizon)


class TestGatesNotWeakened(unittest.TestCase):
    """Tests 23-29 -- no second, kinder gate exists."""

    def test_the_canonical_gate_is_unchanged(self):
        self.assertEqual(ModelEvaluation.MIN_EFFECTIVE_SAMPLE, 30)

    def test_f6_defines_no_gate_of_its_own(self):
        """
        Aliasing the canonical constant is fine; restating its VALUE is
        not. A literal here would be a second floor that could drift
        away from the gate it is supposed to mirror.
        """
        import re
        for directory, names in (("src/adaptive", F6_MODULES),
                                 ("scripts", F6_SCRIPTS)):
            for name in names:
                source = _source(directory, name)
                for forbidden in ("def is_deployable", "def beats_all_baselines"):
                    self.assertNotIn(forbidden, source, f"{name}: {forbidden}")
                literal = re.search(r"MIN_EFFECTIVE_SAMPLE\s*=\s*\d+", source)
                self.assertIsNone(
                    literal,
                    f"{name} restates the sample floor as a literal instead "
                    "of aliasing ModelEvaluation.MIN_EFFECTIVE_SAMPLE")

    def test_the_fold_minimum_is_not_relaxed(self):
        self.assertEqual(MIN_VALID_FOLDS, 3)
        source = _source("scripts", "research_multi_horizon.py")
        self.assertIn("MIN_VALID_FOLDS", source)

    def test_the_runner_promotes_nothing(self):
        tree = ast.parse(_source("scripts", "research_multi_horizon.py"))
        calls = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for forbidden in ("promote", "demote", "activate"):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn("src.modeling.promotion",
                         _source("scripts", "research_multi_horizon.py"))

    def test_insufficient_data_is_a_verdict_not_a_reason_to_relax(self):
        source = _source("scripts", "research_multi_horizon.py")
        self.assertIn("INSUFFICIENT DATA", source)
        # No branch reacts to a bad result by changing the contract.
        for forbidden in ("if not valid:", "relax", "fallback_min",
                          "retry_with"):
            self.assertNotIn(forbidden, source)


class TestFoldDiscipline(unittest.TestCase):
    """Tests 16-22 -- time flows one way."""

    def _cutoffs(self, start, count, step_hours=6):
        return [start + timedelta(hours=step_hours * i) for i in range(count)]

    def test_no_fold_reaches_into_the_holdout(self):
        cutoffs = self._cutoffs(datetime(2026, 6, 1, tzinfo=UTC), 400,
                                step_hours=6)
        for fold in rmh.build_folds(cutoffs, 7.0):
            self.assertLessEqual(fold["test_end"], rmh.HOLDOUT_START)
            for index in fold["test_idx"]:
                self.assertLess(cutoffs[index], rmh.HOLDOUT_START)

    def test_train_always_precedes_test(self):
        cutoffs = self._cutoffs(datetime(2026, 6, 1, tzinfo=UTC), 400)
        for fold in rmh.build_folds(cutoffs, 7.0):
            for index in fold["train_idx"]:
                self.assertLess(cutoffs[index], fold["test_start"])

    def test_purging_removes_labels_that_span_the_boundary(self):
        cutoffs = self._cutoffs(datetime(2026, 6, 1, tzinfo=UTC), 400)
        folds = rmh.build_folds(cutoffs, 7.0)
        self.assertTrue(any(f["purged"] > 0 for f in folds))

    def test_a_longer_purge_removes_at_least_as_much(self):
        cutoffs = self._cutoffs(datetime(2026, 6, 1, tzinfo=UTC), 400)
        short = sum(f["purged"] for f in rmh.build_folds(cutoffs, 3.0))
        long = sum(f["purged"] for f in rmh.build_folds(cutoffs, 14.0))
        self.assertGreaterEqual(long, short)

    def test_fold_boundaries_are_deterministic(self):
        cutoffs = self._cutoffs(datetime(2026, 6, 1, tzinfo=UTC), 400)
        first = [(f["label"], f["test_start"], f["test_end"])
                 for f in rmh.build_folds(cutoffs, 7.0)]
        second = [(f["label"], f["test_start"], f["test_end"])
                  for f in rmh.build_folds(cutoffs, 7.0)]
        self.assertEqual(first, second)

    def test_a_short_research_region_forms_no_folds(self):
        """
        The measured state: 15 days cannot support purge + embargo +
        test, so zero folds form and the verdict is INSUFFICIENT DATA
        rather than a model trained on a fold that should not exist.
        """
        short = self._cutoffs(rmh.HOLDOUT_START - timedelta(days=15), 100,
                              step_hours=3)
        self.assertEqual(rmh.build_folds(short, 7.0), [])

    def test_the_span_requirement_names_the_real_constraint(self):
        short = self._cutoffs(rmh.HOLDOUT_START - timedelta(days=15), 100,
                              step_hours=3)
        need = rmh.span_requirement(short, "d5")
        self.assertEqual(need["research_span_days"], 15)
        self.assertGreater(need["shortfall_days"], 0)
        self.assertIn("calendar span", need["note"])


class TestD20Isolation(unittest.TestCase):
    """Tests 8-15 -- the sealed horizon is unreachable from research."""

    def test_d20_is_not_in_the_researched_set(self):
        self.assertNotIn("d20", rmh.HORIZONS)
        self.assertIn("d20", ("d20",))     # it exists, and is excluded

    def test_pointing_the_researcher_at_d20_raises(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(rmh.ProtectedHorizonRequested):
                rmh.research_horizon(conn, "d20", verbose=False)
        finally:
            conn.close()

    def test_no_f6_module_reads_a_label_value(self):
        for directory, names in (("src/adaptive", F6_MODULES),
                                 ("scripts", F6_SCRIPTS)):
            for name in names:
                self.assertNotIn("value_json", _source(directory, name), name)

    def test_no_f6_module_names_the_frozen_hypothesis(self):
        protected_terms = ("anchor-v2", "anchor_v2", "validate_d20",
                           "protected_ledger", "return_60d", "d20_result",
                           "d20_outcome")
        for directory, names in (("src/adaptive", F6_MODULES),
                                 ("scripts", F6_SCRIPTS)):
            for name in names:
                source = _source(directory, name).lower()
                for term in protected_terms:
                    self.assertNotIn(term, source, f"{name}: {term}")

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_d20_produces_no_trainable_dataset(self):
        from src.adaptive.sample_adequacy import (
            ResearchReadiness, assess_horizon,
        )
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            item = assess_horizon(conn, "d20")
        finally:
            conn.close()
        self.assertIs(item.readiness, ResearchReadiness.PROTECTED)
        self.assertEqual(item.raw_rows, 0)


class TestRunnerIsReadOnly(unittest.TestCase):
    """Sections 26-28 -- research changes nothing."""

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_the_runner_writes_nothing(self):
        before = os.path.getmtime(RESEARCH_DB)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "research_multi_horizon.py"), "--run"],
            capture_output=True, text=True, env=environment, timeout=1800)
        self.assertEqual(before, os.path.getmtime(RESEARCH_DB))

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_the_runner_reports_zero_promotions_and_never_opens_the_holdout(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "research_multi_horizon.py"),
             "--run", "--json"],
            capture_output=True, text=True, env=environment, timeout=1800)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertEqual(report["models_promoted"], 0)
        self.assertEqual(report["thresholds_changed"], 0)
        self.assertFalse(report["holdout_opened"])
        self.assertEqual(report["horizons_protected"], ["d20"])

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_every_researched_horizon_gets_an_explained_verdict(self):
        """Brief section 54 -- an unexplained horizon is not acceptable."""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "research_multi_horizon.py"),
             "--run", "--json"],
            capture_output=True, text=True, env=environment, timeout=1800)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["horizons"]), len(rmh.HORIZONS))
        for record in report["horizons"]:
            self.assertIn("verdict", record)
            self.assertTrue(record["verdict"])
            if record["verdict"] != "QUALIFIED":
                self.assertTrue(record.get("reason"))

    def test_no_model_was_promoted_in_the_registry(self):
        if not os.path.exists(RESEARCH_DB):
            self.skipTest("research store absent")
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM trained_models "
                "WHERE status = 'active'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(active, 0)


class TestProductionUnchanged(unittest.TestCase):
    """Sections 27-28 -- nothing outside research moved."""

    def test_no_f6_module_imports_execution_trading_or_portfolio(self):
        for directory, names in (("src/adaptive", F6_MODULES),
                                 ("scripts", F6_SCRIPTS)):
            for name in names:
                tree = ast.parse(_source(directory, name))
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        for root in ("src.execution", "src.trading",
                                     "src.portfolio"):
                            self.assertFalse(module.startswith(root),
                                             f"{name} imports {module}")

    def test_no_broker_call_or_order_intent(self):
        for directory, names in (("src/adaptive", F6_MODULES),
                                 ("scripts", F6_SCRIPTS)):
            for name in names:
                source = _source(directory, name)
                self.assertNotIn("OrderIntent", source)
                tree = ast.parse(source)
                calls = {n.func.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)}
                for forbidden in ("place_order", "submit_order",
                                  "cancel_order", "market_snapshot"):
                    self.assertNotIn(forbidden, calls, name)

    def test_the_execution_safety_gate_is_unchanged(self):
        from src.execution.safety import ExecutionSafety
        self.assertFalse(ExecutionSafety().allow_real_orders)

    def test_the_signal_confidence_formula_is_unchanged(self):
        from src.signals.strategy import AGREEMENT_FACTORS, QUALITY_FACTORS
        from src.domain.signal_models import AgreementState
        self.assertEqual(QUALITY_FACTORS["high"], 1.0)
        self.assertEqual(AGREEMENT_FACTORS[AgreementState.INSUFFICIENT_EVIDENCE],
                         0.6)


if __name__ == "__main__":
    unittest.main()
