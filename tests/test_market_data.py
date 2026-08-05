"""
test_market_data.py
-----------------------
Unit tests for Market Data v1 (market_data.py).

TESTING STRATEGY: fetch_snapshot() is mocked directly (exactly like
every collector's fetch_* method elsewhere in this project), returning
hand-crafted dicts shaped like yfinance's real `.info` — so tests are
offline, deterministic, and never touch the network or require
yfinance itself to be installed.
"""

import unittest
from unittest.mock import patch

from market_data import MarketDataFetcher, normalize_ticker_for_yfinance


class TestNormalizeTickerForYfinance(unittest.TestCase):
    """
    Tests for normalize_ticker_for_yfinance() — reproduces the exact
    real-world data-quality bug found in production output: bare crypto
    tickers ("ETH", "XRP") returning wrong data instead of an error.
    """

    def test_crypto_ticker_gets_usd_suffix(self):
        self.assertEqual(normalize_ticker_for_yfinance("ETH", "crypto"), "ETH-USD")
        self.assertEqual(normalize_ticker_for_yfinance("XRP", "crypto"), "XRP-USD")
        self.assertEqual(normalize_ticker_for_yfinance("ADA", "crypto"), "ADA-USD")

    def test_bvb_ticker_returns_none_to_signal_skip(self):
        self.assertIsNone(normalize_ticker_for_yfinance("BRD", "bvb"))
        self.assertIsNone(normalize_ticker_for_yfinance("TLV", "bvb"))

    def test_stock_ticker_is_unchanged(self):
        self.assertEqual(normalize_ticker_for_yfinance("AAPL", "stocks"), "AAPL")

    def test_etf_ticker_is_unchanged(self):
        self.assertEqual(normalize_ticker_for_yfinance("SPY", "etf"), "SPY")



FULL_INFO = {
    "currentPrice": 220.0,
    "previousClose": 200.0,
    "fiftyTwoWeekHigh": 250.0,
    "fiftyTwoWeekLow": 150.0,
    "trailingPE": 45.5,
    "marketCap": 700_000_000_000,
    "currency": "USD",
}


class TestGetSnapshot(unittest.TestCase):
    def setUp(self):
        self.fetcher = MarketDataFetcher()

    def test_parses_all_fields_from_full_info(self):
        with patch.object(self.fetcher, "fetch_snapshot", return_value=FULL_INFO):
            snapshot = self.fetcher.get_snapshot("TSLA")

        self.assertEqual(snapshot["ticker"], "TSLA")
        self.assertEqual(snapshot["current_price"], 220.0)
        self.assertEqual(snapshot["trailing_pe"], 45.5)
        self.assertEqual(snapshot["currency"], "USD")
        self.assertNotIn("error", snapshot)

    def test_computes_daily_change_pct_correctly(self):
        with patch.object(self.fetcher, "fetch_snapshot", return_value=FULL_INFO):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertEqual(snapshot["daily_change_pct"], 10.0)

    def test_computes_pct_from_52w_high_correctly(self):
        with patch.object(self.fetcher, "fetch_snapshot", return_value=FULL_INFO):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertEqual(snapshot["pct_from_52w_high"], -12.0)

    def test_computes_pct_from_52w_low_correctly(self):
        with patch.object(self.fetcher, "fetch_snapshot", return_value=FULL_INFO):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertAlmostEqual(snapshot["pct_from_52w_low"], 46.67, places=1)

    def test_uses_fallback_key_when_primary_price_field_missing(self):
        info = {**FULL_INFO}
        del info["currentPrice"]
        info["regularMarketPrice"] = 221.0
        with patch.object(self.fetcher, "fetch_snapshot", return_value=info):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertEqual(snapshot["current_price"], 221.0)

    def test_missing_pe_ratio_returns_none_not_error(self):
        info = {**FULL_INFO}
        del info["trailingPE"]
        with patch.object(self.fetcher, "fetch_snapshot", return_value=info):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertIsNone(snapshot["trailing_pe"])
        self.assertNotIn("error", snapshot)

    def test_empty_info_returns_error_and_none_fields(self):
        with patch.object(self.fetcher, "fetch_snapshot", return_value={}):
            snapshot = self.fetcher.get_snapshot("UNKNOWN")
        self.assertIn("error", snapshot)
        self.assertIsNone(snapshot["current_price"])

    def test_fetch_exception_returns_error_gracefully(self):
        with patch.object(self.fetcher, "fetch_snapshot", side_effect=RuntimeError("network down")):
            snapshot = self.fetcher.get_snapshot("TSLA")
        self.assertIn("error", snapshot)
        self.assertIn("network down", snapshot["error"])


