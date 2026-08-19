"""
backtest_engine.py
---------------------
Backtest Engine module for MarketLens.

RESPONSIBILITY:
Check whether past BUY/SELL recommendations were actually followed by
the price moving in the predicted direction, using REAL historical
price data (via the `yfinance` library — free, no API key required).
This turns "the system said BUY" into "here's whether that call was
actually right", grounding MarketLens in verifiable outcomes instead
of leaving its output as an unverified opinion.

IMPORTANT — WHAT THIS MODULE DELIBERATELY DOES NOT DO:
It never predicts a future price, and never claims a stock is
under/overvalued. It only checks, AFTER THE FACT, whether a
DIRECTIONAL call (BUY = price should rise, SELL = price should fall)
was correct over a chosen holding period, using real closing prices.
Any feature that predicts a magnitude or a valuation verdict would
need real fundamental data (earnings, P/E, analyst targets) that this
platform does not yet collect — this module is intentionally scoped
to what can be verified honestly with what MarketLens has today.

CHANGE LOG (v1.1) — WHY THE HOLDING PERIOD IS NOW PER-HORIZON:
Previously every recommendation was checked after the SAME fixed
holding period (5 days), regardless of whether it was tagged
short-term or long-term. That meant a "long-term BUY" — a thesis that
was never meant to play out in under a week — got marked "wrong"
whenever the price simply dipped on short-term noise, well before its
actual timeframe had elapsed. The hit rate this produced reflected
short-term volatility, not whether the underlying call was sound.
Each recommendation is now checked after ITS OWN declared
time_horizon's appropriate holding period instead.

DEPENDENCY NOTE: `yfinance` (and its dependency `pandas`) is imported
INSIDE fetch_price_history(), not at module level, so importing this
module — and running its unit tests — never requires yfinance to be
installed.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.backtest_engine")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class BacktestEngine:
    """
    Checks past recommendations against real historical price
    movement, and aggregates the results into a hit-rate summary.
    """

    # Default holding period per declared time_horizon — how long to
    # wait before a recommendation with that horizon is fairly judged.
    # A "long-term" call gets ~45 days to play out; a "short-term" one
    # only needs ~5 (roughly a trading week). Overridable per instance.
    DEFAULT_HOLDING_PERIOD_BY_HORIZON: Dict[str, int] = {
        "short-term": 5,
        "mixed": 15,
        "long-term": 45,
    }

    def __init__(
        self,
        holding_period_days: int = 5,
        holding_period_days_by_horizon: Optional[Dict[str, int]] = None,
    ):
        """
        Args:
            holding_period_days: FALLBACK number of days to wait
                before checking a recommendation, used when its
                time_horizon is missing (older, pre-migration log
                rows) or not present in
                holding_period_days_by_horizon. Also the value used if
                a recommendation was never tagged with a horizon at
                all. Default 5 — unchanged from the original behavior,
                so anything without a recorded horizon is judged
                exactly as before.
            holding_period_days_by_horizon: per-horizon override, e.g.
                {"short-term": 5, "mixed": 15, "long-term": 45}.
                Defaults to DEFAULT_HOLDING_PERIOD_BY_HORIZON. This is
                what lets a "long-term" BUY be judged on a long-term
                clock instead of the same short window as everything
                else.
        """
        self.holding_period_days = holding_period_days
        self.holding_period_days_by_horizon = (
            dict(holding_period_days_by_horizon)
            if holding_period_days_by_horizon is not None
            else dict(self.DEFAULT_HOLDING_PERIOD_BY_HORIZON)
        )

    def _holding_period_for(self, recommendation: Dict[str, Any]) -> int:
        """Resolve the holding period to use for one recommendation, based on its own time_horizon."""
        horizon = recommendation.get("time_horizon")
        return self.holding_period_days_by_horizon.get(horizon, self.holding_period_days)

    def fetch_price_history(self, ticker: str, start_date, end_date):
        """
        Fetch historical daily closing prices for a ticker between two
        dates, via yfinance.

        Isolated as its own method — exactly like every collector's
        fetch_* method in this project — so unit tests can mock it
        with no real network call, and so `yfinance` only needs to be
        installed when this method actually runs, not to import the
        module or run its test suite.
        """
        import yfinance as yf
        return yf.Ticker(ticker).history(start=start_date, end=end_date)

    def _get_closing_price(self, price_history, target_date) -> Optional[float]:
        """
        Get the closing price on or after `target_date` from a price
        history DataFrame. Markets are closed on weekends/holidays, so
        the exact calendar date requested may have no trading data —
        this takes the first available trading day on or after it.

        Handles a timezone mismatch that real price data commonly
        triggers: `target_date` is timezone-AWARE (from a recommendation's
        `generated_at`), but pandas DatetimeIndex data from yfinance
        may be timezone-NAIVE (or vice versa) — comparing the two
        directly raises a TypeError, so timezone-awareness is aligned
        before comparing, without changing either timestamp's actual
        moment in time.
        """
        if price_history is None or price_history.empty:
            return None

        index_is_tz_aware = price_history.index.tz is not None
        target_is_tz_aware = target_date.tzinfo is not None

        if index_is_tz_aware and not target_is_tz_aware:
            target_date = target_date.replace(tzinfo=timezone.utc)
        elif not index_is_tz_aware and target_is_tz_aware:
            target_date = target_date.replace(tzinfo=None)

        available = price_history[price_history.index >= target_date]
        if available.empty:
            return None
        return float(available.iloc[0]["Close"])

    def check_recommendation(self, recommendation: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check ONE past recommendation against real price history,
        using the holding period appropriate to ITS OWN time_horizon
        (see _holding_period_for).

        Args:
            recommendation: a dict with at least "entity", "ticker",
                "recommendation" (BUY/SELL), and "generated_at" (ISO
                timestamp) — the shape produced by
                RecommendationLog.load_actionable_due_for_check() (or
                the older load_actionable_before()). "time_horizon" is
                used if present, else the instance's fallback default
                applies.

        Returns:
            The input dict, extended with either:
            - `outcome: "checked"`, plus `entry_price`, `exit_price`,
              `actual_change_pct`, `was_correct`,
              `holding_period_days_used`; or
            - `outcome: "skipped"`, plus `skipped_reason` explaining
              why (never raises — a single unbacktestable
              recommendation must never stop the whole batch).
        """
        ticker = recommendation.get("ticker")
        rec_type = recommendation.get("recommendation")

        if not ticker:
            return {**recommendation, "outcome": "skipped", "skipped_reason": "No known ticker for this entity"}

        if rec_type not in ("BUY", "SELL"):
            return {**recommendation, "outcome": "skipped", "skipped_reason": "Only BUY/SELL recommendations can be backtested"}

        try:
            generated_at = datetime.fromisoformat(str(recommendation["generated_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            return {**recommendation, "outcome": "skipped", "skipped_reason": "Invalid or missing generated_at timestamp"}

        holding_period_days = self._holding_period_for(recommendation)
        exit_date = generated_at + timedelta(days=holding_period_days)
        if exit_date > datetime.now(timezone.utc):
            return {**recommendation, "outcome": "skipped", "skipped_reason": "Holding period has not fully elapsed yet"}

        try:
            # A few extra days of buffer at the end, in case the exact
            # exit date falls on a weekend/holiday with no trading.
            history = self.fetch_price_history(
                ticker, generated_at.date(), (exit_date + timedelta(days=4)).date()
            )
        except Exception as exc:  # noqa: BLE001 — never let one bad ticker halt the batch
            logger.error("Failed to fetch price history for '%s': %s", ticker, exc)
            return {**recommendation, "outcome": "skipped", "skipped_reason": f"Price data unavailable: {exc}"}

        entry_price = self._get_closing_price(history, generated_at)
        exit_price = self._get_closing_price(history, exit_date)

        if entry_price is None or exit_price is None:
            return {**recommendation, "outcome": "skipped", "skipped_reason": "Could not find matching trading days in price history"}

        actual_change_pct = round((exit_price - entry_price) / entry_price * 100, 2)
        was_correct = actual_change_pct > 0 if rec_type == "BUY" else actual_change_pct < 0

        return {
            **recommendation,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "actual_change_pct": actual_change_pct,
            "was_correct": was_correct,
            "holding_period_days_used": holding_period_days,
            "outcome": "checked",
        }

    def run_backtest(self, recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Check a whole batch of past recommendations and compute an
        aggregate hit-rate summary.

        Returns:
            {"results": [...per-recommendation outcomes...],
             "summary": {total, checked, skipped, correct, hit_rate,
                         average_change_pct}}
        """
        results = [self.check_recommendation(r) for r in recommendations]
        checked = [r for r in results if r["outcome"] == "checked"]
        correct = [r for r in checked if r["was_correct"]]

        summary = {
            "total_recommendations": len(recommendations),
            "checked": len(checked),
            "skipped": len(recommendations) - len(checked),
            "correct": len(correct),
            "hit_rate": round(len(correct) / len(checked), 3) if checked else None,
            "average_change_pct": (
                round(sum(r["actual_change_pct"] for r in checked) / len(checked), 2) if checked else None
            ),
        }

        logger.info(
            "Backtest: %d/%d recommendations checked, hit rate = %s",
            summary["checked"], summary["total_recommendations"], summary["hit_rate"],
        )

        return {"results": results, "summary": summary}
