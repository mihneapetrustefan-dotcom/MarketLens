"""
tests/adaptive/test_signal_availability.py
------------------------------------------------------
Phase 25.9I-F.5: every blank answer now has a reason behind it.

The assertion this file exists for is the one separating a DECISION
from a CAPABILITY GAP. "I found no edge" and "I have no model" both
render as an empty dashboard cell today, and only the first is a
finding about the market.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.adaptive.horizon_intelligence import (
    HorizonIntelligence, HorizonPolicy, horizon_answer_for_request,
)
from src.adaptive.horizon_inventory import (
    HorizonInventory, PROTECTED_HORIZONS, ProtectedHorizonAccess,
    guard_protected, is_protected,
)
from src.adaptive.model_inventory import (
    GATE_BEATS_BASELINES, GATE_NOT_ACTIVE, GATE_SAMPLE, ModelInventory,
    ModelRecord, QualificationVerdict,
)
from src.adaptive.signal_availability import (
    AvailabilityPolicy, InstrumentInputs, SignalAvailabilityEngine,
    build_coverage,
)
from src.adaptive.target_estimation import (
    ExpectedReturnEstimate, TargetEstimator, TargetPolicy,
    target_answer_for_request,
)
from src.domain.signal_availability_models import (
    HorizonAgreement, HorizonDescriptor, HorizonStatus,
    InstrumentHorizonStatus, NoSignalReason, SUPPRESSION_TO_REASON,
    SignalAvailabilityClass, TargetStatus,
)
from src.domain.signal_models import SignalDirection, SuppressionReason

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)


def descriptor(horizon_id, **kw):
    base = dict(horizon_id=horizon_id, label_count=400, model_count=0,
                qualified_model_count=0)
    if horizon_id == "d20":
        base.update(is_protected=True,
                    protected_note=PROTECTED_HORIZONS["d20"])
    base.update(kw)
    return HorizonDescriptor(**base)


def model(horizon_id="d5", *, qualified=False, active=False, **kw):
    base = dict(
        trained_model_id="tm-1", model_qualified_id="m:v1", family="ridge",
        task="abnormal_return", label_name=f"{horizon_id}.abnormal_return",
        horizon_id=horizon_id, status="active" if active else "evaluated",
        verdict=(QualificationVerdict.QUALIFIED if qualified
                 else QualificationVerdict.FAILED),
        gate_failures=() if qualified else (GATE_SAMPLE, GATE_BEATS_BASELINES))
    base.update(kw)
    return ModelRecord(**base)


def engine(horizons=("d5",), models=(), policy=None, **inventory_kw):
    return SignalAvailabilityEngine(
        HorizonInventory(tuple(descriptor(h, **inventory_kw.get(h, {}))
                               for h in horizons)),
        ModelInventory(tuple(models)), policy or AvailabilityPolicy())


def inputs(**kw):
    base = dict(instrument_id="inst-a", sector_id="tech",
                observation_count=200, feature_count=5200)
    base.update(kw)
    return InstrumentInputs(**base)


class TestReasonTaxonomy(unittest.TestCase):
    """Tests 1-4 -- a decision and a gap are different things."""

    def test_a_missing_model_is_not_a_decision(self):
        self.assertFalse(NoSignalReason.NO_MODEL_AVAILABLE.is_decision)
        self.assertIs(NoSignalReason.NO_MODEL_AVAILABLE.availability_class,
                      SignalAvailabilityClass.CAPABILITY_GAP)

    def test_an_unqualified_model_is_not_a_decision(self):
        self.assertFalse(NoSignalReason.MODEL_NOT_QUALIFIED.is_decision)

    def test_low_confidence_is_a_decision(self):
        self.assertTrue(NoSignalReason.LOW_CONFIDENCE.is_decision)
        self.assertIs(NoSignalReason.LOW_CONFIDENCE.availability_class,
                      SignalAvailabilityClass.VALID_NO_SIGNAL)

    def test_a_pipeline_error_is_never_a_valid_no_signal(self):
        """Test 8 -- a crash must not look like a considered abstention."""
        self.assertIs(NoSignalReason.PIPELINE_ERROR.availability_class,
                      SignalAvailabilityClass.ERROR)
        self.assertFalse(NoSignalReason.PIPELINE_ERROR.is_decision)

    def test_a_protected_horizon_is_its_own_class(self):
        self.assertIs(
            NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE.availability_class,
            SignalAvailabilityClass.PROTECTED)
        self.assertFalse(
            NoSignalReason.PROTECTED_HORIZON_UNAVAILABLE.is_decision)

    def test_the_funnel_order_puts_models_before_staleness(self):
        """A prediction can only be stale if a model made one."""
        self.assertLess(NoSignalReason.NO_MODEL_AVAILABLE.order,
                        NoSignalReason.MODEL_NOT_QUALIFIED.order)
        self.assertLess(NoSignalReason.MODEL_NOT_QUALIFIED.order,
                        NoSignalReason.STALE_DATA.order)
        self.assertLess(NoSignalReason.NO_DATA.order,
                        NoSignalReason.MISSING_FEATURES.order)

    def test_every_suppression_reason_maps_to_this_taxonomy(self):
        for reason in SuppressionReason:
            self.assertIn(reason, SUPPRESSION_TO_REASON, reason.value)

    def test_the_class_must_match_the_reason(self):
        from src.domain.signal_availability_models import SignalAvailability
        with self.assertRaises(ValueError):
            SignalAvailability(
                instrument_id="x", as_of=NOW,
                availability=SignalAvailabilityClass.VALID_NO_SIGNAL,
                reason=NoSignalReason.NO_MODEL_AVAILABLE,
                explanation="mislabelled")


class TestFunnelBlockers(unittest.TestCase):
    """Tests 1-7 -- the FIRST blocker is reported, and it names the repair."""

    def test_no_data_beats_no_model(self):
        verdict = engine().assess(
            inputs(observation_count=0, feature_count=0), as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.NO_DATA)
        self.assertIn("research_observations", verdict.missing_inputs)

    def test_insufficient_history_is_reported_with_numbers(self):
        verdict = engine(policy=AvailabilityPolicy(min_observations=120)).assess(
            inputs(observation_count=43), as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.INSUFFICIENT_HISTORY)
        self.assertIn("43", verdict.explanation)
        self.assertIn("120", verdict.explanation)

    def test_missing_features_is_reported(self):
        verdict = engine().assess(inputs(feature_count=0), as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.MISSING_FEATURES)
        self.assertIn("research_features", verdict.missing_inputs)

    def test_no_model_is_distinguished_from_unqualified_model(self):
        no_model = engine(horizons=("d5",), models=()).assess(
            inputs(), as_of=NOW)
        self.assertIs(no_model.reason, NoSignalReason.NO_MODEL_AVAILABLE)

        unqualified = engine(horizons=("d5",), models=(model(),)).assess(
            inputs(), as_of=NOW)
        self.assertIs(unqualified.reason, NoSignalReason.MODEL_NOT_QUALIFIED)
        self.assertNotEqual(no_model.reason, unqualified.reason)

    def test_an_unqualified_model_exposes_its_gate_failures(self):
        verdict = engine(horizons=("d5",), models=(model(),)).assess(
            inputs(), as_of=NOW)
        self.assertIn(GATE_SAMPLE, verdict.qualification_failures)
        self.assertIn(GATE_BEATS_BASELINES, verdict.qualification_failures)
        self.assertIn("SAMPLE_TOO_SMALL", verdict.explanation)

    def test_every_verdict_carries_an_explanation(self):
        for case in (inputs(observation_count=0),
                     inputs(feature_count=0), inputs()):
            verdict = engine(models=(model(),)).assess(case, as_of=NOW)
            self.assertTrue(verdict.explanation.strip())

    def test_a_blank_explanation_is_refused(self):
        from src.domain.signal_availability_models import SignalAvailability
        with self.assertRaises(ValueError):
            SignalAvailability(instrument_id="x", as_of=NOW,
                               reason=NoSignalReason.NO_DATA, explanation="  ")

    def test_a_qualified_model_with_a_prediction_yields_a_decision(self):
        verdict = engine(
            horizons=("d5",),
            models=(model(qualified=True, active=True),)).assess(
            inputs(predictions=({"horizon_id": "d5", "predicted_value": 0.01,
                                 "confidence": 0.31,
                                 "predicted_at": NOW},)), as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.NO_SIGNAL_VALID)
        self.assertTrue(verdict.is_decision)

    def test_a_suppressed_candidate_reports_the_engines_own_reason(self):
        verdict = engine(
            horizons=("d5",),
            models=(model(qualified=True, active=True),)).assess(
            inputs(predictions=({"horizon_id": "d5", "predicted_value": 0.01,
                                 "confidence": 0.15, "predicted_at": NOW},),
                   suppressions=(SuppressionReason.LOW_CONFIDENCE,)),
            as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.LOW_CONFIDENCE)
        self.assertTrue(verdict.is_decision)

    def test_an_unqualified_model_outranks_its_own_stale_suppression(self):
        """
        The bug this test was written for.

        Four real instruments reported "stale data" when the deeper
        truth was that no model had ever been qualified. A stale
        prediction from an unqualified model is a symptom, and naming
        it sends someone to fix the wrong thing.
        """
        verdict = engine(horizons=("d5",), models=(model(),)).assess(
            inputs(suppressions=(SuppressionReason.STALE_PREDICTION,)),
            as_of=NOW)
        self.assertIs(verdict.reason, NoSignalReason.MODEL_NOT_QUALIFIED)


class TestHorizonInventory(unittest.TestCase):
    """Tests 9-12 -- built from evidence, protected horizon explicit."""

    def test_a_protected_horizon_must_state_why(self):
        with self.assertRaises(ValueError):
            HorizonDescriptor(horizon_id="d20", is_protected=True)

    def test_a_protected_horizon_can_never_produce_a_signal(self):
        sealed = descriptor("d20", model_count=1, qualified_model_count=1)
        self.assertTrue(sealed.has_qualified_model)
        self.assertFalse(sealed.can_produce_signal)

    def test_an_unprotected_qualified_horizon_can(self):
        open_horizon = descriptor("d5", model_count=1, qualified_model_count=1)
        self.assertTrue(open_horizon.can_produce_signal)

    def test_qualified_cannot_exceed_total(self):
        with self.assertRaises(ValueError):
            HorizonDescriptor(horizon_id="d5", model_count=1,
                              qualified_model_count=2)

    def test_the_protected_registry_names_d20(self):
        self.assertTrue(is_protected("d20"))
        self.assertTrue(is_protected("D20"))
        self.assertFalse(is_protected("d5"))

    def test_the_guard_refuses_loudly(self):
        with self.assertRaises(ProtectedHorizonAccess):
            guard_protected("d20", "expected return")
        guard_protected("d5")   # no raise

    def test_per_horizon_status_is_not_collapsed(self):
        statuses = (
            InstrumentHorizonStatus("x", "d1", HorizonStatus.AVAILABLE,
                                    explanation="ok"),
            InstrumentHorizonStatus("x", "d10",
                                    HorizonStatus.NO_QUALIFIED_MODEL,
                                    explanation="none"),
            InstrumentHorizonStatus("x", "d20", HorizonStatus.PROTECTED,
                                    explanation="sealed"))
        self.assertEqual(len({s.status for s in statuses}), 3)

    def test_a_protected_status_cannot_carry_an_expected_return(self):
        with self.assertRaises(ValueError):
            InstrumentHorizonStatus("x", "d20", HorizonStatus.PROTECTED,
                                    expected_return=0.02)


class TestHorizonIntelligence(unittest.TestCase):
    """Tests 17-20 -- availability is not preference."""

    def setUp(self):
        self.intelligence = HorizonIntelligence(HorizonPolicy())

    def _status(self, horizon, direction, confidence=0.6,
                status=HorizonStatus.AVAILABLE):
        return InstrumentHorizonStatus(
            "x", horizon, status, direction=direction, confidence=confidence,
            explanation="fixture")

    def test_agreement_is_represented(self):
        result = self.intelligence.assess("x", [
            self._status("d1", SignalDirection.LONG, 0.55),
            self._status("d5", SignalDirection.LONG, 0.68)], as_of=NOW)
        self.assertIs(result.agreement, HorizonAgreement.AGREEMENT)
        self.assertEqual(result.preferred_horizon, "d5")
        self.assertEqual(result.alternative_horizons, ("d1",))

    def test_disagreement_yields_no_preference(self):
        result = self.intelligence.assess("x", [
            self._status("d1", SignalDirection.SHORT, 0.60),
            self._status("d5", SignalDirection.LONG, 0.71)], as_of=NOW)
        self.assertIs(result.agreement, HorizonAgreement.DISAGREEMENT)
        self.assertIsNone(result.preferred_horizon)
        self.assertTrue(any("not voted on" in r for r in result.reasons))

    def test_disagreement_is_not_resolved_by_majority(self):
        result = self.intelligence.assess("x", [
            self._status("d1", SignalDirection.LONG, 0.6),
            self._status("d3", SignalDirection.LONG, 0.6),
            self._status("d5", SignalDirection.SHORT, 0.9)], as_of=NOW)
        self.assertIs(result.agreement, HorizonAgreement.DISAGREEMENT)
        self.assertIsNone(result.preferred_horizon)

    def test_no_usable_horizon_yields_no_preference(self):
        result = self.intelligence.assess("x", [
            self._status("d5", SignalDirection.LONG,
                         status=HorizonStatus.NO_QUALIFIED_MODEL)], as_of=NOW)
        self.assertIsNone(result.preferred_horizon)
        self.assertIs(result.agreement, HorizonAgreement.INSUFFICIENT_EVIDENCE)

    def test_one_usable_horizon_is_not_agreement(self):
        result = self.intelligence.assess(
            "x", [self._status("d5", SignalDirection.LONG, 0.7)], as_of=NOW)
        self.assertIs(result.agreement, HorizonAgreement.INSUFFICIENT_EVIDENCE)

    def test_a_protected_horizon_is_excluded_and_reported(self):
        result = self.intelligence.assess("x", [
            self._status("d5", SignalDirection.LONG, 0.7),
            self._status("d20", SignalDirection.LONG, 0.99,
                         status=HorizonStatus.PROTECTED)], as_of=NOW)
        self.assertEqual(result.protected_horizons, ("d20",))
        self.assertNotEqual(result.preferred_horizon, "d20")

    def test_a_protected_horizon_can_never_be_preferred(self):
        from src.domain.signal_availability_models import (
            HorizonIntelligenceResult,
        )
        with self.assertRaises(ValueError):
            HorizonIntelligenceResult(
                instrument_id="x", as_of=NOW, preferred_horizon="d20",
                protected_horizons=("d20",))

    def test_availability_does_not_force_preference(self):
        """Test 20 -- 'd5 because d5 exists' must be refusable."""
        strict = HorizonIntelligence(HorizonPolicy(
            single_horizon_may_be_preferred=False))
        result = strict.assess(
            "x", [self._status("d5", SignalDirection.LONG, 0.7)], as_of=NOW)
        self.assertIsNone(result.preferred_horizon)

    def test_an_unmeasured_confidence_does_not_win(self):
        result = self.intelligence.assess("x", [
            self._status("d1", SignalDirection.LONG, None),
            self._status("d5", SignalDirection.LONG, 0.4)], as_of=NOW)
        self.assertEqual(result.preferred_horizon, "d5")

    def test_the_horizon_request_can_go_unanswered_safely(self):
        """Test 35 -- 25.9I-F may ask and get nothing."""
        answer = horizon_answer_for_request(self.intelligence.assess(
            "x", [self._status("d5", SignalDirection.LONG,
                               status=HorizonStatus.NO_QUALIFIED_MODEL)],
            as_of=NOW))
        self.assertFalse(answer["answered"])
        self.assertIsNone(answer["preferred_horizon"])
        self.assertNotIn("extra_days", answer)

    def test_no_horizon_extension_is_ever_proposed(self):
        answer = horizon_answer_for_request(self.intelligence.assess(
            "x", [self._status("d5", SignalDirection.LONG, 0.7)], as_of=NOW))
        for forbidden in ("extra_days", "extend_by", "additional_days",
                          "new_horizon_days"):
            self.assertNotIn(forbidden, answer)


class TestTargetEstimation(unittest.TestCase):
    """Tests 28-31 -- no model, no target."""

    def setUp(self):
        self.estimator = TargetEstimator()

    def _estimate(self, **kw):
        base = dict(instrument_id="x", horizon_id="d5", expected_return=0.023,
                    model_qualified_id="m:v1", as_of=NOW,
                    model_is_qualified=True, is_calibrated=True,
                    standard_error=0.011, confidence=0.64,
                    uncertainty_basis="bootstrap se")
        base.update(kw)
        return ExpectedReturnEstimate(**base)

    def test_no_estimate_means_no_target(self):
        target = self.estimator.estimate("x", as_of=NOW, reference_price=100.0)
        self.assertIs(target.status, TargetStatus.UNAVAILABLE)
        self.assertIsNone(target.target_low)
        self.assertIsNone(target.target_high)

    def test_an_unqualified_model_cannot_set_a_target(self):
        target = self.estimator.estimate(
            "x", as_of=NOW, reference_price=100.0,
            expected_return=self._estimate(model_is_qualified=False))
        self.assertIs(target.status, TargetStatus.UNAVAILABLE)
        self.assertTrue(any("not qualified" in r for r in target.reasons))

    def test_an_uncalibrated_model_cannot_set_a_target(self):
        target = self.estimator.estimate(
            "x", as_of=NOW, reference_price=100.0,
            expected_return=self._estimate(is_calibrated=False))
        self.assertIs(target.status, TargetStatus.UNAVAILABLE)
        self.assertTrue(any("not calibrated" in r for r in target.reasons))

    def test_no_uncertainty_means_no_zone(self):
        target = self.estimator.estimate(
            "x", as_of=NOW, reference_price=100.0,
            expected_return=self._estimate(standard_error=None,
                                           interval_low=None,
                                           interval_high=None))
        self.assertIs(target.status, TargetStatus.UNAVAILABLE)
        self.assertTrue(any("decoration" in r for r in target.reasons))

    def test_a_valid_estimate_produces_a_zone_with_lineage(self):
        target = self.estimator.estimate("x", as_of=NOW, reference_price=100.0,
                                         expected_return=self._estimate())
        self.assertIs(target.status, TargetStatus.AVAILABLE)
        self.assertTrue(target.has_zone)
        self.assertEqual(target.model_qualified_id, "m:v1")
        self.assertEqual(target.horizon_id, "d5")
        self.assertIn("m:v1", target.reasons[0])

    def test_the_zone_is_deterministic(self):
        first = self.estimator.estimate("x", as_of=NOW, reference_price=100.0,
                                        expected_return=self._estimate())
        second = self.estimator.estimate("x", as_of=NOW, reference_price=100.0,
                                         expected_return=self._estimate())
        self.assertEqual((first.target_low, first.target_high),
                         (second.target_low, second.target_high))

    def test_the_zone_brackets_the_expected_move(self):
        target = self.estimator.estimate("x", as_of=NOW, reference_price=100.0,
                                         expected_return=self._estimate())
        self.assertLess(target.target_low, 102.3)
        self.assertGreater(target.target_high, 102.3)

    def test_a_target_cannot_be_smuggled_in_without_a_model(self):
        from src.domain.signal_availability_models import TargetEstimate
        with self.assertRaises(ValueError):
            TargetEstimate(instrument_id="x", as_of=NOW,
                           status=TargetStatus.UNAVAILABLE,
                           target_low=100.0, target_high=110.0)

    def test_an_available_target_must_name_its_model(self):
        from src.domain.signal_availability_models import TargetEstimate
        with self.assertRaises(ValueError):
            TargetEstimate(instrument_id="x", as_of=NOW,
                           status=TargetStatus.AVAILABLE,
                           expected_return=0.02, target_low=100.0,
                           target_high=110.0)

    def test_the_protected_horizon_cannot_supply_a_target(self):
        target = self.estimator.estimate("x", as_of=NOW, horizon_id="d20",
                                         reference_price=100.0)
        self.assertIs(target.status, TargetStatus.PROTECTED)
        self.assertFalse(target.has_zone)

    def test_an_expected_return_cannot_be_built_from_the_sealed_horizon(self):
        with self.assertRaises(ValueError):
            self._estimate(horizon_id="d20")

    def test_the_target_request_can_go_unanswered_safely(self):
        """Test 34 -- 25.9I-F may ask and get nothing."""
        answer = target_answer_for_request(
            self.estimator.estimate("x", as_of=NOW, reference_price=100.0))
        self.assertFalse(answer["answered"])
        self.assertIsNone(answer["target_zone"])
        self.assertTrue(answer["reasons"])

    def test_event_materiality_alone_cannot_produce_a_target(self):
        """Test 31 -- no event field exists on this path at all."""
        import inspect
        signature = inspect.signature(TargetEstimator.estimate)
        for forbidden in ("event", "materiality", "direction", "urgency"):
            self.assertNotIn(forbidden, signature.parameters)


class TestNoFabrication(unittest.TestCase):
    """Tests 21-24 -- nothing is invented to fill a gap."""

    def test_expected_return_is_none_without_a_model(self):
        verdict = engine(horizons=("d5",), models=()).assess(
            inputs(), as_of=NOW)
        self.assertIsNone(verdict.expected_return)

    def test_confidence_is_none_without_a_prediction(self):
        verdict = engine(horizons=("d5",), models=(model(),)).assess(
            inputs(), as_of=NOW)
        self.assertIsNone(verdict.confidence)

    def test_preferred_horizon_is_none_without_evidence(self):
        verdict = engine(horizons=("d5",), models=(model(),)).assess(
            inputs(), as_of=NOW)
        self.assertIsNone(verdict.preferred_horizon)

    def test_the_attention_score_is_never_called_alpha(self):
        """Test 24 -- 25.9I-B's desirability must not become expected return."""
        import ast
        for name in ("signal_availability.py", "horizon_intelligence.py",
                     "target_estimation.py", "model_inventory.py",
                     "horizon_inventory.py"):
            path = os.path.join(os.path.dirname(__file__), "..", "..",
                                "src", "adaptive", name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    for forbidden in ("src.adaptive.scoring",
                                      "src.adaptive.desirability"):
                        self.assertFalse(
                            module.startswith(forbidden),
                            f"{name} imports {module}; an attention score is "
                            "not an expected return")

    def test_unknown_cost_does_not_become_zero(self):
        """Test 37 -- there is no cost field that silently defaults to 0."""
        from src.domain.signal_availability_models import SignalAvailability
        fields = set(SignalAvailability.__dataclass_fields__)
        for forbidden in ("cost", "net_edge", "spread", "commission",
                          "slippage"):
            self.assertNotIn(forbidden, fields)


class TestCoverage(unittest.TestCase):
    """Tests 38-39 -- the numbers must add up."""

    def _verdicts(self):
        cases = [
            inputs(instrument_id="a", observation_count=0, feature_count=0),
            inputs(instrument_id="b", feature_count=0),
            inputs(instrument_id="c"),
            inputs(instrument_id="d"),
        ]
        return [engine(horizons=("d5",), models=(model(),)).assess(c, as_of=NOW)
                for c in cases]

    def test_coverage_reconciles_to_the_universe(self):
        report = build_coverage(self._verdicts(), as_of=NOW)
        self.assertTrue(report.reconciles)
        self.assertEqual(report.total_by_reason, report.universe_size)
        self.assertEqual(report.total_by_class, report.universe_size)

    def test_every_instrument_contributes_exactly_one_reason(self):
        verdicts = self._verdicts()
        report = build_coverage(verdicts, as_of=NOW)
        self.assertEqual(sum(report.by_reason.values()), len(verdicts))

    def test_capability_gaps_are_not_counted_as_decisions(self):
        report = build_coverage(self._verdicts(), as_of=NOW)
        self.assertEqual(report.decisions, 0)

    def test_the_funnel_is_derived_not_declared(self):
        report = build_coverage(self._verdicts(), as_of=NOW)
        self.assertEqual(report.funnel[0].remaining, 4)
        self.assertEqual(report.funnel[0].name, "universe")
        self.assertTrue(any(s.excluded for s in report.funnel))

    def test_an_empty_universe_still_reconciles(self):
        report = build_coverage([], as_of=NOW)
        self.assertTrue(report.reconciles)
        self.assertEqual(report.universe_size, 0)


class TestDeterminism(unittest.TestCase):
    """Tests 41-42 -- same inputs, same status."""

    def test_the_same_inputs_produce_the_same_verdict(self):
        first = engine(models=(model(),)).assess(inputs(), as_of=NOW)
        second = engine(models=(model(),)).assess(inputs(), as_of=NOW)
        self.assertEqual(first.describe(), second.describe())

    def test_candidate_order_does_not_change_the_outcome(self):
        forward = engine(horizons=("d1", "d5", "d20"),
                         models=(model(),)).assess(inputs(), as_of=NOW)
        backward = engine(horizons=("d20", "d5", "d1"),
                          models=(model(),)).assess(inputs(), as_of=NOW)
        self.assertIs(forward.reason, backward.reason)
        self.assertEqual(forward.preferred_horizon, backward.preferred_horizon)


if __name__ == "__main__":
    unittest.main()
