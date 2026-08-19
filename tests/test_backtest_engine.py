"""
test_backtest_engine.py
---------------------------
Unit tests for Backtest Engine v1.1 (per-time-horizon holding period).

TESTING STRATEGY: fetch_price_history() is mocked with a small fake
pandas DataFrame — no real network call, deterministic prices.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backtest_engine import BacktestEngine


def make_price_df(prices_by_date):
    import pandas as pd
    dates = pd.to_datetime(list(prices_by_date.keys()), utc=True)
    return pd.DataFrame({"Close": list(prices_by_date.values())}, index=dates)


def make_recommendation(entity="Tesla", ticker="TSLA", recommendation="BUY",
                         generated_at=None, time_horizon=None):
    return {
        "entity": entity, "ticker": ticker, "recommendation": recommendation,
        "generated_at": generated_at, "time_horizon": time_horizon,
    }


class TestHoldingPeriodResolution(unittest.TestCase):
    def test_short_term_uses_short_default(self):
        engine = BacktestEngine()
        rec = make_recommendation(time_horizon="short-term")
        self.assertEqual(engine._holding_period_for(rec), 5)

    def test_long_term_uses_long_default(self):
        engine = BacktestEngine()
        rec = make_recommendation(time_horizon="long-term")
        self.assertEqual(engine._holding_period_for(rec), 45)

    def test_mixed_uses_medium_default(self):
        engine = BacktestEngine()
        rec = make_recommendation(time_horizon="mixed")
        self.assertEqual(engine._holding_period_for(rec), 15)

    def test_missing_horizon_falls_back_to_instance_default(self):
        engine = BacktestEngine(holding_period_days=7)
        rec = make_recommendation(time_horizon=None)
        self.assertEqual(engine._holding_period_for(rec), 7)

    def test_unrecognized_horizon_falls_back_to_instance_default(self):
        engine = BacktestEngine(holding_period_days=7)
        rec = make_recommendation(time_horizon="something-unknown")
        self.assertEqual(engine._holding_period_for(rec), 7)

    def test_custom_mapping_overrides_defaults(self):
        engine = BacktestEngine(holding_period_days_by_horizon={"long-term": 90})
        rec = make_recommendation(time_horizon="long-term")
        self.assertEqual(engine._holding_period_for(rec), 90)


class TestLongTermNotCheckedTooEarly(unittest.TestCase):
    """
    The exact scenario the user found in the real dashboard: a
    'long-term' BUY should NOT be checked (and therefore never marked
    incorrect) after only 5 days — it needs its own, longer horizon.
    """

    def test_long_term_recommendation_5_days_old_is_skipped_as_too_early(self):
        engine = BacktestEngine()
        generated_at = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        rec = make_recommendation(recommendation="BUY", time_horizon="long-term", generated_at=generated_at)

        result = engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn("not fully elapsed", result["skipped_reason"])

    def test_long_term_recommendation_45_days_old_is_checked(self):
        engine = BacktestEngine()
        generated_at = datetime.now(timezone.utc) - timedelta(days=46)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=46)).date().isoformat(): 110.0,
        })
        rec = make_recommendation(recommendation="BUY", time_horizon="long-term", generated_at=generated_at.isoformat())

        with patch.object(engine, "fetch_price_history", return_value=price_df):
            result = engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertEqual(result["holding_period_days_used"], 45)
        self.assertTrue(result["was_correct"])

    def test_short_term_recommendation_5_days_old_is_checked_as_before(self):
        engine = BacktestEngine()
        generated_at = datetime.now(timezone.utc) - timedelta(days=6)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 105.0,
        })
        rec = make_recommendation(recommendation="BUY", time_horizon="short-term", generated_at=generated_at.isoformat())

        with patch.object(engine, "fetch_price_history", return_value=price_df):
            result = engine.check_recommendation(rec)

        self.assertEqual(result["outcome"], "checked")
        self.assertEqual(result["holding_period_days_used"], 5)


class TestCheckRecommendation(unittest.TestCase):
    def setUp(self):
        self.engine = BacktestEngine()

    def test_no_ticker_is_skipped(self):
        rec = make_recommendation(ticker=None)
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_hold_recommendation_is_skipped(self):
        rec = make_recommendation(recommendation="HOLD")
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_invalid_generated_at_is_skipped(self):
        rec = make_recommendation(generated_at="not-a-date")
        result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_fetch_exception_is_skipped_gracefully(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        rec = make_recommendation(time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", side_effect=RuntimeError("network down")):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "skipped")

    def test_buy_correct_when_price_rises(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 110.0,
        })
        rec = make_recommendation(recommendation="BUY", time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", return_value=price_df):
            result = self.engine.check_recommendation(rec)
        self.assertTrue(result["was_correct"])

    def test_sell_correct_when_price_falls(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 90.0,
        })
        rec = make_recommendation(recommendation="SELL", time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", return_value=price_df):
            result = self.engine.check_recommendation(rec)
        self.assertTrue(result["was_correct"])

    def test_strong_buy_is_backtested_as_a_buy_direction_call(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 110.0,
        })
        rec = make_recommendation(recommendation="STRONG_BUY", time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", return_value=price_df):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "checked")
        self.assertTrue(result["was_correct"])

    def test_strong_sell_is_backtested_as_a_sell_direction_call(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 90.0,
        })
        rec = make_recommendation(recommendation="STRONG_SELL", time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", return_value=price_df):
            result = self.engine.check_recommendation(rec)
        self.assertEqual(result["outcome"], "checked")
        self.assertTrue(result["was_correct"])

    def test_strong_buy_marked_incorrect_when_price_falls(self):
        generated_at = datetime.now(timezone.utc) - timedelta(days=10)
        price_df = make_price_df({
            (generated_at + timedelta(days=1)).date().isoformat(): 100.0,
            (generated_at + timedelta(days=6)).date().isoformat(): 90.0,
        })
        rec = make_recommendation(recommendation="STRONG_BUY", time_horizon="short-term", generated_at=generated_at.isoformat())
        with patch.object(self.engine, "fetch_price_history", return_value=price_df):
            result = self.engine.check_recommendation(rec)
        self.assertFalse(result["was_correct"])


class TestRunBacktest(unittest.TestCase):
    def test_summary_counts_checked_and_skipped(self):
        engine = BacktestEngine()
        recs = [make_recommendation(ticker=None), make_recommendation(recommendation="HOLD")]
        result = engine.run_backtest(recs)
        self.assertEqual(result["summary"]["total_recommendations"], 2)
        self.assertEqual(result["summary"]["checked"], 0)
        self.assertEqual(result["summary"]["skipped"], 2)

    def test_empty_batch(self):
        engine = BacktestEngine()
        result = engine.run_backtest([])
        self.assertEqual(result["summary"]["total_recommendations"], 0)
        self.assertIsNone(result["summary"]["hit_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
