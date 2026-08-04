"""
test_risk_score.py
----------------------
Unit tests for Risk Score v1 (risk_score.py).

TESTING STRATEGY: fetch_price_history() is mocked directly (exactly
like Backtest Engine's tests), returning small pandas DataFrames
shaped like yfinance's real output — offline, deterministic, no
network or yfinance dependency required to run the tests.
"""

import math
import unittest
from unittest.mock import patch

import pandas as pd

from risk_score import RiskScoreCalculator


def make_price_history(closes):
    return pd.DataFrame({"Close": closes})


def expected_annualized_volatility(closes):
    """Independently compute the expected volatility using the exact same, well-known formula."""
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(variance) * math.sqrt(252) * 100, 2)


class TestGetRiskScore(unittest.TestCase):
    def setUp(self):
        self.calculator = RiskScoreCalculator(lookback_days=30)

    def test_computes_volatility_matching_manual_calculation(self):
        closes = [100, 102, 101, 103, 100, 105, 98]
        with patch.object(self.calculator, "fetch_price_history", return_value=make_price_history(closes)):
            result = self.calculator.get_risk_score("TEST")
        self.assertEqual(result["annualized_volatility_pct"], expected_annualized_volatility(closes))

    def test_low_volatility_series_gets_low_risk_level(self):
        closes = [100.0, 100.1, 100.0, 100.2, 100.1, 100.0]
        with patch.object(self.calculator, "fetch_price_history", return_value=make_price_history(closes)):
            result = self.calculator.get_risk_score("STABLE")
        self.assertEqual(result["risk_level"], "Low")

    def test_high_volatility_series_gets_high_risk_level(self):
        closes = [100, 140, 80, 130, 70, 150, 60]
        with patch.object(self.calculator, "fetch_price_history", return_value=make_price_history(closes)):
            result = self.calculator.get_risk_score("WILD")
        self.assertEqual(result["risk_level"], "High")

    def test_empty_history_returns_error(self):
        with patch.object(self.calculator, "fetch_price_history", return_value=pd.DataFrame()):
            result = self.calculator.get_risk_score("EMPTY")
        self.assertIn("error", result)
        self.assertIsNone(result["annualized_volatility_pct"])

    def test_single_row_history_returns_error(self):
        with patch.object(self.calculator, "fetch_price_history", return_value=make_price_history([100])):
            result = self.calculator.get_risk_score("ONEROW")
        self.assertIn("error", result)

    def test_fetch_exception_returns_error_gracefully(self):
        with patch.object(self.calculator, "fetch_price_history", side_effect=RuntimeError("network down")):
            result = self.calculator.get_risk_score("BROKEN")
        self.assertIn("error", result)
        self.assertIn("network down", result["error"])


class TestGetRiskScoresBatch(unittest.TestCase):
    def setUp(self):
        self.calculator = RiskScoreCalculator()

    def test_scores_multiple_tickers(self):
        closes = [100, 101, 99, 102, 98]

        def fake_fetch(ticker):
            return make_price_history(closes)

        with patch.object(self.calculator, "fetch_price_history", side_effect=fake_fetch):
            scores = self.calculator.get_risk_scores_batch(["AAA", "BBB"])
        self.assertEqual(set(scores.keys()), {"AAA", "BBB"})
        self.assertNotIn("error", scores["AAA"])

    def test_one_failing_ticker_does_not_block_others(self):
        def fake_fetch(ticker):
            if ticker == "BAD":
                raise RuntimeError("simulated failure")
            return make_price_history([100, 101, 99, 102])

        with patch.object(self.calculator, "fetch_price_history", side_effect=fake_fetch):
            scores = self.calculator.get_risk_scores_batch(["GOOD", "BAD"])
        self.assertNotIn("error", scores["GOOD"])
        self.assertIn("error", scores["BAD"])

    def test_empty_ticker_list_returns_empty_dict(self):
        self.assertEqual(self.calculator.get_risk_scores_batch([]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
