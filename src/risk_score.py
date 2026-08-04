"""
risk_score.py
----------------
Risk Score module for MarketLens.

RESPONSIBILITY:
Compute a REAL, historical volatility-based risk score per ticker, via
`yfinance` (already used by Backtest Engine and Market Data — no new
dependency). Quantifies how much a stock's price has actually swung
recently — this is a MEASUREMENT of past volatility, never a
prediction of future risk.

METHOD: standard deviation of daily returns over a lookback window,
annualized via the standard sqrt(252 trading days) convention — a
well-established, textbook volatility measure, not a custom invention.

DEPENDENCY NOTE: `yfinance` is imported INSIDE fetch_price_history(),
not at module level — consistent with every other yfinance-using
module in this project (Backtest Engine, Market Data).
"""

import math
import logging
from typing import List, Dict, Any

logger = logging.getLogger("marketlens.risk_score")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class RiskScoreCalculator:
    """
    Computes annualized historical volatility and a Low/Medium/High
    risk label per ticker.
    """

    # Annualized volatility (%) thresholds for the risk label. These
    # are common rough industry conventions (large, stable stocks
    # typically run under ~20%; small-caps/growth/crypto-adjacent
    # names often exceed 40%) — not a precise scientific cutoff, just
    # a reasonable, documented default.
    _LOW_RISK_MAX = 20.0
    _MEDIUM_RISK_MAX = 40.0

    def __init__(self, lookback_days: int = 30):
        """
        Args:
            lookback_days: how many recent calendar days of price
                history to use for the volatility calculation. 30 days
                balances responsiveness (recent behavior) against
                having enough data points to be statistically
                meaningful.
        """
        self.lookback_days = lookback_days

    def fetch_price_history(self, ticker: str):
        """
        Fetch recent daily price history for a ticker via yfinance.

        Isolated as its own method — exactly like Backtest Engine and
        Market Data's fetch_* methods — so unit tests can mock it with
        no real network call.
        """
        import yfinance as yf
        return yf.Ticker(ticker).history(period=f"{self.lookback_days}d")

    def get_risk_score(self, ticker: str) -> Dict[str, Any]:
        """
        Compute the risk score for one ticker.

        Returns:
            {"ticker", "annualized_volatility_pct", "risk_level"} on
            success, or the same shape with both value fields as None
            and an "error" key on failure — NEVER raises.
        """
        try:
            history = self.fetch_price_history(ticker)
        except Exception as exc:  # noqa: BLE001 — never let one bad ticker halt a batch
            logger.error("Failed to fetch price history for risk score '%s': %s", ticker, exc)
            return {
                "ticker": ticker, "annualized_volatility_pct": None, "risk_level": None,
                "error": f"Price data unavailable: {exc}",
            }

        if history is None or history.empty or len(history) < 2:
            return {
                "ticker": ticker, "annualized_volatility_pct": None, "risk_level": None,
                "error": "Not enough price history to compute volatility",
            }

        closes = history["Close"].tolist()
        daily_returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] != 0
        ]
        if len(daily_returns) < 2:
            return {
                "ticker": ticker, "annualized_volatility_pct": None, "risk_level": None,
                "error": "Not enough return observations to compute volatility",
            }

        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        daily_std = math.sqrt(variance)
        annualized_volatility_pct = round(daily_std * math.sqrt(252) * 100, 2)

        if annualized_volatility_pct <= self._LOW_RISK_MAX:
            risk_level = "Low"
        elif annualized_volatility_pct <= self._MEDIUM_RISK_MAX:
            risk_level = "Medium"
        else:
            risk_level = "High"

        return {
            "ticker": ticker,
            "annualized_volatility_pct": annualized_volatility_pct,
            "risk_level": risk_level,
        }

    def get_risk_scores_batch(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Compute risk scores for a whole list of tickers.

        Returns:
            A dict mapping ticker -> risk score dict. A failing ticker
            still appears, with its `error` field set, and never stops
            the rest of the batch.
        """
        scores = {ticker: self.get_risk_score(ticker) for ticker in tickers}

        succeeded = sum(1 for s in scores.values() if "error" not in s)
        logger.info("Risk Score: %d/%d ticker(s) scored successfully", succeeded, len(tickers))
        return scores