class TestGetSnapshotsBatch(unittest.TestCase):
    def setUp(self):
        self.fetcher = MarketDataFetcher()

    def test_fetches_multiple_tickers(self):
        def fake_fetch(ticker):
            return FULL_INFO

        with patch.object(self.fetcher, "fetch_snapshot", side_effect=fake_fetch):
            snapshots = self.fetcher.get_snapshots_batch(["TSLA", "AAPL"])

        self.assertEqual(set(snapshots.keys()), {"TSLA", "AAPL"})
        self.assertNotIn("error", snapshots["TSLA"])

    def test_one_failing_ticker_does_not_block_others(self):
        def fake_fetch(ticker):
            if ticker == "BAD":
                raise RuntimeError("simulated failure")
            return FULL_INFO

        with patch.object(self.fetcher, "fetch_snapshot", side_effect=fake_fetch):
            snapshots = self.fetcher.get_snapshots_batch(["GOOD", "BAD"])

        self.assertNotIn("error", snapshots["GOOD"])
        self.assertIn("error", snapshots["BAD"])

    def test_empty_ticker_list_returns_empty_dict(self):
        self.assertEqual(self.fetcher.get_snapshots_batch([]), {})


class TestGetPriceHistory(unittest.TestCase):
    def setUp(self):
        self.fetcher = MarketDataFetcher()

    def test_parses_close_prices_into_date_series(self):
        import pandas as pd
        history_df = pd.DataFrame(
            {"Close": [100.0, 102.5, 101.2]},
            index=pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]),
        )
        with patch.object(self.fetcher, "fetch_price_history_raw", return_value=history_df):
            series = self.fetcher.get_price_history("TSLA")

        self.assertEqual(len(series), 3)
        self.assertEqual(series[0], {"date": "2026-07-01", "close": 100.0})
        self.assertEqual(series[-1], {"date": "2026-07-03", "close": 101.2})

    def test_empty_history_returns_empty_list(self):
        import pandas as pd
        with patch.object(self.fetcher, "fetch_price_history_raw", return_value=pd.DataFrame()):
            series = self.fetcher.get_price_history("UNKNOWN")
        self.assertEqual(series, [])

    def test_fetch_exception_returns_empty_list_not_error(self):
        with patch.object(self.fetcher, "fetch_price_history_raw", side_effect=RuntimeError("network down")):
            series = self.fetcher.get_price_history("TSLA")
        self.assertEqual(series, [])

    def test_none_history_returns_empty_list(self):
        with patch.object(self.fetcher, "fetch_price_history_raw", return_value=None):
            series = self.fetcher.get_price_history("TSLA")
        self.assertEqual(series, [])


class TestGetPriceHistoryBatch(unittest.TestCase):
    def setUp(self):
        self.fetcher = MarketDataFetcher()

    def test_fetches_multiple_tickers(self):
        import pandas as pd
        history_df = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-07-01"]))
        with patch.object(self.fetcher, "fetch_price_history_raw", return_value=history_df):
            histories = self.fetcher.get_price_history_batch(["AAA", "BBB"])
        self.assertEqual(set(histories.keys()), {"AAA", "BBB"})
        self.assertEqual(len(histories["AAA"]), 1)

    def test_one_failing_ticker_does_not_block_others(self):
        import pandas as pd
        history_df = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-07-01"]))

        def fake_fetch(ticker, days=30):
            if ticker == "BAD":
                raise RuntimeError("simulated failure")
            return history_df

        with patch.object(self.fetcher, "fetch_price_history_raw", side_effect=fake_fetch):
            histories = self.fetcher.get_price_history_batch(["GOOD", "BAD"])
        self.assertEqual(len(histories["GOOD"]), 1)
        self.assertEqual(histories["BAD"], [])

    def test_empty_ticker_list_returns_empty_dict(self):
        self.assertEqual(self.fetcher.get_price_history_batch([]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
