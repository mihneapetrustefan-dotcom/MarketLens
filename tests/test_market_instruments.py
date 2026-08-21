"""
test_market_instruments.py
------------------------------
Sanity tests for market_instruments.py (Indices + Commodities registry).
"""

import unittest

from market_instruments import INDICES, COMMODITIES, MARKET_INSTRUMENTS


class TestRegistryIntegrity(unittest.TestCase):
    def test_no_duplicate_tickers(self):
        tickers = [i["yfinance_ticker"] for i in MARKET_INSTRUMENTS]
        self.assertEqual(len(tickers), len(set(tickers)))

    def test_no_duplicate_names(self):
        names = [i["name"] for i in MARKET_INSTRUMENTS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_entry_has_required_fields(self):
        for entry in MARKET_INSTRUMENTS:
            self.assertIn("name", entry)
            self.assertIn("yfinance_ticker", entry)
            self.assertIn("category", entry)
            self.assertIn(entry["category"], ("index", "commodity"))

    def test_combined_list_matches_indices_plus_commodities(self):
        self.assertEqual(len(MARKET_INSTRUMENTS), len(INDICES) + len(COMMODITIES))

    def test_index_tickers_use_caret_prefix_convention(self):
        for entry in INDICES:
            self.assertTrue(entry["yfinance_ticker"].startswith("^"))

    def test_commodity_tickers_use_futures_suffix_convention(self):
        for entry in COMMODITIES:
            self.assertTrue(entry["yfinance_ticker"].endswith("=F"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
