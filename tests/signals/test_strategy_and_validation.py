"""
tests/signals/test_strategy_and_validation.py
-----------------------------------------------------------
Tests for the Phase 10 strategy framework, scoring, and validator.

The properties defended here are the ones that keep the layer honest:
that strength and confidence stay independent, that conflict lowers
confidence instead of being averaged away, that no candidate is ever
silently dropped, and that unknown inputs fail closed rather than open.
"""

import os
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.signal_models import (
    AgreementState, ModelContribution, SignalContext, SignalDirection,
    SignalStatus, SignalStrategyDefinition, SignalType, SuppressionReason,
)
from src.signals.strategy import (
    DEFAULT_STRENGTH_SCALE, GenerationContext, MLDirectionalStrategy,
    classify_agreement, compute_confidence, direction_from_value,
    normalize_strength,
)
from src.signals.validator import SignalValidator, ValidationConfig

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakePrediction:
    prediction_id: str
    trained_model_id: str
    model_qualified_id: str
    predicted_value: Optional[float]
    confidence: Optional[float] = None
    class_probabilities: Optional[Dict] = None
    is_abstention: bool = False
    abstention_reason: Optional[str] = None


def make_strategy(**parameters):
    params = {"strength_scale": 0.05, "horizon_days": 5}
    params.update(parameters)
    return MLDirectionalStrategy(SignalStrategyDefinition(
        strategy_id="ml_dir", name="ML Directional", version="v1",
        signal_type=SignalType.DIRECTIONAL, parameters=params, created_at=NOW))


def make_context(predictions, quality="high", **overrides):
    defaults = dict(
        instrument_id="inst-nvda", information_cutoff=NOW,
        predictions=predictions, context=SignalContext(data_quality_level=quality))
    defaults.update(overrides)
    return GenerationContext(**defaults)


class TestNormalizeStrength(unittest.TestCase):
    def test_half_scale_gives_half_strength(self):
        self.assertAlmostEqual(normalize_strength(0.025, 0.05), 0.5)

    def test_strength_saturates_at_one(self):
        self.assertEqual(normalize_strength(0.5, 0.05), 1.0)

    def test_strength_uses_magnitude_not_sign(self):
        self.assertEqual(normalize_strength(-0.025, 0.05), normalize_strength(0.025, 0.05))

    def test_missing_value_gives_none_not_zero(self):
        # Zero strength is a claim; no strength is not.
        self.assertIsNone(normalize_strength(None))


class TestComputeConfidence(unittest.TestCase):
    def test_unknown_model_confidence_defaults_to_half_not_one(self):
        known = compute_confidence(1.0, "high", AgreementState.AGREEMENT)
        unknown = compute_confidence(None, "high", AgreementState.AGREEMENT)
        self.assertLess(unknown, known)
        self.assertAlmostEqual(unknown, 0.5)

    def test_invalid_data_quality_collapses_confidence_to_zero(self):
        # Multiplicative combination means a fatal factor dominates.
        self.assertEqual(
            compute_confidence(1.0, "invalid", AgreementState.AGREEMENT), 0.0)

    def test_conflict_reduces_confidence_sharply(self):
        agree = compute_confidence(0.8, "high", AgreementState.AGREEMENT)
        conflict = compute_confidence(0.8, "high", AgreementState.CONFLICT)
        self.assertLess(conflict, agree / 2)

    def test_small_sample_halves_confidence(self):
        full = compute_confidence(0.8, "high", AgreementState.AGREEMENT, False)
        small = compute_confidence(0.8, "high", AgreementState.AGREEMENT, True)
        self.assertAlmostEqual(small, full * 0.5)

    def test_confidence_stays_within_unit_range(self):
        self.assertLessEqual(compute_confidence(1.0, "high", AgreementState.AGREEMENT), 1.0)
        self.assertGreaterEqual(compute_confidence(0.0, "low", AgreementState.CONFLICT), 0.0)

    def test_confidence_is_not_the_model_probability(self):
        # A model 100% confident on poor data must not yield confidence 1.
        self.assertLess(compute_confidence(1.0, "low", AgreementState.AGREEMENT), 1.0)


