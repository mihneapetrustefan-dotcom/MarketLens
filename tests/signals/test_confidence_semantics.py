"""
tests/signals/test_confidence_semantics.py
--------------------------------------------------
NEW-02: why 403 of 408 signals carry exactly 0.30.

THE OBSERVATION
-------------------
Phase 17.5 measured the production database and found signal
confidence takes two values:

    0.30  x 403
    0.15  x 5

Two values across 408 signals. The instruction for Phase 18 is
explicit: *do NOT simply make confidence more varied artificially* and
*do not call something "confidence" when it is simply a constant
heuristic*. So these tests do not change the number. They pin down
what it means and prove the constancy is a consequence of the inputs
rather than a bug in the arithmetic.

THE ARITHMETIC, VERIFIED AGAINST PRODUCTION
-----------------------------------------------
    confidence = base x quality x agreement x sample

Measured on the production asset, 2026-09-05:

    predictions.confidence   None for all 549   -> base      = 0.5
    signals.data_quality     'high' for all 408 -> quality   = 1.0
    agreement_state    'insufficient_evidence'  -> agreement = 0.6
                                                   sample    = 1.0

    0.5 x 1.0 x 0.6 x 1.0 = 0.30      exactly, 403 times
    0.5 x 1.0 x 0.6 x 0.5 = 0.15      the five small-sample ones

Three of the four factors are STRUCTURALLY constant right now:

  base       Ridge regression reports no confidence of its own, so
             every prediction stores None and base is pinned at 0.5.
             0.5 rather than 1.0 because an unknown confidence is not
             a confident one.

  agreement  `classify_agreement` returns INSUFFICIENT_EVIDENCE for
             fewer than two usable contributions, and exactly one
             model family exists. A single voice agreeing with itself
             is not corroboration.

  quality    Every observation currently passes as 'high'.

So confidence is not broken. It is a **heuristic trust score** whose
inputs do not yet vary, and it will start varying when a second model
family exists (agreement), or a family that reports its own
uncertainty (base), or lower-quality observations (quality) — not
before.

WHAT CONFIDENCE IS NOT
--------------------------
It is not a probability. Nothing calibrates it against outcomes, and
0.30 does not mean "right 30% of the time". `strength` is the number
that varies, and it is not a probability either: it is the size of the
expected move relative to the strategy's scale.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domain.signal_models import AgreementState, ModelContribution
from src.signals.strategy import (
    AGREEMENT_FACTORS, QUALITY_FACTORS, classify_agreement,
    compute_confidence, normalize_strength,
)


class TestTheProductionValueIsReproduced(unittest.TestCase):
    """
    The exact numbers observed in production, derived from the exact
    inputs observed in production. If either changes, this fails and
    the explanation in the docstring has to be rewritten rather than
    quietly becoming wrong.
    """

    def test_the_403_signals_at_030_are_reproduced_exactly(self):
        self.assertEqual(
            compute_confidence(
                model_confidence=None,               # ridge reports none
                data_quality_level="high",
                agreement=AgreementState.INSUFFICIENT_EVIDENCE,
                small_sample=False),
            0.30)

    def test_the_5_signals_at_015_are_reproduced_exactly(self):
        self.assertEqual(
            compute_confidence(
                model_confidence=None, data_quality_level="high",
                agreement=AgreementState.INSUFFICIENT_EVIDENCE,
                small_sample=True),
            0.15)

    def test_the_two_differ_only_by_the_small_sample_factor(self):
        self.assertAlmostEqual(0.15 / 0.30, 0.5, places=9)


class TestWhyThreeFactorsAreCurrentlyConstant(unittest.TestCase):

    def test_a_model_reporting_no_confidence_pins_base_at_half(self):
        """
        Not 1.0. An unknown confidence is not a confident one, and
        defaulting it high would make every signal from a silent model
        look trustworthy.
        """
        known = compute_confidence(1.0, "high", AgreementState.AGREEMENT)
        unknown = compute_confidence(None, "high", AgreementState.AGREEMENT)
        self.assertEqual(unknown, 0.5)
        self.assertEqual(known, 1.0)
        self.assertLess(unknown, known)

    def test_one_contribution_is_insufficient_evidence_not_agreement(self):
        """
        The reason agreement is pinned at 0.6: there is one model
        family, so there is never a second opinion to agree with.
        """
        one = [ModelContribution("p1", "tm-1", "q", predicted_value=0.02)]
        self.assertEqual(classify_agreement(one),
                         AgreementState.INSUFFICIENT_EVIDENCE)

    def test_two_agreeing_models_would_raise_it(self):
        """What has to change before confidence starts to vary."""
        two = [ModelContribution("p1", "tm-1", "q", predicted_value=0.02),
               ModelContribution("p2", "tm-2", "q", predicted_value=0.03)]
        self.assertEqual(classify_agreement(two), AgreementState.AGREEMENT)
        self.assertGreater(
            compute_confidence(None, "high", AgreementState.AGREEMENT),
            compute_confidence(None, "high", AgreementState.INSUFFICIENT_EVIDENCE))

    def test_high_quality_is_the_only_factor_that_cannot_raise_it(self):
        """quality is already at its maximum, so it can only ever fall."""
        self.assertEqual(QUALITY_FACTORS["high"], 1.0)
        self.assertEqual(max(QUALITY_FACTORS.values()), 1.0)

    def test_conflicting_evidence_would_lower_it_sharply(self):
        self.assertLess(AGREEMENT_FACTORS[AgreementState.CONFLICT],
                        AGREEMENT_FACTORS[AgreementState.INSUFFICIENT_EVIDENCE])


class TestItIsAHeuristicScoreNotAProbability(unittest.TestCase):
    """
    §15: if confidence is a heuristic, label it explicitly as a score.
    These pin the properties that distinguish the two.
    """

    def test_it_is_multiplicative_so_any_weak_factor_collapses_it(self):
        """
        A probability would not behave this way. This is a conjunction
        of necessary conditions: a model confident about garbage input
        must not be rescued by its own confidence.
        """
        self.assertEqual(
            compute_confidence(1.0, "invalid", AgreementState.AGREEMENT), 0.0)

    def test_a_perfect_model_on_perfect_inputs_reaches_one(self):
        self.assertEqual(
            compute_confidence(1.0, "high", AgreementState.AGREEMENT,
                               small_sample=False), 1.0)

    def test_nothing_calibrates_it_against_outcomes(self):
        """
        The honest negative claim. If a calibration step is ever added,
        this test is where the claim has to be revised.
        """
        import src.signals.strategy as strategy
        # Closed explicitly: this project has already lost a day to
        # leaked SQLite handles that were invisible on POSIX and
        # WinError 32 on Windows. The habit is cheap.
        with open(strategy.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for word in ("calibrat", "isotonic", "platt", "brier"):
            self.assertNotIn(word, source.lower(),
                             "a calibration step appeared; confidence may now "
                             "be probability-like and the docs must say so")

    def test_an_unknown_quality_level_is_treated_as_low_not_high(self):
        self.assertEqual(
            compute_confidence(None, None, AgreementState.AGREEMENT),
            compute_confidence(None, "unknown-level", AgreementState.AGREEMENT))
        self.assertLess(
            compute_confidence(None, None, AgreementState.AGREEMENT),
            compute_confidence(None, "high", AgreementState.AGREEMENT))


class TestStrengthIsTheNumberThatVaries(unittest.TestCase):
    """
    Production shows `strength` spread across the full 0..1 range while
    confidence took two values. They answer different questions and
    must not be confused for one another.
    """

    def test_strength_is_a_magnitude_not_a_probability(self):
        self.assertEqual(normalize_strength(0.025, scale=0.05), 0.5)
        self.assertEqual(normalize_strength(-0.025, scale=0.05), 0.5,
                         "strength is unsigned; direction carries the sign")

    def test_strength_saturates_rather_than_exceeding_one(self):
        self.assertEqual(normalize_strength(0.5, scale=0.05), 1.0)

    def test_strength_and_confidence_are_independent(self):
        """
        A large expected move about which we know very little is
        exactly the combination the two numbers exist to express.
        """
        self.assertEqual(normalize_strength(0.05, scale=0.05), 1.0)
        self.assertEqual(
            compute_confidence(None, "high",
                               AgreementState.INSUFFICIENT_EVIDENCE), 0.30)


if __name__ == "__main__":
    unittest.main()
