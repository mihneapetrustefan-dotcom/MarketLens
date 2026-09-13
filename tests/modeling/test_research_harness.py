"""
tests/modeling/test_research_harness.py
-----------------------------------------------------------
The Phase 25.9 research harness (§41).

These assert the properties that make a research result trustworthy:
the protected window is unreachable during development, leakage
controls are the library's own, feature selection sees only training
rows, and the verdict rules are the pre-registered ones.

They also pin the finding this phase produced: that beating the
mandatory baselines is a weak bar under regime shift, and that a
coin-flip model can clear it.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scripts.research_model_quality as R

START = datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc)


def daily_cutoffs(days, per_day=30):
    return [START + timedelta(days=d, hours=h % 8)
            for d in range(days) for h in range(per_day)]


class TestProtectedWindowIsUnreachable(unittest.TestCase):

    def test_no_fold_ever_contains_a_protected_row(self):
        """The whole point of reserving it."""
        cutoffs = daily_cutoffs(60)
        folds = R.build_folds(cutoffs)
        self.assertTrue(folds)
        for fold in folds:
            for index in fold["train_idx"] + fold["test_idx"]:
                self.assertLess(cutoffs[index], R.PROTECTED_START,
                                f"{fold['label']} reached protected data")

    def test_a_test_window_never_extends_past_the_protected_start(self):
        for fold in R.build_folds(daily_cutoffs(60)):
            self.assertLessEqual(fold["test_end"], R.PROTECTED_START)


class TestLeakageControls(unittest.TestCase):

    def test_training_rows_resolve_before_the_test_starts(self):
        """
        Purge at the label horizon: no training label may resolve
        inside the test period.
        """
        cutoffs = daily_cutoffs(45)
        for fold in R.build_folds(cutoffs):
            for index in fold["train_idx"]:
                resolves = cutoffs[index] + timedelta(days=R.LABEL_HORIZON_DAYS)
                self.assertLessEqual(resolves, fold["test_start"])

    def test_the_purge_horizon_covers_five_trading_days(self):
        """
        d5 is five TRADING days, which spans up to seven calendar days
        across a weekend. A five-calendar-day purge under-protects.
        """
        self.assertGreaterEqual(R.LABEL_HORIZON_DAYS, 7.0)

    def test_folds_are_chronological_and_expanding(self):
        folds = [f for f in R.build_folds(daily_cutoffs(45)) if f["train_idx"]]
        starts = [f["test_start"] for f in folds]
        self.assertEqual(starts, sorted(starts))
        sizes = [len(f["train_idx"]) for f in folds]
        self.assertEqual(sizes, sorted(sizes))


class TestFeatureSelectionSeesOnlyTraining(unittest.TestCase):

    def test_dense_columns_ignore_rows_outside_the_training_set(self):
        """
        C3's rule must be computed from the fold's training rows. A
        column that is sparse in training but full in test must still
        be dropped.
        """
        X = [[1.0, None],   # training
             [1.0, None],   # training
             [1.0, 9.0],    # test
             [1.0, 9.0]]    # test
        self.assertEqual(R.dense_columns(X, train_idx=[0, 1]), [0])

    def test_no_training_rows_selects_nothing(self):
        self.assertEqual(R.dense_columns([[1.0]], train_idx=[]), [])


class TestCalibration(unittest.TestCase):

    def test_a_perfect_forecast_has_zero_brier(self):
        self.assertEqual(R.brier([1.0, 0.0], [0.01, -0.01]), 0.0)

    def test_a_maximally_wrong_forecast_has_brier_one(self):
        self.assertEqual(R.brier([0.0, 1.0], [0.01, -0.01]), 1.0)

    def test_abstentions_are_excluded(self):
        self.assertEqual(R.brier([1.0, None], [0.01, -0.01]), 0.0)


class TestPreRegisteredVerdict(unittest.TestCase):

    def fold(self, name, beats, clusters=50, brier=None, base=None):
        result = {"fold": name, "beats_all_baselines": beats,
                  "test_clusters": clusters}
        if brier is not None:
            result["brier"] = brier
            result["brier_base_rate"] = base
        return result

    def test_fewer_than_three_folds_is_insufficient_data(self):
        """Regardless of score -- pre-registration §5.2."""
        verdict = R.verdict("CX", [self.fold("a", True), self.fold("b", True)],
                            valid_folds=2)
        self.assertEqual(verdict["status"], "INSUFFICIENT DATA")

    def test_a_single_winning_fold_is_not_a_majority(self):
        verdict = R.verdict("CX", [self.fold("a", True), self.fold("b", False),
                                   self.fold("c", False)], valid_folds=3)
        self.assertEqual(verdict["status"], "NOT QUALIFIED")

    def test_beating_baselines_everywhere_still_fails_on_calibration(self):
        """
        THE PHASE 25.9 FINDING, pinned.

        C1 beat every mandatory baseline in all three folds and
        averaged 0.504 directional accuracy -- a coin flip. The existing
        deployability gate would have certified it. The pre-registered
        Brier criterion is what refused it.
        """
        folds = [self.fold("wf1", True, 127, brier=0.367, base=0.293),
                 self.fold("wf2", True, 116, brier=0.274, base=0.252),
                 self.fold("wf3", True, 93, brier=0.299, base=0.253)]
        verdict = R.verdict("C1", folds, valid_folds=3)
        self.assertEqual(verdict["status"], "NOT QUALIFIED")
        self.assertTrue(any("Brier" in r for r in verdict["reasons"]))

    def test_a_calibrated_majority_winner_survives(self):
        folds = [self.fold("a", True, brier=0.20, base=0.25),
                 self.fold("b", True, brier=0.21, base=0.25),
                 self.fold("c", False, brier=0.22, base=0.25)]
        verdict = R.verdict("CX", folds, valid_folds=3)
        self.assertEqual(verdict["status"], "SURVIVES RESEARCH REGION")

    def test_a_small_pooled_sample_is_refused(self):
        folds = [self.fold(n, True, clusters=5) for n in ("a", "b", "c")]
        verdict = R.verdict("CX", folds, valid_folds=3)
        self.assertEqual(verdict["status"], "NOT QUALIFIED")

    def test_the_candidate_set_is_fixed_at_four(self):
        """Pre-registration §6: no fifth candidate after results."""
        self.assertEqual(R.CANDIDATES, ("C0", "C1", "C2", "C3"))


class TestTheMandatoryBaselineIsWeakUnderRegimeShift(unittest.TestCase):
    """
    Why beating the baselines is not, alone, evidence of skill.

    A majority-class baseline is fit on training data. When the test
    period's direction differs, it scores BELOW 0.5 -- and any model
    no better than a coin flip beats it.
    """

    def test_a_regime_shift_drives_the_majority_baseline_below_chance(self):
        from src.domain.model_models import ModelFamily
        from src.modeling import algorithms
        from src.modeling.engine import directional_accuracy

        train_Y = [0.01] * 58 + [-0.01] * 42          # 58% up
        test_Y = [0.01] * 27 + [-0.01] * 73           # 27% up
        params = algorithms.fit(ModelFamily.BASELINE_MAJORITY_CLASS,
                                [[0.0]] * len(train_Y), train_Y)
        predicted = algorithms.predict_batch(params, [[0.0]] * len(test_Y))
        score = directional_accuracy(test_Y, predicted)
        self.assertLess(score, 0.5)

        coin_flip = [0.01, -0.01] * 50
        self.assertGreater(directional_accuracy(test_Y, coin_flip), score)


if __name__ == "__main__":
    unittest.main(verbosity=2)
