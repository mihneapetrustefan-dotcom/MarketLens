"""
test_backtest_engine.py
--------------------------
Unit tests for Backtest Engine v1 (backtest_engine.py).

TESTING STRATEGY:
fetch_price_history() is mocked directly (exactly like every
collector's fetch_* method elsewhere in this project), returning small,
hand-crafted pandas DataFrames shaped exactly like what yfinance
actually returns (a DatetimeIndex and a "Close" column) — so tests
are offline, deterministic, and never touch the network or require
yfinance itself to be installed.
"""

import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import pandas as pd

from backtest_engine import BacktestEngine


def make_price_history(prices_by_date):
    """Helper: builds a yfinance-shaped DataFrame from {date: close_price}."""
    dates = pd.to_datetime(list(prices_by_date.keys()))
    closes = list(prices_by_date.values())
    return pd.DataFrame({"Close": closes}, index=dates)


def make_recommendation(**overrides):
    base = {
        "entity": "Tesla",
        "ticker": "TSLA",
        "recommendation": "BUY",
        "confidence_score": 0.7,
        "generated_at": "2026-07-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestSkipConditions(unittest.TestCase):
    """Tests for every reason a recommendation can't be checked."""

    def setUp(self):
        self.engine = BacktestEngine(holding_period_days=5)

    def test_no_ticker_is_skipped(self):
        rec = make_recommendation(ticker=None)
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn("ticker", result["skipped_reason"].lower())

    def test_hold_recommendation_is_skipped(self):
        rec = make_recommendation(recommendation="HOLD")
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_missing_generated_at_is_skipped(self):
        rec = make_recommendation()
        del rec["generated_at"]
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_holding_period_not_elapsed_is_skipped(self):
        rec = make_recommendation(generated_at=datetime.now(timezone.utc).isoformat())
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn("not fully elapsed", result["skipped_reason"])

    def test_fetch_exception_is_skipped_gracefully(self):
        rec = make_recommendation()
        with patch.object(self.engine, "fetch_price_history", side_effect=RuntimeError("network down")):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn("Price data unavailable", result["skipped_reason"])

    def test_empty_price_history_is_skipped(self):
        rec = make_recommendation()
        with patch.object(self.engine, "fetch_price_history", return_value=pd.DataFrame()):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")


class TestCheckRecommendationOutcomes(unittest.TestCase):
    """Tests for the actual correctness-checking logic."""

    def setUp(self):
        self.engine = BacktestEngine(holding_period_days=5)

    def test_buy_followed_by_price_rise_is_correct(self):
        rec = make_recommendation(recommendation="BUY", generated_at="2026-07-01T00:00:00+00:00")
        history = make_price_history({"2026-07-01": 100.0, "2026-07-06": 110.0})
        with patch.object(self.engine, "fetch_price_history", return_value=history):
            result = self.engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertTrue(result["was_correct"])
        self.assertEqual(result["actual_change_pct"], 10.0)

    def test_buy_followed_by_price_drop_is_incorrect(self):
        rec = make_recommendation(recommendation="BUY", generated_at="2026-07-01T00:00:00+00:00")
        history = make_price_history({"2026-07-01": 100.0, "2026-07-06": 90.0})
        with patch.object(self.engine, "fetch_price_history", return_value=history):
            result = self.engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertFalse(result["was_correct"])
        self.assertEqual(result["actual_change_pct"], -10.0)

    def test_sell_followed_by_price_drop_is_correct(self):
        rec = make_recommendation(recommendation="SELL", generated_at="2026-07-01T00:00:00+00:00")
        history = make_price_history({"2026-07-01": 100.0, "2026-07-06": 85.0})
        with patch.object(self.engine, "fetch_price_history", return_value=history):
            result = self.engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertTrue(result["was_correct"])

    def test_sell_followed_by_price_rise_is_incorrect(self):
        rec = make_recommendation(recommendation="SELL", generated_at="2026-07-01T00:00:00+00:00")
        history = make_price_history({"2026-07-01": 100.0, "2026-07-06": 105.0})
        with patch.object(self.engine, "fetch_price_history", return_value=history):
            result = self.engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertFalse(result["was_correct"])

    def test_weekend_gap_uses_next_available_trading_day(self):
        rec = make_recommendation(recommendation="BUY", generated_at="2026-07-01T00:00:00+00:00")
        history = make_price_history({"2026-07-01": 100.0, "2026-07-08": 120.0})
        with patch.object(self.engine, "fetch_price_history", return_value=history):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "checked")
        self.assertEqual(result["exit_price"], 120.0)


class TestRunBacktest(unittest.TestCase):
    """Tests for run_backtest(): the full-batch aggregation method."""

    def setUp(self):
        self.engine = BacktestEngine(holding_period_days=5)

    def test_aggregates_hit_rate_across_mixed_outcomes(self):
        recommendations = [
            make_recommendation(entity="A", ticker="AAA", recommendation="BUY"),
            make_recommendation(entity="B", ticker="BBB", recommendation="BUY"),
            make_recommendation(entity="C", ticker=None, recommendation="BUY"),
        ]

        def fake_fetch(ticker, start, end):
            if ticker == "AAA":
                return make_price_history({"2026-07-01": 100.0, "2026-07-06": 110.0})
            return make_price_history({"2026-07-01": 100.0, "2026-07-06": 90.0})

        with patch.object(self.engine, "fetch_price_history", side_effect=fake_fetch):
            result = self.engine.run_backtest(recommendations)

        summary = result["summary"]
        self.assertEqual(summary["total_recommendations"], 3)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["correct"], 1)
        self.assertEqual(summary["hit_rate"], 0.5)

    def test_empty_list_returns_none_hit_rate_not_crash(self):
        result = self.engine.run_backtest([])
        self.assertEqual(result["summary"]["total_recommendations"], 0)
        self.assertIsNone(result["summary"]["hit_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