class TestClassifyAgreement(unittest.TestCase):
    def _contribution(self, value, abstain=False):
        return ModelContribution(prediction_id="p", trained_model_id="t",
                                 model_qualified_id="m", predicted_value=value,
                                 is_abstention=abstain)

    def test_single_model_is_insufficient_evidence_not_agreement(self):
        state = classify_agreement([self._contribution(0.02)])
        self.assertEqual(state, AgreementState.INSUFFICIENT_EVIDENCE)

    def test_same_sign_models_agree(self):
        state = classify_agreement([self._contribution(0.02), self._contribution(0.03)])
        self.assertEqual(state, AgreementState.AGREEMENT)

    def test_even_split_is_conflict(self):
        state = classify_agreement([self._contribution(0.02), self._contribution(-0.02)])
        self.assertEqual(state, AgreementState.CONFLICT)

    def test_lone_dissenter_among_many_is_partial_agreement(self):
        contributions = [self._contribution(0.02) for _ in range(4)]
        contributions.append(self._contribution(-0.01))
        self.assertEqual(classify_agreement(contributions),
                         AgreementState.PARTIAL_AGREEMENT)

    def test_abstentions_do_not_count_as_votes(self):
        state = classify_agreement([self._contribution(0.02),
                                    self._contribution(None, abstain=True)])
        self.assertEqual(state, AgreementState.INSUFFICIENT_EVIDENCE)


class TestDirectionFromValue(unittest.TestCase):
    def test_missing_prediction_is_no_signal_not_neutral(self):
        self.assertEqual(direction_from_value(None), SignalDirection.NO_SIGNAL)

    def test_inside_neutral_band_is_neutral(self):
        self.assertEqual(direction_from_value(0.001, neutral_band=0.005),
                         SignalDirection.NEUTRAL)

    def test_positive_is_long_and_negative_is_short(self):
        self.assertEqual(direction_from_value(0.02), SignalDirection.LONG)
        self.assertEqual(direction_from_value(-0.02), SignalDirection.SHORT)


