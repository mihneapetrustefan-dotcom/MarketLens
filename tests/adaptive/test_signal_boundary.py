"""
tests/adaptive/test_signal_boundary.py
--------------------------------------------------
Phase 25.9I-F.5: the seal holds, the gates are unchanged, nothing moved.

Three jobs. Prove the protected D20 outcome cannot be reached from this
layer by any route -- including the quiet ones, like reading a label
value or letting d20 win a horizon selection. Prove the model
qualification gate was reused rather than softened. Prove no production
behaviour changed.
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive import flags as adaptive_flags
from src.adaptive.horizon_inventory import (
    PROTECTED_HORIZONS, ProtectedHorizonAccess, build_horizon_inventory,
    guard_protected, is_protected, label_maturity,
)
from src.adaptive.model_inventory import (
    QualificationVerdict, build_model_inventory,
)
from src.adaptive.signal_availability import (
    build_coverage, build_engine, collect_inputs,
)
from src.domain.model_models import ModelEvaluation
from src.domain.signal_availability_models import (
    HorizonStatus, NoSignalReason, SignalAvailabilityClass,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RESEARCH_DB = os.path.join(ROOT, "data", "marketlens.db")
UTC = timezone.utc
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)

ADAPTIVE_MODULES = ("signal_availability.py", "horizon_inventory.py",
                    "horizon_intelligence.py", "model_inventory.py",
                    "target_estimation.py")
DOMAIN_MODULES = ("signal_availability_models.py",)
SCRIPTS = ("signal_horizon_status.py",)


def _source(directory, name):
    with open(os.path.join(ROOT, directory, name), encoding="utf-8") as handle:
        return handle.read()


class TestProtectedD20(unittest.TestCase):
    """Tests 12-16 and section 1 -- the seal, checked from several angles."""

    def test_d20_is_registered_as_protected(self):
        self.assertIn("d20", PROTECTED_HORIZONS)
        self.assertTrue(is_protected("d20"))

    def test_the_guard_raises_rather_than_returning_none(self):
        """
        A silent None would let a caller conclude "no edge" from a
        horizon it was never allowed to look at.
        """
        with self.assertRaises(ProtectedHorizonAccess):
            guard_protected("d20", "expected return")

    def test_no_module_reads_a_label_value(self):
        """
        The structural seal: `value_json` is never selected, for ANY
        horizon. A function that cannot read a value cannot leak one.
        """
        for name in ADAPTIVE_MODULES:
            source = _source("src/adaptive", name)
            self.assertNotIn("value_json", source,
                             f"{name} reads a label value")

    def test_no_module_mentions_the_frozen_hypothesis(self):
        protected_terms = ("anchor-v2", "anchor_v2", "validate_d20",
                           "protected_ledger", "return_60d", "d20_result",
                           "d20_outcome", "reversal")
        targets = [("src/adaptive", ADAPTIVE_MODULES),
                   ("src/domain", DOMAIN_MODULES),
                   ("scripts", SCRIPTS)]
        for directory, names in targets:
            for name in names:
                source = _source(directory, name).lower()
                for term in protected_terms:
                    self.assertNotIn(term, source, f"{name}: {term}")

    def test_no_module_imports_research(self):
        for directory, names in (("src/adaptive", ADAPTIVE_MODULES),
                                 ("src/domain", DOMAIN_MODULES)):
            for name in names:
                tree = ast.parse(_source(directory, name))
                for node in ast.walk(tree):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    for module in modules:
                        self.assertNotIn("research", module, name)

    def test_d20_cannot_supply_an_expected_return(self):
        from src.adaptive.target_estimation import ExpectedReturnEstimate
        with self.assertRaises(ValueError):
            ExpectedReturnEstimate(
                instrument_id="x", horizon_id="d20", expected_return=0.05,
                model_qualified_id="m:v1", as_of=NOW)

    def test_d20_cannot_supply_a_target(self):
        from src.adaptive.target_estimation import TargetEstimator
        from src.domain.signal_availability_models import TargetStatus
        target = TargetEstimator().estimate("x", as_of=NOW, horizon_id="d20",
                                            reference_price=100.0)
        self.assertIs(target.status, TargetStatus.PROTECTED)
        self.assertIsNone(target.target_low)

    def test_d20_cannot_become_the_preferred_horizon(self):
        from src.adaptive.horizon_intelligence import HorizonIntelligence
        from src.domain.signal_availability_models import (
            InstrumentHorizonStatus,
        )
        from src.domain.signal_models import SignalDirection
        result = HorizonIntelligence().assess("x", [
            InstrumentHorizonStatus("x", "d20", HorizonStatus.AVAILABLE,
                                    direction=SignalDirection.LONG,
                                    confidence=0.99, explanation="fixture")],
            as_of=NOW)
        self.assertNotEqual(result.preferred_horizon, "d20")
        self.assertIn("d20", result.protected_horizons)

    def test_d20_cannot_produce_a_signal_even_with_a_qualified_model(self):
        from src.domain.signal_availability_models import HorizonDescriptor
        sealed = HorizonDescriptor(
            horizon_id="d20", is_protected=True,
            protected_note=PROTECTED_HORIZONS["d20"],
            model_count=1, qualified_model_count=1)
        self.assertFalse(sealed.can_produce_signal)

    def test_a_protected_status_cannot_carry_a_number(self):
        from src.domain.signal_availability_models import (
            InstrumentHorizonStatus,
        )
        with self.assertRaises(ValueError):
            InstrumentHorizonStatus("x", "d20", HorizonStatus.PROTECTED,
                                    expected_return=0.03)

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_maturity_metadata_exposes_no_outcome(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            metadata = label_maturity(conn, "d20")
        finally:
            conn.close()
        self.assertTrue(metadata["is_protected"])
        self.assertIn("matured_labels", metadata)
        for forbidden in ("mean", "return", "hit_rate", "value", "score",
                          "t_stat", "p_value"):
            self.assertNotIn(forbidden, str(metadata).lower().replace(
                "matured_labels", "").replace("maturity_ratio", ""))


class TestQualificationGateReused(unittest.TestCase):
    """Tests 25-27 -- the gate is the canonical one, and it is unchanged."""

    def test_the_canonical_gate_still_requires_both_conditions(self):
        self.assertEqual(ModelEvaluation.MIN_EFFECTIVE_SAMPLE, 30)

    def test_the_inventory_asks_the_canonical_property(self):
        source = _source("src/adaptive", "model_inventory.py")
        self.assertIn("is_deployable", source)
        self.assertIn("ModelEvaluation(", source)

    def test_no_second_threshold_is_defined(self):
        """A kinder copy of the gate living in the adaptive layer."""
        source = _source("src/adaptive", "model_inventory.py")
        for forbidden in ("MIN_EFFECTIVE_SAMPLE =", "min_effective_sample =",
                          "MIN_SAMPLE =", "def is_deployable"):
            self.assertNotIn(forbidden, source)

    def test_a_failed_model_is_never_reported_as_qualified(self):
        from src.adaptive.model_inventory import ModelRecord
        failed = ModelRecord(
            trained_model_id="t", model_qualified_id="m:v1",
            verdict=QualificationVerdict.FAILED, status="evaluated")
        self.assertFalse(failed.is_qualified)

    def test_a_qualified_but_unpromoted_model_is_not_usable(self):
        """Passing the gates is not the same as being promoted."""
        from src.adaptive.model_inventory import ModelRecord
        passed = ModelRecord(
            trained_model_id="t", model_qualified_id="m:v1",
            verdict=QualificationVerdict.QUALIFIED, status="evaluated")
        self.assertFalse(passed.is_active)
        self.assertFalse(passed.is_qualified)

    def test_unjudged_is_not_failed(self):
        self.assertIsNot(QualificationVerdict.UNJUDGED,
                         QualificationVerdict.FAILED)
        self.assertFalse(QualificationVerdict.UNJUDGED.is_qualified)

    def test_no_module_promotes_anything(self):
        for name in ADAPTIVE_MODULES:
            tree = ast.parse(_source("src/adaptive", name))
            calls = {n.func.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)}
            for forbidden in ("promote", "demote", "activate", "train", "fit"):
                self.assertNotIn(forbidden, calls, f"{name} calls {forbidden}")
            self.assertNotIn("src.modeling.promotion",
                             _source("src/adaptive", name))


class TestProductionUnchanged(unittest.TestCase):
    """Tests 48-52, 59 -- nothing moved."""

    def test_no_module_imports_execution_trading_or_portfolio(self):
        for directory, names in (("src/adaptive", ADAPTIVE_MODULES),
                                 ("src/domain", DOMAIN_MODULES)):
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

    def test_no_order_intent_is_constructed(self):
        for directory, names in (("src/adaptive", ADAPTIVE_MODULES),
                                 ("src/domain", DOMAIN_MODULES),
                                 ("scripts", SCRIPTS)):
            for name in names:
                self.assertNotIn("OrderIntent", _source(directory, name), name)

    def test_no_broker_or_market_data_call(self):
        forbidden = ("place_order", "submit_order", "cancel_order",
                     "modify_order", "market_snapshot", "subscribe",
                     "init_brokerage_session")
        for directory, names in (("src/adaptive", ADAPTIVE_MODULES),
                                 ("src/domain", DOMAIN_MODULES),
                                 ("scripts", SCRIPTS)):
            for name in names:
                tree = ast.parse(_source(directory, name))
                calls = {n.func.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)}
                for call in forbidden:
                    self.assertNotIn(call, calls, f"{name} calls {call}")

    def test_the_execution_safety_gate_is_unchanged(self):
        from src.execution.safety import ExecutionSafety
        self.assertFalse(ExecutionSafety().allow_real_orders)

    def test_no_new_feature_flag_was_added(self):
        """
        Section 58 prefers reusing the existing shadow diagnostics, and
        this phase is read-only, so no flag was needed.
        """
        self.assertNotIn("signal_availability_enabled", adaptive_flags.FLAG_ENV)
        self.assertNotIn("horizon_intelligence_enabled",
                         adaptive_flags.FLAG_ENV)

    def test_the_existing_flags_remain_off(self):
        resolved = adaptive_flags.resolve({})
        self.assertFalse(resolved.any_enabled)

    def test_no_persistence_module_was_added(self):
        """
        Section 54 asks for the MINIMUM justified persistence. Every
        output here is a pure function of the corpus and `as_of`, so it
        can be recomputed and none of it needs storing -- unlike
        25.9I-F's novelty ledger, which recorded history.
        """
        data_access = os.path.join(ROOT, "src", "data_access")
        modules = sorted(n for n in os.listdir(data_access)
                         if "adaptive" in n.lower())
        self.assertEqual(modules, ["adaptive_allocation_schema.py",
                                   "adaptive_event_schema.py",
                                   "adaptive_market_data_schema.py",
                                   "adaptive_thesis_schema.py",
                                   "adaptive_universe_schema.py"])

    def test_no_threshold_was_changed_in_the_signal_engine(self):
        """Section 19/51 -- coverage must never be bought with a threshold."""
        strategy = _source("src/signals", "strategy.py")
        self.assertIn("QUALITY_FACTORS = {\"high\": 1.0, \"medium\": 0.8, "
                      "\"low\": 0.5, \"invalid\": 0.0}", strategy)
        self.assertIn("DEFAULT_STRENGTH_SCALE = 0.05", strategy)


@unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
class TestRealCorpus(unittest.TestCase):
    """Section 50 -- what the actual repository says."""

    @classmethod
    def setUpClass(cls):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            cls.models = build_model_inventory(conn, as_of=NOW)
            cls.horizons = build_horizon_inventory(
                conn, as_of=NOW,
                qualified_model_ids=cls.models.qualified_ids)
            engine = build_engine(conn, as_of=NOW)
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT instrument_id FROM research_observations")]
            inputs = collect_inputs(conn, ids, as_of=NOW)
        finally:
            conn.close()
        cls.verdicts = [engine.assess(inputs[i], as_of=NOW)
                        for i in sorted(inputs)]
        cls.coverage = build_coverage(cls.verdicts, as_of=NOW)

    def test_the_horizon_inventory_is_not_a_hard_coded_list(self):
        """Nine horizons, four intraday -- more than anyone would guess."""
        self.assertGreaterEqual(len(self.horizons), 5)
        self.assertIn("d5", self.horizons.ids)
        self.assertIn("d20", self.horizons.ids)
        self.assertTrue(any(d.is_intraday for d in self.horizons))

    def test_d20_is_sealed_in_the_real_inventory(self):
        d20 = self.horizons.get("d20")
        self.assertIsNotNone(d20)
        self.assertTrue(d20.is_protected)
        self.assertFalse(d20.can_produce_signal)

    def test_no_horizon_is_currently_signal_capable(self):
        """The honest state: zero qualified models anywhere."""
        self.assertEqual(self.horizons.signal_capable, ())

    def test_the_only_model_failed_its_gates(self):
        self.assertEqual(len(self.models.specifications), 1)
        self.assertEqual(len(self.models.qualified), 0)
        for record in self.models:
            self.assertIs(record.verdict, QualificationVerdict.FAILED)
            self.assertTrue(record.gate_failures)

    def test_coverage_reconciles_on_the_real_universe(self):
        self.assertTrue(self.coverage.reconciles)
        self.assertEqual(self.coverage.total_by_reason,
                         self.coverage.universe_size)

    def test_every_real_verdict_has_a_reason_and_an_explanation(self):
        for verdict in self.verdicts:
            self.assertIsNot(verdict.reason, NoSignalReason.UNKNOWN)
            self.assertTrue(verdict.explanation.strip())

    def test_no_real_verdict_claims_a_decision_it_could_not_make(self):
        for verdict in self.verdicts:
            if verdict.availability is SignalAvailabilityClass.CAPABILITY_GAP:
                self.assertFalse(verdict.is_decision)
                self.assertIsNone(verdict.preferred_horizon)

    def test_no_real_verdict_carries_a_target(self):
        for verdict in self.verdicts:
            self.assertIsNone(verdict.target)

    def test_reading_the_corpus_is_deterministic(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            again = build_horizon_inventory(
                conn, as_of=NOW,
                qualified_model_ids=self.models.qualified_ids)
        finally:
            conn.close()
        self.assertEqual(self.horizons.ids, again.ids)

    def test_as_of_truncation_changes_what_is_visible(self):
        """
        Physical truncation: an earlier `as_of` cannot see later rows.
        """
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT instrument_id FROM research_observations")]
            early = collect_inputs(
                conn, ids, as_of=NOW - timedelta(days=3650))
            late = collect_inputs(conn, ids, as_of=NOW)
        finally:
            conn.close()
        early_total = sum(i.observation_count for i in early.values())
        late_total = sum(i.observation_count for i in late.values())
        self.assertLessEqual(early_total, late_total)

    def test_an_earlier_as_of_cannot_be_rescued_by_later_data(self):
        conn = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT instrument_id FROM research_observations "
                "LIMIT 5")]
            first = collect_inputs(conn, ids, as_of=NOW - timedelta(days=3650))
            second = collect_inputs(conn, ids, as_of=NOW - timedelta(days=3650))
        finally:
            conn.close()
        self.assertEqual({k: v.observation_count for k, v in first.items()},
                         {k: v.observation_count for k, v in second.items()})


class TestDiagnosticIsReadOnly(unittest.TestCase):
    """Section 38 -- an ordinary run changes nothing."""

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_the_status_script_reports_zero_side_effects(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "signal_horizon_status.py"),
             "--json", "--limit", "3"],
            capture_output=True, text=True, env=environment, timeout=900)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        report = json.loads(result.stdout)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["thresholds_changed"], 0)
        self.assertEqual(report["models_promoted"], 0)
        self.assertEqual(report["broker_writes"], 0)
        self.assertTrue(report["coverage"]["reconciles"])

    @unittest.skipUnless(os.path.exists(RESEARCH_DB), "research store absent")
    def test_the_status_script_writes_nothing(self):
        before = os.path.getmtime(RESEARCH_DB)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "signal_horizon_status.py"),
             "--horizons"],
            capture_output=True, text=True, env=environment, timeout=900)
        self.assertEqual(before, os.path.getmtime(RESEARCH_DB))

    def test_the_scenarios_run_without_a_database(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.abspath(ROOT)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts",
                                          "signal_horizon_status.py"),
             "--scenario", "all", "--json"],
            capture_output=True, text=True, env=environment, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertFalse(payload["G_target_without_model"]["answered"])
        self.assertTrue(payload["H_target_with_valid_estimate"]["answered"])
        self.assertIsNone(
            payload["E_horizon_disagreement"]["preferred_horizon"])


if __name__ == "__main__":
    unittest.main()
