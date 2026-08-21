"""
test_fred_connector.py
--------------------------
Unit tests for FRED Connector v1.
"""

import unittest
from unittest.mock import patch

from fred_connector import FredConnector


FAKE_RESPONSE = {
    "observations": [
        {"date": "2026-07-01", "value": "3.4"},
    ]
}


class TestIsConfigured(unittest.TestCase):
    def test_with_api_key_is_configured(self):
        self.assertTrue(FredConnector(api_key="abc").is_configured())

    def test_without_api_key_is_not_configured(self):
        self.assertFalse(FredConnector(api_key=None).is_configured())


class TestGetLatestValue(unittest.TestCase):
    def setUp(self):
        self.connector = FredConnector(api_key="fake-key")

    def test_parses_observation_correctly(self):
        with patch.object(self.connector, "fetch_latest_observation_raw", return_value=FAKE_RESPONSE):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertEqual(result["series_id"], "UNRATE")
        self.assertEqual(result["label"], "Rata șomajului")
        self.assertEqual(result["value"], 3.4)
        self.assertEqual(result["date"], "2026-07-01")

    def test_not_configured_returns_none_without_network_call(self):
        connector = FredConnector(api_key=None)
        with patch.object(connector, "fetch_latest_observation_raw") as mock_fetch:
            result = connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)
        mock_fetch.assert_not_called()

    def test_fetch_exception_returns_none_gracefully(self):
        with patch.object(self.connector, "fetch_latest_observation_raw", side_effect=RuntimeError("network down")):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)

    def test_missing_value_placeholder_returns_none(self):
        response = {"observations": [{"date": "2026-07-01", "value": "."}]}
        with patch.object(self.connector, "fetch_latest_observation_raw", return_value=response):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)

    def test_empty_observations_returns_none(self):
        with patch.object(self.connector, "fetch_latest_observation_raw", return_value={"observations": []}):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)

    def test_unparseable_value_returns_none(self):
        response = {"observations": [{"date": "2026-07-01", "value": "not-a-number"}]}
        with patch.object(self.connector, "fetch_latest_observation_raw", return_value=response):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)

    def test_malformed_response_shape_returns_none(self):
        with patch.object(self.connector, "fetch_latest_observation_raw", return_value="not a dict"):
            result = self.connector.get_latest_value("UNRATE", "Rata șomajului")
        self.assertIsNone(result)


class TestGetAllLatest(unittest.TestCase):
    def test_fetches_all_configured_series(self):
        connector = FredConnector(api_key="fake-key", series={"UNRATE": "Somaj", "GDP": "PIB"})
        with patch.object(connector, "fetch_latest_observation_raw", return_value=FAKE_RESPONSE):
            results = connector.get_all_latest()
        self.assertEqual(len(results), 2)

    def test_one_failing_series_does_not_block_others(self):
        connector = FredConnector(api_key="fake-key", series={"GOOD": "Good", "BAD": "Bad"})

        def fake_fetch(series_id):
            if series_id == "BAD":
                raise RuntimeError("simulated failure")
            return FAKE_RESPONSE

        with patch.object(connector, "fetch_latest_observation_raw", side_effect=fake_fetch):
            results = connector.get_all_latest()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["series_id"], "GOOD")

    def test_not_configured_returns_empty_list(self):
        connector = FredConnector(api_key=None)
        self.assertEqual(connector.get_all_latest(), [])

    def test_default_series_are_used_when_not_specified(self):
        connector = FredConnector(api_key="fake-key")
        self.assertEqual(set(connector.series.keys()), set(FredConnector.DEFAULT_SERIES.keys()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
