"""
tests/attribution/test_detectors.py
-------------------------------------------
The rules, one at a time. §63 items 1-15 and 18-19.

WHY SO MANY TESTS ABOUT REFUSING TO ANSWER
----------------------------------------------
A diagnostic layer fails in a characteristic way: it explains
everything. Given a bad result and a rule that can fire, it fires, and
the output looks like insight while being a restatement of the loss.

So roughly half of what follows checks that a detector declined:
that a missing input produced INSUFFICIENT_EVIDENCE rather than a
verdict, that a loss on its own produced nothing, and that each
detector reads only the evidence it is entitled to read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.attribution.detectors import (
    HORIZON_RESCUE_RETURN, MAGNITUDE_OVERSHOOT_RATIO,
    MAGNITUDE_SHORTFALL_RATIO, TIMING_CAPTURE_RATIO, TIMING_MFE_FLOOR,
    detect_data_error, detect_execution_error, detect_horizon_mismatch,
    detect_magnitude_error, detect_portfolio_error, detect_prediction_error,
    detect_regime_error, detect_risk_error, detect_signal_error,
    detect_sizing_error, detect_timing_error,
)
from src.domain.attribution_models import (
    AttributionConfidence, ErrorType, Severity,
)


def outcome(**overrides):
    base = {
        "subject_kind": "signal", "subject_id": "sig-1", "horizon": "5d",
        "status": "available", "direction_result": "hit",
        "expected_direction": "long", "expected_return": 0.02,
        "simple_return": 0.025, "realized_direction": "long",
        "mfe": 0.03, "mae": -0.01, "time_to_mfe_seconds": 86400.0,
        "reference_price": 100.0, "market_regime": None,
        "horizon_sort": 5 * 6.5 * 3600.0,
    }
    base.update(overrides)
    return base


class TestPredictionError(unittest.TestCase):
    """§63.1, §63.2 — direction."""

    def test_a_miss_fires_with_high_confidence(self):
        result = detect_prediction_error(outcome(direction_result="miss",
                                                 simple_return=-0.03))
        self.assertTrue(result.fired)
        self.assertEqual(result.confidence, AttributionConfidence.HIGH)

    def test_a_hit_does_not_fire_but_is_still_judged(self):
        """
        Judged-and-clean is a finding. Reporting it as unjudgeable would
        make a working layer look unmeasured.
        """
        result = detect_prediction_error(outcome(direction_result="hit"))
        self.assertFalse(result.fired)
        self.assertTrue(result.judgeable)

    def test_a_neutral_result_is_not_a_prediction_error(self):
        """
        §7: a move too small to mean anything is not evidence the
        prediction was wrong.
        """
        result = detect_prediction_error(outcome(direction_result="neutral",
                                                 simple_return=0.0001))
        self.assertFalse(result.fired)

    def test_an_unmeasured_outcome_cannot_be_judged(self):
        result = detect_prediction_error(outcome(status="pending"))
        self.assertFalse(result.judgeable)
        self.assertEqual(result.confidence,
                         AttributionConfidence.INSUFFICIENT_EVIDENCE)

    def test_it_names_the_missing_input(self):
        result = detect_prediction_error(outcome(status="insufficient_data"))
        self.assertIn("status", result.missing)

    def test_it_cites_the_numbers(self):
        result = detect_prediction_error(outcome(direction_result="miss",
                                                 simple_return=-0.04))
        kinds = {item.kind for item in result.evidence}
        self.assertIn("direction", kinds)
        self.assertIn("neutral_band", kinds)

    def test_severity_scales_with_the_move_not_with_certainty(self):
        small = detect_prediction_error(outcome(direction_result="miss",
                                                simple_return=-0.005))
        large = detect_prediction_error(outcome(direction_result="miss",
                                                simple_return=-0.15))
        self.assertEqual(large.severity, Severity.CRITICAL)
        self.assertEqual(small.severity, Severity.LOW)
        self.assertEqual(small.confidence, large.confidence)


class TestMagnitudeError(unittest.TestCase):
    """§63.3, §8 — right sign, wrong size."""

    def test_a_large_shortfall_fires(self):
        result = detect_magnitude_error(outcome(expected_return=0.05,
                                                simple_return=0.002))
        self.assertTrue(result.fired)
        self.assertIn("shortfall", result.summary)

    def test_a_large_overshoot_also_fires(self):
        """Only counting shortfalls would bias the calibration profile."""
        result = detect_magnitude_error(outcome(expected_return=0.01,
                                                simple_return=0.09))
        self.assertTrue(result.fired)
        self.assertIn("overshoot", result.summary)

    def test_a_reasonable_magnitude_does_not_fire(self):
        result = detect_magnitude_error(outcome(expected_return=0.02,
                                                simple_return=0.024))
        self.assertFalse(result.fired)

    def test_a_wrong_direction_is_not_also_a_magnitude_error(self):
        """
        §8: the two must stay distinguishable. If every wrong call were
        also a magnitude error, neither label would mean anything.
        """
        result = detect_magnitude_error(outcome(direction_result="miss",
                                                simple_return=-0.03))
        self.assertFalse(result.fired)

    def test_a_tiny_expected_move_offers_no_magnitude_claim(self):
        result = detect_magnitude_error(outcome(expected_return=0.0001,
                                                simple_return=0.02))
        self.assertFalse(result.judgeable)

    def test_a_missing_expectation_cannot_be_judged(self):
        result = detect_magnitude_error(outcome(expected_return=None))
        self.assertFalse(result.judgeable)
        self.assertIn("expected_return", result.missing)


class TestHorizonMismatch(unittest.TestCase):
    """§63.4, §9 — right eventually, wrong when asked."""

    def sibling(self, horizon, value, result="hit", sort=None):
        return outcome(horizon=horizon, simple_return=value,
                       direction_result=result,
                       horizon_sort=sort if sort is not None else 10 * 6.5 * 3600.0)

    def test_wrong_at_the_horizon_and_right_later_fires(self):
        result = detect_horizon_mismatch(
            outcome(direction_result="miss", simple_return=-0.01),
            [self.sibling("10d", 0.04)])
        self.assertTrue(result.fired)
        self.assertIn("10d", result.summary)

    def test_a_trivial_later_move_does_not_rescue_it(self):
        """
        Without a floor a two-basis-point drift would rescue every miss
        and the finding would mean nothing.
        """
        result = detect_horizon_mismatch(
            outcome(direction_result="miss", simple_return=-0.01),
            [self.sibling("10d", HORIZON_RESCUE_RETURN / 4)])
        self.assertFalse(result.fired)

    def test_an_earlier_horizon_does_not_count_as_later(self):
        result = detect_horizon_mismatch(
            outcome(direction_result="miss", simple_return=-0.01,
                    horizon_sort=10 * 6.5 * 3600.0),
            [self.sibling("1d", 0.04, sort=6.5 * 3600.0)])
        self.assertFalse(result.fired)

    def test_a_hit_is_never_a_horizon_mismatch(self):
        result = detect_horizon_mismatch(outcome(direction_result="hit"),
                                         [self.sibling("10d", 0.04)])
        self.assertFalse(result.fired)

    def test_with_no_siblings_it_cannot_be_judged(self):
        result = detect_horizon_mismatch(
            outcome(direction_result="miss", simple_return=-0.01), [])
        self.assertFalse(result.judgeable)


class TestTimingError(unittest.TestCase):
    """§63.5, §10 — the move happened and was not kept."""

    def test_a_large_unrealised_excursion_fires(self):
        result = detect_timing_error(outcome(mfe=0.08, simple_return=0.005))
        self.assertTrue(result.fired)

    def test_a_small_excursion_is_not_a_missed_opportunity(self):
        result = detect_timing_error(outcome(mfe=TIMING_MFE_FLOOR / 2,
                                             simple_return=0.0))
        self.assertFalse(result.fired)

    def test_keeping_most_of_the_move_is_not_a_timing_error(self):
        result = detect_timing_error(outcome(mfe=0.05, simple_return=0.045))
        self.assertFalse(result.fired)

    def test_a_short_is_measured_in_its_own_direction(self):
        """
        Without signing the capture by direction, the ratio is
        meaningless for half the book.
        """
        result = detect_timing_error(outcome(
            expected_direction="short", mfe=0.08, simple_return=-0.07))
        self.assertFalse(result.fired, "a profitable short read as bad timing")

    def test_an_excursion_at_entry_is_reported_distinctly(self):
        result = detect_timing_error(outcome(mfe=0.08, simple_return=0.005,
                                             time_to_mfe_seconds=0.0))
        self.assertTrue(any("already over" in item.statement
                            for item in result.evidence))

    def test_it_never_claims_more_than_medium_confidence(self):
        """
        There is no order and no exit rule in this database, so "not
        captured" is inferred from the price path rather than observed
        from a fill.
        """
        result = detect_timing_error(outcome(mfe=0.08, simple_return=0.0))
        self.assertEqual(result.confidence, AttributionConfidence.MEDIUM)

    def test_it_carries_that_caveat_as_evidence(self):
        result = detect_timing_error(outcome(mfe=0.08, simple_return=0.0))
        self.assertTrue(any(item.kind == "caveat" for item in result.evidence))

    def test_a_missing_excursion_cannot_be_judged(self):
        result = detect_timing_error(outcome(mfe=None))
        self.assertFalse(result.judgeable)


class TestSignalError(unittest.TestCase):
    """§63.6, §11 — what the signal layer did to the prediction."""

    def signal(self, **overrides):
        base = {"signal_id": "sig-1", "status": "suppressed",
                "suppression_note": "information is 29 days old"}
        base.update(overrides)
        return base

    def test_a_suppressed_signal_that_was_right_fires(self):
        result = detect_signal_error(
            outcome(direction_result="hit", simple_return=0.05), self.signal())
        self.assertTrue(result.fired)

    def test_a_suppressed_signal_that_was_wrong_does_not(self):
        """Suppressing a bad call is the rule working, not failing."""
        result = detect_signal_error(
            outcome(direction_result="miss", simple_return=-0.05), self.signal())
        self.assertFalse(result.fired)

    def test_an_active_signal_withheld_nothing(self):
        result = detect_signal_error(outcome(),
                                     self.signal(status="active"))
        self.assertFalse(result.fired)

    def test_a_trivially_right_suppression_does_not_fire(self):
        result = detect_signal_error(
            outcome(direction_result="hit", simple_return=0.005), self.signal())
        self.assertFalse(result.fired)

    def test_it_records_the_suppression_reason_as_evidence(self):
        result = detect_signal_error(
            outcome(direction_result="hit", simple_return=0.05), self.signal())
        self.assertTrue(any("29 days old" in item.statement
                            for item in result.evidence))

    def test_it_states_that_hindsight_is_not_a_verdict_on_the_rule(self):
        result = detect_signal_error(
            outcome(direction_result="hit", simple_return=0.05), self.signal())
        self.assertTrue(any(item.kind == "caveat" for item in result.evidence))

    def test_no_signal_record_cannot_be_judged(self):
        result = detect_signal_error(outcome(), None)
        self.assertFalse(result.judgeable)
        self.assertEqual(result.missing, "signals")


class TestDataError(unittest.TestCase):
    """§63.11, §16, §38 — the state of the data at decision time."""

    def test_invalid_decision_time_data_fires(self):
        result = detect_data_error(outcome(), {"quality_level": "invalid"})
        self.assertTrue(result.fired)
        self.assertEqual(result.severity, Severity.HIGH)

    def test_high_quality_data_does_not_fire(self):
        result = detect_data_error(outcome(), {"quality_level": "high"})
        self.assertFalse(result.fired)

    def test_a_bad_outcome_alone_never_produces_a_data_error(self):
        """
        §16 exactly: do not infer data error merely because the outcome
        was bad. The detector never reads the return.
        """
        result = detect_data_error(
            outcome(direction_result="miss", simple_return=-0.30),
            {"quality_level": "high"})
        self.assertFalse(result.fired)

    def test_it_says_it_does_not_read_the_return(self):
        result = detect_data_error(outcome(), {"quality_level": "invalid"})
        self.assertTrue(any("never reads the realised return" in item.statement
                            for item in result.evidence))

    def test_no_observation_cannot_be_judged(self):
        result = detect_data_error(outcome(), None)
        self.assertFalse(result.judgeable)


class TestTheLayersWithNoEvidenceHere(unittest.TestCase):
    """
    §63.7-9, §63.12 and §15. Six layers have no evidence source in this
    database. Each must decline by name rather than report a clean bill
    of health.
    """

    def test_sizing_declines_and_names_the_missing_tables(self):
        result = detect_sizing_error(outcome(), None)
        self.assertFalse(result.judgeable)
        self.assertIn("positions", result.missing)

    def test_risk_declines_and_names_the_missing_table(self):
        result = detect_risk_error(outcome(), None)
        self.assertFalse(result.judgeable)
        self.assertEqual(result.missing, "risk_decisions")

    def test_execution_declines_and_names_the_missing_table(self):
        result = detect_execution_error(outcome(), None)
        self.assertFalse(result.judgeable)
        self.assertIn("execution_fills", result.missing)

    def test_portfolio_declines_and_names_the_missing_tables(self):
        result = detect_portfolio_error(outcome(), None)
        self.assertFalse(result.judgeable)
        self.assertIn("portfolios", result.missing)

    def test_regime_declines_when_no_regime_is_recorded(self):
        result = detect_regime_error(outcome(market_regime=None))
        self.assertFalse(result.judgeable)
        self.assertIn("market_regime", result.missing)

    def test_none_of_them_ever_reports_no_error(self):
        """
        The flattering lie this phase is written against: silence read
        as health.
        """
        for result in (detect_sizing_error(outcome(), None),
                       detect_risk_error(outcome(), None),
                       detect_execution_error(outcome(), None),
                       detect_portfolio_error(outcome(), None),
                       detect_regime_error(outcome())):
            self.assertFalse(result.fired)
            self.assertFalse(result.judgeable)


class TestTheLayersWorkOnceTheirInputsExist(unittest.TestCase):
    """
    The same six detectors, given the inputs they are waiting for. They
    are implemented, not stubbed, and this proves it.
    """

    def test_risk_distinguishes_an_expected_block_from_a_violation(self):
        """
        §13's central distinction. A block followed by a favourable move
        is risk working; grading it on hindsight would train it to
        decline less.
        """
        blocked = detect_risk_error(
            outcome(), {"is_approved": False, "violated_limits": []})
        self.assertFalse(blocked.fired)
        self.assertIn("expected risk block", blocked.summary)

        violation = detect_risk_error(
            outcome(), {"is_approved": True, "violated_limits": ["max_position"]})
        self.assertTrue(violation.fired)
        self.assertEqual(violation.severity, Severity.CRITICAL)

    def test_execution_measures_slippage_against_the_decision_price(self):
        result = detect_execution_error(
            outcome(expected_direction="long"),
            {"decision_price": 100.0, "fill_price": 103.0})
        self.assertTrue(result.fired)
        self.assertIn("slippage", result.summary)

    def test_execution_is_not_blamed_for_a_wrong_direction(self):
        """
        §14 exactly. A clean fill on a losing call is not an execution
        error.
        """
        result = detect_execution_error(
            outcome(direction_result="miss", simple_return=-0.20),
            {"decision_price": 100.0, "fill_price": 100.0})
        self.assertFalse(result.fired)

    def test_sizing_fires_when_the_position_exceeds_its_budget(self):
        result = detect_sizing_error(
            outcome(), {"quantity": 30.0, "risk_budget": 1000.0})
        self.assertTrue(result.fired)

    def test_portfolio_fires_on_concentration_above_its_limit(self):
        result = detect_portfolio_error(
            outcome(), {"max_concentration": 0.6, "concentration_limit": 0.25})
        self.assertTrue(result.fired)

    def test_regime_needs_a_population_not_an_anecdote(self):
        """§15: do not label every loss in a regime a regime error."""
        thin = detect_regime_error(
            outcome(market_regime="high_vol", direction_result="miss"),
            {"sample_size": 4, "directional_accuracy": 0.2})
        self.assertFalse(thin.judgeable)

        real = detect_regime_error(
            outcome(market_regime="high_vol", direction_result="miss"),
            {"sample_size": 120, "directional_accuracy": 0.31})
        self.assertTrue(real.fired)


class TestDeterminism(unittest.TestCase):
    """§65 — same inputs, same answer, every time."""

    def test_every_detector_is_stable_across_repeated_calls(self):
        sample = outcome(direction_result="miss", simple_return=-0.04,
                         mfe=0.08)
        for _ in range(5):
            self.assertEqual(
                detect_prediction_error(sample).summary,
                detect_prediction_error(sample).summary)
            self.assertEqual(
                detect_timing_error(sample).summary,
                detect_timing_error(sample).summary)

    def test_no_detector_uses_a_clock_or_randomness(self):
        import inspect
        import src.attribution.detectors as module
        source = inspect.getsource(module)
        for forbidden in ("random.", "datetime.now", "time.time", "uuid"):
            self.assertNotIn(forbidden, source,
                             f"detectors use {forbidden}; output would not be "
                             f"reproducible")


if __name__ == "__main__":
    unittest.main()
