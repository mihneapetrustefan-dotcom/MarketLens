"""
tests/modeling/test_edge_diagnostic.py
-----------------------------------------------------------
Phase 25.9A edge diagnostic (§39).

THE CASE THAT MATTERS MOST

`TestASharedAnchorManufacturesCorrelation`. Phase 25.9A's Stage 1 test
returned INFORMATION PRESENT at p = 0.002, and the strongest intraday
correlation (-0.362) turned out to be manufactured by label
construction: every post-event window in ImpactEngine shares one
baseline price, so a trailing return ending at that price and a
forward return starting from it are mechanically anti-correlated by
measurement noise alone. Cancelling the anchor flipped the sign.

These cases reproduce that mechanism on synthetic data with NO signal
in it, so the next person to see a large short-horizon correlation has
a ready way to test whether it is real.
"""

import os
import sqlite3
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import scripts.research_edge_diagnostic as D


def spearman(a, b):
    return float(np.corrcoef(D.rank(np.asarray(a, float)),
                             D.rank(np.asarray(b, float)))[0, 1])


class TestRanking(unittest.TestCase):

    def test_distinct_values_rank_in_order(self):
        self.assertEqual(list(D.rank(np.array([30.0, 10.0, 20.0]))),
                         [2.0, 0.0, 1.0])

    def test_ties_share_the_average_rank(self):
        self.assertEqual(list(D.rank(np.array([5.0, 5.0, 1.0]))),
                         [1.5, 1.5, 0.0])

    def test_standardise_leaves_a_constant_column_at_zero(self):
        """A constant feature carries no rank information and must not
        divide by zero."""
        out = D.standardise(np.array([[1.0], [1.0], [1.0]]))
        self.assertTrue(np.all(out == 0))


class TestASharedAnchorManufacturesCorrelation(unittest.TestCase):
    """
    Prices are a pure random walk -- there is no predictability at all.
    Only the anchor price carries independent measurement noise, as a
    stale or mismatched close would.
    """

    def setUp(self):
        rng = np.random.default_rng(7)
        n = 4000
        self.p_prev = 100.0 * np.exp(rng.normal(0, 0.02, n))
        true_anchor = self.p_prev * np.exp(rng.normal(0, 0.02, n))
        measured_anchor = true_anchor * np.exp(rng.normal(0, 0.02, n))
        self.p_5m = true_anchor * np.exp(rng.normal(0, 0.005, n))
        self.p_60m = self.p_5m * np.exp(rng.normal(0, 0.01, n))
        self.anchor = measured_anchor

    def test_a_trailing_and_a_forward_return_look_predictive(self):
        """The artifact: strong negative correlation from noise alone."""
        trailing = self.anchor / self.p_prev - 1
        forward_5m = self.p_5m / self.anchor - 1
        self.assertLess(spearman(trailing, forward_5m), -0.25)

    def test_cancelling_the_anchor_removes_it(self):
        """
        Differencing two windows that share the anchor cancels it,
        leaving only the genuine move -- which, here, is unpredictable.
        """
        trailing = self.anchor / self.p_prev - 1
        r_5m = self.p_5m / self.anchor - 1
        r_60m = self.p_60m / self.anchor - 1
        self.assertLess(abs(spearman(trailing, r_60m - r_5m)), 0.05)

    def test_the_artifact_barely_decays_with_horizon(self):
        """
        The fingerprint seen in production: -0.33 at 5m, -0.30 at 60m.
        Real short-horizon predictability decays; a shared anchor does
        not, because every window inherits the same noisy base.
        """
        trailing = self.anchor / self.p_prev - 1
        ic_5 = spearman(trailing, self.p_5m / self.anchor - 1)
        ic_60 = spearman(trailing, self.p_60m / self.anchor - 1)
        self.assertLess(ic_5, -0.25)
        self.assertLess(ic_60, -0.20)


class TestTheDiagnosticNeverReadsProtectedData(unittest.TestCase):

    def test_load_excludes_rows_at_or_after_the_protected_start(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE research_observations (
                observation_id TEXT, information_cutoff TEXT, quality_level TEXT);
            CREATE TABLE research_features (
                observation_id TEXT, qualified_name TEXT, value_json TEXT);
            CREATE TABLE research_labels (
                observation_id TEXT, name TEXT, value_json TEXT);
            INSERT INTO research_observations VALUES
                ('research', '2026-08-01T10:00:00', 'high'),
                ('protected', '2026-08-20T10:00:00', 'high');
            INSERT INTO research_features VALUES
                ('research', 'market.return_1d', '0.01'),
                ('protected', 'market.return_1d', '0.99');
            INSERT INTO research_labels VALUES
                ('research', 'd5.abnormal_return', '0.02'),
                ('protected', 'd5.abnormal_return', '0.99');
        """)
        ids, _cutoffs, features, targets = D.load(conn)
        self.assertEqual(ids, ["research"])
        self.assertNotIn(0.99, list(features["market.return_1d"]))
        self.assertNotIn(0.99, list(targets["d5"]))


class TestPreRegisteredConstants(unittest.TestCase):

    def test_the_search_is_the_one_that_was_registered(self):
        """9 targets x 2 formulations x 26 features = 468."""
        self.assertEqual(len(D.TARGETS), 9)
        self.assertEqual(D.PERMUTATIONS, 500)
        self.assertEqual(D.MIN_ROWS_PER_DATE, 10)

    def test_the_seed_is_fixed_for_reproducibility(self):
        self.assertEqual(D.SEED, 20260913)


if __name__ == "__main__":
    unittest.main(verbosity=2)
