"""
test_portfolio_simulator.py
-------------------------------
Unit tests for Portfolio Simulator v1.
"""

import unittest

from portfolio_simulator import PortfolioSimulator


def make_backtest_result(entity, recommendation, actual_change_pct, outcome="checked"):
    return {
        "entity": entity, "recommendation": recommendation,
        "actual_change_pct": actual_change_pct, "outcome": outcome,
    }


class TestSimulate(unittest.TestCase):
    def setUp(self):
        self.simulator = PortfolioSimulator(investment_per_trade=1000.0)

    def test_buy_with_positive_change_produces_profit(self):
        results = [make_backtest_result("Tesla", "BUY", 10.0)]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades"][0]["final_value"], 1100.0)
        self.assertEqual(result["total_return_pct"], 10.0)

    def test_buy_with_negative_change_produces_loss(self):
        results = [make_backtest_result("Tesla", "BUY", -10.0)]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades"][0]["final_value"], 900.0)
        self.assertEqual(result["total_return_pct"], -10.0)

    def test_sell_with_price_decline_produces_profit(self):
        results = [make_backtest_result("Bitcoin", "SELL", -10.0)]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades"][0]["final_value"], 1100.0)
        self.assertEqual(result["trades"][0]["return_pct"], 10.0)

    def test_sell_with_price_rise_produces_loss(self):
        results = [make_backtest_result("Bitcoin", "SELL", 10.0)]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades"][0]["final_value"], 900.0)
        self.assertEqual(result["trades"][0]["return_pct"], -10.0)

    def test_aggregates_totals_across_multiple_trades(self):
        results = [
            make_backtest_result("Tesla", "BUY", 10.0),
            make_backtest_result("Apple", "BUY", -10.0),
        ]
        result = self.simulator.simulate(results)
        self.assertEqual(result["total_invested"], 2000.0)
        self.assertEqual(result["total_final_value"], 2000.0)
        self.assertEqual(result["total_return_pct"], 0.0)

    def test_skipped_outcomes_are_excluded_from_simulation(self):
        results = [
            make_backtest_result("Tesla", "BUY", 10.0),
            make_backtest_result("Apple", "BUY", None, outcome="skipped"),
        ]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades_simulated"], 1)

    def test_empty_results_returns_zeroed_summary_not_crash(self):
        result = self.simulator.simulate([])
        self.assertEqual(result["trades_simulated"], 0)
        self.assertIsNone(result["total_return_pct"])

    def test_all_skipped_returns_zeroed_summary(self):
        results = [make_backtest_result("Tesla", "BUY", None, outcome="skipped")]
        result = self.simulator.simulate(results)
        self.assertEqual(result["trades_simulated"], 0)

    def test_custom_investment_amount_is_respected(self):
        simulator = PortfolioSimulator(investment_per_trade=500.0)
        results = [make_backtest_result("Tesla", "BUY", 20.0)]
        result = simulator.simulate(results)
        self.assertEqual(result["trades"][0]["final_value"], 600.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
