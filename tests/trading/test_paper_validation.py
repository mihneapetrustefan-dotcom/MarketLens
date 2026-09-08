"""
tests/trading/test_paper_validation.py
--------------------------------------------
Phase 25 §21, §22, §38, §41 — what a paper run establishes, and the
governance ladder it may climb.

THE TWO CLAIMS THIS FILE DEFENDS
------------------------------------
    PAPER PERFORMANCE IS EVIDENCE, NOT PROOF.
    `is_conclusive()` requires a real sample AND every dimension
    measured, and on anything this project can currently produce it
    returns False. `compare()` reports differences and declares no
    winner.

    NOTHING REACHES LIVE.
    Every automatic transition is enumerated, LIVE_ELIGIBLE is absent
    from all of them, and both `assert_transition` and
    `PaperValidator.review` refuse it independently.
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.data_access.challenger_schema import initialize_challenger_schema
from src.domain.trading_loop_models import (
    AUTOMATIC_TRANSITIONS, DimensionReading, PaperStrategyState,
    PaperValidation, PromotionRefused, QualityDimension, assert_transition,
)
from src.trading.validation import (
    MIN_PAPER_TRADES, NotEligibleForPaper, PaperValidator, compare,
    measure_dimensions, validation_id_for,
)
from tests.trading.helpers import NOW, make_connection


def a_challenger(conn, challenger_id="chl-1", status="paper_candidate"):
    initialize_challenger_schema(conn)
    conn.execute("""
        INSERT INTO challengers
        (challenger_id, version, method_version, variant_type, name, status,
         candidate_id, hypothesis_id, experiment_id, conclusion_id, family_id,
         baseline_kind, baseline_name, baseline_version, change_kind,
         change_summary, fingerprint, created_at)
        VALUES (?,1,'phase24-v1','signal','c',?,'','','','','','','','','','',
                'fp','2026-09-01')
    """, (challenger_id, status))
    conn.commit()


class TestTheLadder(unittest.TestCase):

    def test_live_eligible_is_absent_from_every_automatic_transition(self):
        for state, allowed in AUTOMATIC_TRANSITIONS.items():
            with self.subTest(state=state.value):
                self.assertNotIn(PaperStrategyState.LIVE_ELIGIBLE, allowed)

    def test_the_two_human_steps_are_not_automatic(self):
        """
        BACKTEST_VALIDATED -> PAPER_ELIGIBLE and PAPER_EVALUATED ->
        HUMAN_REVIEW are judgements, not facts, and are absent on
        purpose.
        """
        self.assertEqual(
            AUTOMATIC_TRANSITIONS[PaperStrategyState.BACKTEST_VALIDATED], ())
        self.assertEqual(
            AUTOMATIC_TRANSITIONS[PaperStrategyState.PAPER_EVALUATED], ())

    def test_a_session_start_and_end_are_automatic(self):
        assert_transition(PaperStrategyState.PAPER_ELIGIBLE,
                          PaperStrategyState.PAPER_RUNNING)
        assert_transition(PaperStrategyState.PAPER_RUNNING,
                          PaperStrategyState.PAPER_EVALUATED)


class TestChallengerGovernance(unittest.TestCase):

    def setUp(self):
        self.conn = make_connection()
        self.validator = PaperValidator(self.conn)

    def tearDown(self):
        self.conn.close()

    def start(self, **kwargs):
        return self.validator.start(
            strategy_id="s", strategy_version="v1", session_id="sess",
            at=NOW, **kwargs)

    def test_a_paper_candidate_challenger_may_run(self):
        a_challenger(self.conn, status="paper_candidate")
        validation = self.start(challenger_id="chl-1")
        self.assertIs(validation.state, PaperStrategyState.PAPER_RUNNING)

    def test_a_promising_challenger_may_not(self):
        """§38: PAPER_CANDIDATE is a label a named reviewer applied."""
        a_challenger(self.conn, status="promising")
        with self.assertRaises(NotEligibleForPaper) as caught:
            self.start(challenger_id="chl-1")
        self.assertIn("PAPER_CANDIDATE", str(caught.exception))

    def test_an_unknown_challenger_may_not(self):
        a_challenger(self.conn, challenger_id="chl-other")
        with self.assertRaises(NotEligibleForPaper):
            self.start(challenger_id="chl-missing")

    def test_a_run_with_no_challenger_is_allowed(self):
        """A plain strategy run needs no Phase 24 approval."""
        self.assertIsNotNone(self.start())

    def test_the_validation_id_is_deterministic(self):
        first = validation_id_for("s", "v1", "sess")
        second = validation_id_for("s", "v1", "sess")
        self.assertEqual(first, second)
        self.assertNotEqual(first, validation_id_for("s", "v2", "sess"))


class TestReview(unittest.TestCase):

    def setUp(self):
        self.conn = make_connection()
        self.validator = PaperValidator(self.conn)
        self.validation = self.validator.start(
            strategy_id="s", strategy_version="v1", session_id="sess", at=NOW)

    def tearDown(self):
        self.conn.close()

    def test_a_running_validation_cannot_be_reviewed(self):
        with self.assertRaises(ValueError):
            self.validator.review(self.validation.validation_id,
                                  to_state=PaperStrategyState.HUMAN_REVIEW,
                                  reviewer="g.stefan", reason="looks fine",
                                  at=NOW)

    def test_a_finished_validation_can_be_reviewed(self):
        self.validator.finish(self.validation, NOW + timedelta(days=1))
        review_id = self.validator.review(
            self.validation.validation_id,
            to_state=PaperStrategyState.HUMAN_REVIEW,
            reviewer="g.stefan", reason="the record is worth reading", at=NOW)
        self.assertTrue(review_id)
        reviews = self.validator.repository.reviews_for(
            self.validation.validation_id)
        self.assertEqual(reviews[0]["reviewer"], "g.stefan")

    def test_a_review_needs_a_named_reviewer_and_a_reason(self):
        self.validator.finish(self.validation, NOW + timedelta(days=1))
        for kwargs in ({"reviewer": "", "reason": "r"},
                       {"reviewer": "a", "reason": ""}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.validator.review(
                        self.validation.validation_id,
                        to_state=PaperStrategyState.HUMAN_REVIEW,
                        at=NOW, **kwargs)

    def test_live_eligible_is_refused_at_review(self):
        self.validator.finish(self.validation, NOW + timedelta(days=1))
        with self.assertRaises(PromotionRefused):
            self.validator.review(self.validation.validation_id,
                                  to_state=PaperStrategyState.LIVE_ELIGIBLE,
                                  reviewer="g.stefan",
                                  reason="I really want to", at=NOW)

    def test_reviews_are_append_only(self):
        self.validator.finish(self.validation, NOW + timedelta(days=1))
        for reason in ("first look", "second thoughts"):
            self.validator.review(self.validation.validation_id,
                                  to_state=PaperStrategyState.HUMAN_REVIEW,
                                  reviewer="g.stefan", reason=reason, at=NOW)
        self.assertEqual(
            len(self.validator.repository.reviews_for(
                self.validation.validation_id)), 2)


class TestTheDimensions(unittest.TestCase):

    def a_validation(self, **kwargs):
        defaults = dict(validation_id="v", strategy_id="s",
                        strategy_version="v1", session_id="sess")
        defaults.update(kwargs)
        return PaperValidation(**defaults)

    def test_a_measured_reading_must_carry_a_value(self):
        """
        The combination that turns an unmeasured dimension into a
        passing one. Refused at construction.
        """
        with self.assertRaises(ValueError):
            DimensionReading(QualityDimension.RISK, measured=True, value=None)

    def test_every_dimension_gets_a_reading(self):
        readings = measure_dimensions(self.a_validation(), [])
        self.assertEqual({r.dimension for r in readings},
                         set(QualityDimension))

    def test_an_unmeasurable_dimension_says_why(self):
        readings = {r.dimension: r
                    for r in measure_dimensions(self.a_validation(), [])}
        self.assertFalse(readings[QualityDimension.EXECUTION].measured)
        self.assertIn("no fill",
                      readings[QualityDimension.EXECUTION].detail)

    def test_model_quality_is_never_claimed_from_paper_pnl(self):
        """§41: a strategy with good trades and a bad model must not average."""
        readings = {r.dimension: r
                    for r in measure_dimensions(
                        self.a_validation(realized_pnl=10_000.0), [])}
        self.assertFalse(readings[QualityDimension.MODEL].measured)
        self.assertIn("Phase 18", readings[QualityDimension.MODEL].detail)

    def test_a_strategy_claim_needs_a_real_sample(self):
        few = [{"is_open": False, "net_pnl": 1.0}] * 3
        readings = {r.dimension: r
                    for r in measure_dimensions(
                        self.a_validation(realized_pnl=3.0), few)}
        self.assertFalse(readings[QualityDimension.STRATEGY].measured)
        self.assertIn(str(MIN_PAPER_TRADES),
                      readings[QualityDimension.STRATEGY].detail)

    def test_a_validation_has_no_total_and_cannot_be_sorted(self):
        """§41, and the Phase 24 rule: a sortable score gets sorted."""
        card = self.a_validation()
        for attribute in ("total", "overall", "score", "rank"):
            self.assertFalse(hasattr(card, attribute))
        with self.assertRaises(TypeError):
            sorted([card, self.a_validation(validation_id="w")])

    def test_a_record_with_no_trades_is_not_conclusive(self):
        self.assertFalse(self.a_validation().is_conclusive())

    def test_a_record_with_trades_but_unmeasured_dimensions_is_not_conclusive(self):
        card = self.a_validation(completed_trades=100)
        card.dimensions = measure_dimensions(card, [])
        self.assertFalse(card.is_conclusive())
        self.assertTrue(card.unmeasured())


class TestComparison(unittest.TestCase):

    def test_a_comparison_declares_no_winner(self):
        comparison = compare({"realized_pnl": 100.0, "conclusive": False,
                              "unmeasured": ["model"]},
                             {"realized_pnl": 10.0, "conclusive": False,
                              "unmeasured": ["model", "risk"]})
        for attribute in ("winner", "better", "rank"):
            self.assertFalse(hasattr(comparison, attribute))
        self.assertEqual(comparison.differences["realized_pnl"], 90.0)
        self.assertFalse(comparison.conclusive)
        self.assertIn("neither record supports a claim", comparison.note)

    def test_a_missing_figure_produces_none_not_zero(self):
        """'We could not compare' and 'they are equal' are different."""
        comparison = compare({"realized_pnl": None}, {"realized_pnl": 10.0})
        self.assertIsNone(comparison.differences["realized_pnl"])

    def test_unmeasured_dimensions_are_unioned(self):
        comparison = compare({"unmeasured": ["model"]},
                             {"unmeasured": ["risk"]})
        self.assertEqual(comparison.unmeasured_either, ["model", "risk"])


if __name__ == "__main__":
    unittest.main()