class TestMLDirectionalStrategy(unittest.TestCase):
    def test_no_predictions_yields_no_candidates(self):
        self.assertEqual(make_strategy().generate(make_context([])), [])

    def test_agreeing_models_produce_a_long_candidate(self):
        context = make_context([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", 0.02, 0.6)])
        candidate = make_strategy().generate(context)[0]
        self.assertEqual(candidate.direction, SignalDirection.LONG)
        self.assertEqual(candidate.metadata["agreement_state"], "agreement")

    def test_candidate_id_is_deterministic_across_runs(self):
        context = make_context([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7)])
        first = make_strategy().generate(context)[0].candidate_id
        second = make_strategy().generate(context)[0].candidate_id
        self.assertEqual(first, second)

    def test_abstaining_model_is_recorded_not_dropped(self):
        context = make_context([
            FakePrediction("pr-1", "tm-1", "m1:v1", None, None,
                           is_abstention=True, abstention_reason="no value")])
        candidate = make_strategy().generate(context)[0]
        self.assertEqual(len(candidate.contributions), 1)
        self.assertTrue(candidate.contributions[0].is_abstention)
        self.assertEqual(candidate.direction, SignalDirection.NO_SIGNAL)

    def test_explanation_lists_each_contributing_model(self):
        context = make_context([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", 0.02, 0.6)])
        candidate = make_strategy().generate(context)[0]
        self.assertEqual(len(candidate.explanation.factors), 2)

    def test_conflict_is_recorded_as_a_caveat(self):
        context = make_context([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", -0.03, 0.7)])
        candidate = make_strategy().generate(context)[0]
        self.assertTrue(any("agreement" in c for c in candidate.explanation.caveats))

    def test_provenance_carries_the_information_cutoff(self):
        context = make_context([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7)])
        candidate = make_strategy().generate(context)[0]
        self.assertEqual(candidate.provenance.source_information_cutoff, NOW)
        self.assertEqual(candidate.provenance.strategy_version, "v1")


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.validator = SignalValidator()
        self.strategy = make_strategy()

    def _candidate(self, predictions, quality="high", **kwargs):
        return self.strategy.generate(make_context(predictions, quality, **kwargs))[0]

    def test_a_clean_candidate_becomes_an_active_signal(self):
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", 0.02, 0.6)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertEqual(signal.status, SignalStatus.ACTIVE)
        self.assertTrue(signal.is_actionable)

    def test_a_failing_candidate_still_produces_a_signal(self):
        # Spec §23: never silently discard.
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.0001, 0.05)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.status, SignalStatus.SUPPRESSED)
        self.assertTrue(signal.suppression_reasons)

    def test_conflict_suppresses_rather_than_picking_a_side(self):
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", -0.03, 0.7)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIn(SuppressionReason.MODEL_CONFLICT, signal.suppression_reasons)
        self.assertFalse(signal.is_actionable)

    def test_unknown_data_quality_fails_closed(self):
        candidate = self._candidate(
            [FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7)], quality=None)
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIn(SuppressionReason.POOR_DATA_QUALITY, signal.suppression_reasons)

    def test_stale_information_is_suppressed(self):
        candidate = self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.9)])
        late = NOW + timedelta(days=30)
        signal = self.validator.validate(candidate, now=late)
        self.assertIn(SuppressionReason.STALE_PREDICTION, signal.suppression_reasons)

    def test_weak_strength_is_suppressed(self):
        candidate = self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.001, 0.9)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIn(SuppressionReason.BELOW_STRENGTH_THRESHOLD,
                      signal.suppression_reasons)

    def test_abstention_is_suppressed_with_its_own_reason(self):
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", None, None, is_abstention=True)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIn(SuppressionReason.MODEL_ABSTAINED, signal.suppression_reasons)

    def test_unsupported_instrument_is_suppressed(self):
        validator = SignalValidator(ValidationConfig(
            supported_instruments={"inst-msft"}))
        candidate = self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.9)])
        signal = validator.validate(candidate, now=NOW)
        self.assertIn(SuppressionReason.UNSUPPORTED_INSTRUMENT,
                      signal.suppression_reasons)

    def test_multiple_failures_all_recorded_not_just_the_first(self):
        candidate = self._candidate(
            [FakePrediction("pr-1", "tm-1", "m1:v1", 0.0001, 0.01)], quality=None)
        signal = self.validator.validate(candidate, now=NOW)
        self.assertGreaterEqual(len(signal.suppression_reasons), 2)

    def test_suppression_note_explains_each_failure(self):
        candidate = self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.001, 0.9)])
        signal = self.validator.validate(candidate, now=NOW)
        self.assertIn("below floor", signal.suppression_note)

    def test_signal_records_the_validation_config_version(self):
        validator = SignalValidator(ValidationConfig(version="v7"))
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", 0.02, 0.6)])
        signal = validator.validate(candidate, now=NOW)
        self.assertEqual(signal.provenance.configuration_version, "v7")

    def test_signal_id_is_deterministic(self):
        candidate = self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7)])
        first = self.validator.validate(candidate, now=NOW).signal_id
        second = self.validator.validate(candidate, now=NOW).signal_id
        self.assertEqual(first, second)

    def test_validity_window_starts_at_the_information_cutoff(self):
        candidate = self._candidate([
            FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7),
            FakePrediction("pr-2", "tm-2", "m2:v1", 0.02, 0.6)])
        signal = self.validator.validate(candidate, now=NOW + timedelta(hours=2))
        self.assertEqual(signal.valid_from, NOW)

    def test_validate_all_returns_one_signal_per_candidate(self):
        candidates = [
            self._candidate([FakePrediction("pr-1", "tm-1", "m1:v1", 0.03, 0.7)]),
            self._candidate([FakePrediction("pr-2", "tm-2", "m2:v1", 0.0001, 0.01)]),
        ]
        signals = self.validator.validate_all(candidates, now=NOW)
        self.assertEqual(len(signals), 2)


class TestNoRiskChecksLeakedIn(unittest.TestCase):
    """Spec §21: portfolio risk belongs to Phase 11, not here."""

    def test_validation_config_has_no_portfolio_fields(self):
        fields = set(ValidationConfig.__dataclass_fields__.keys())
        for forbidden in ("max_position_size", "max_exposure", "capital",
                          "max_drawdown", "position_limit"):
            self.assertNotIn(forbidden, fields)


if __name__ == "__main__":
    unittest.main()
