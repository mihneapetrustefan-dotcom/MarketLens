"""
portfolio_simulator.py
--------------------------
Portfolio Simulator module for MarketLens.

RESPONSIBILITY:
Simulate a hypothetical portfolio built from PAST recommendations that
Backtest Engine has ALREADY checked against REAL historical prices —
never a prediction, only a "what if" calculation over outcomes that
have already played out. Turns abstract hit-rate percentages into a
concrete, easy-to-understand number: "if you'd invested $1000 in every
BUY call, you'd have $X today."

DESIGN DECISION — SELL is simulated as a short position:
A real SELL recommendation implies benefiting from a price DECLINE.
To make its simulated payoff consistent with that meaning, a SELL
trade's simulated return is the mirror image of the raw price change
(price down = simulated profit, price up = simulated loss) — the same
logic a real short position follows.

This module NEVER predicts anything — every trade it simulates already
has a real, checked, historical outcome from Backtest Engine.
"""

import logging
from typing import List, Dict, Any

logger = logging.getLogger("marketlens.portfolio_simulator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class PortfolioSimulator:
    """
    Simulates an equal-investment hypothetical portfolio from Backtest
    Engine's checked (already-resolved) recommendation outcomes.
    """

    def __init__(self, investment_per_trade: float = 1000.0):
        """
        Args:
            investment_per_trade: hypothetical fixed dollar amount
                "invested" in each checked recommendation. Purely
                illustrative — makes the aggregate result easy to
                reason about ("$1000 in every BUY call").
        """
        self.investment_per_trade = investment_per_trade

    def simulate(self, backtest_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Simulate the hypothetical portfolio from a list of Backtest
        Engine per-recommendation results (the `results` list from
        BacktestEngine.run_backtest()'s return value).

        Returns:
            {"total_invested", "total_final_value", "total_return_pct",
            "trades_simulated", "trades": [per-trade detail, ...]}
        """
        checked = [r for r in backtest_results if r.get("outcome") == "checked"]

        if not checked:
            return {
                "total_invested": 0.0,
                "total_final_value": 0.0,
                "total_return_pct": None,
                "trades_simulated": 0,
                "trades": [],
            }

        trades = []
        total_final_value = 0.0

        for r in checked:
            raw_change_pct = r["actual_change_pct"]
            effective_change_pct = raw_change_pct if r["recommendation"] == "BUY" else -raw_change_pct
            final_value = round(self.investment_per_trade * (1 + effective_change_pct / 100), 2)
            total_final_value += final_value

            trades.append({
                "entity": r.get("entity"),
                "recommendation": r["recommendation"],
                "invested": self.investment_per_trade,
                "final_value": final_value,
                "return_pct": round(effective_change_pct, 2),
            })

        total_invested = round(self.investment_per_trade * len(checked), 2)
        total_final_value = round(total_final_value, 2)
        total_return_pct = round((total_final_value - total_invested) / total_invested * 100, 2)

        logger.info(
            "Portfolio Simulator: %d trade(s) simulated, total return %.2f%%",
            len(checked), total_return_pct,
        )

        return {
            "total_invested": total_invested,
            "total_final_value": total_final_value,
            "total_return_pct": total_return_pct,
            "trades_simulated": len(checked),
            "trades": trades,
        }
