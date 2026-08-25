"""
tests/impact/test_polygon_connector.py
--------------------------------------------
Tests for PolygonConnector — mocked network calls, and a direct check
that the produced Candles report as split/dividend-adjusted (the whole
point of this connector).
"""

import sys
import os
import unittest
from datetime import date, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.impact.polygon_connector import PolygonConnector

FAKE_RESPONSE = {
    "status": "OK",
    "results": [
        {"t": 1755648000000, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 12_000_000},
        {"t": 1755734400000, "o": 101.0, "h": 105.0, "l": 100.5, "c": 104.0, "v": 18_000_000},
    ],
}


class TestIsConfigured(unittest.TestCase):
    def test_with_api_key_is_configured(self):
        self.assertTrue(PolygonConnector(api_key="abc").is_configured())

    def test_without_api_key_is_not_configured(self):
        self.assertFalse(PolygonConnector(api_key=None).is_configured())


class TestGetDailyCandles(unittest.TestCase):
    def setUp(self):
        self.connector = PolygonConnector(api_key="fake-key")

    def test_parses_bars_into_candles(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value=FAKE_RESPONSE):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].close, 101.0)
        self.assertEqual(candles[1].close, 104.0)

    def test_candle_timestamp_is_timezone_aware_utc(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value=FAKE_RESPONSE):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles[0].timestamp.tzinfo, timezone.utc)

    def test_close_is_mapped_onto_adjusted_close(self):
        """The entire purpose of this connector: uses_adjusted must be True."""
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value=FAKE_RESPONSE):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        for candle in candles:
            self.assertTrue(candle.uses_adjusted)
            self.assertEqual(candle.price, candle.close)

    def test_requests_adjusted_true_explicitly(self):
        """Omitting this parameter would silently return unadjusted prices."""
        captured_url = {}
        original = self.connector.fetch_daily_bars_raw

        def spy(ticker, from_date, to_date):
            with patch("src.impact.polygon_connector.urlopen") as mock_urlopen:
                import json as _json
                from io import BytesIO
                mock_urlopen.return_value.__enter__.return_value = BytesIO(_json.dumps(FAKE_RESPONSE).encode())
                result = original(ticker, from_date, to_date)
                captured_url["url"] = mock_urlopen.call_args[0][0]
                return result

        with patch.object(self.connector._rate_limiter, "wait"):
            spy("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertIn("adjusted=true", captured_url["url"])

    def test_not_configured_returns_empty_list_without_network_call(self):
        connector = PolygonConnector(api_key=None)
        with patch.object(connector, "fetch_daily_bars_raw") as mock_fetch:
            candles = connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])
        mock_fetch.assert_not_called()

    def test_network_failure_returns_empty_list_not_an_exception(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", side_effect=RuntimeError("network down")):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])

    def test_error_status_returns_empty_list(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value={"status": "ERROR", "results": []}):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])

    def test_malformed_response_returns_empty_list(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value="not a dict"):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])

    def test_bar_missing_timestamp_is_skipped_not_fabricated(self):
        response = {"status": "OK", "results": [{"o": 100.0, "c": 101.0, "v": 1000}]}
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value=response):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])

    def test_empty_results_returns_empty_list(self):
        with patch.object(self.connector, "fetch_daily_bars_raw", return_value={"status": "OK", "results": []}):
            candles = self.connector.get_daily_candles("NVDA", date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(candles, [])


class TestBatch(unittest.TestCase):
    def test_batch_fetches_each_ticker(self):
        connector = PolygonConnector(api_key="fake-key")
        with patch.object(connector, "fetch_daily_bars_raw", return_value=FAKE_RESPONSE):
            result = connector.get_daily_candles_batch(["NVDA", "AMD"], date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(set(result.keys()), {"NVDA", "AMD"})
        self.assertEqual(len(result["NVDA"]), 2)

    def test_one_failing_ticker_does_not_block_others(self):
        connector = PolygonConnector(api_key="fake-key")

        def fake_fetch(ticker, from_date, to_date):
            if ticker == "BAD":
                raise RuntimeError("simulated failure")
            return FAKE_RESPONSE

        with patch.object(connector, "fetch_daily_bars_raw", side_effect=fake_fetch):
            result = connector.get_daily_candles_batch(["GOOD", "BAD"], date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(len(result["GOOD"]), 2)
        self.assertEqual(result["BAD"], [])


class TestCandleCompatibilityWithPhase6Engine(unittest.TestCase):
    """The connector's whole purpose: its output must be a drop-in Candle for src/impact/engine.py."""

    def test_produced_candles_work_directly_in_build_study(self):
        from src.impact.engine import EventStudyEngine
        from datetime import datetime, timedelta

        connector = PolygonConnector(api_key="fake-key")
        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        bars = {
            "status": "OK",
            "results": [
                {"t": int((base + timedelta(days=i)).timestamp() * 1000),
                 "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + i * 0.1, "v": 1_000_000}
                for i in range(40)
            ],
        }
        with patch.object(connector, "fetch_daily_bars_raw", return_value=bars):
            candles = connector.get_daily_candles("NVDA", date(2026, 8, 1), date(2026, 9, 10))

        engine = EventStudyEngine()
        study = engine.build_study("e1", "NVDA", candles, publication_time=base + timedelta(days=35))
        self.assertTrue(study.quality.is_usable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
