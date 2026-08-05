"""
market_data.py
-----------------
Market Data module for MarketLens.

RESPONSIBILITY:
Fetch REAL, factual market data for a ticker — current price, daily
change, 52-week high/low, trailing P/E — via `yfinance` (already used
by Backtest Engine, no new dependency).

IMPORTANT — WHAT THIS MODULE DELIBERATELY DOES NOT DO:
It never declares a stock "undervalued" or "overvalued", and never
predicts a future price. Doing so honestly would require real
fundamental analysis (earnings trends, peer comparison, growth
forecasts) that this platform does not perform. This module supplies
FACTS ONLY — e.g. "the current price is 3% below its 52-week high" —
and leaves any judgment about what that means to the person reading
the report. This mirrors the same discipline already established by
Backtest Engine (verify outcomes, never predict magnitudes).

DEPENDENCY NOTE: `yfinance` is imported INSIDE fetch_snapshot(), not at
module level — importing this module or running its test suite never
requires yfinance to be installed; it's only needed when actually
fetching real data in Colab.
"""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.market_data")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def normalize_ticker_for_yfinance(ticker: str, category: str) -> Optional[str]:
    """
    Convert one of MarketLens' internal ticker symbols into the exact
    symbol yfinance expects for that asset type.

    WHY THIS EXISTS — a real data-quality bug found by inspecting
    actual output: bare crypto symbols like "ETH" or "XRP" do NOT
    reliably resolve to the intended cryptocurrency on Yahoo Finance —
    "ETH" returned real-looking but WRONG data for some unrelated
    instrument instead of Ethereum (a silent wrong-answer, worse than a
    visible error). Crypto pairs need an explicit "-USD" suffix (e.g.
    "ETH-USD") to resolve correctly.

    BVB (Romanian exchange) tickers are NOT reliably available via
    yfinance's free data, as far as this project has verified — this
    returns None for category "bvb" so callers can skip fetching
    rather than display blank or (worse) misleading rows. If BVB
    coverage is confirmed working with a different suffix in the
    future, this is the one place to update it.

    Args:
        ticker: the bare ticker symbol as stored in MarketLens'
            registries (e.g. "ETH", "AAPL", "BRD").
        category: "stocks", "etf", "forex", "crypto", or "bvb".

    Returns:
        The symbol to actually query yfinance with, or None if this
        category isn't reliably queryable at all (caller should skip).
    """
    if category == "crypto":
        return f"{ticker}-USD"
    if category == "bvb":
        return None
    return ticker


class MarketDataFetcher:
    """
    Fetches and standardizes real market data snapshots for tickers.
    """

    def fetch_snapshot(self, ticker: str) -> Dict[str, Any]:
        """
        Fetch the raw info dict for one ticker from yfinance.

        Isolated as its own method — exactly like every collector's
        fetch_* method in this project — so unit tests can mock it
        with no real network call, and so `yfinance` only needs to be
        installed when this method actually runs.
        """
        import yfinance as yf
        return yf.Ticker(ticker).info

    def _safe_get(self, info: Dict[str, Any], *keys: str) -> Optional[float]:
        """
        Return the first present, non-None value among several
        possible key names. WHY NEEDED: yfinance's `.info` dict is
        notoriously inconsistent across tickers/regions — the current
        price might appear under "currentPrice", "regularMarketPrice",
        or occasionally something else entirely, depending on the
        exchange. Trying a list of known aliases in order is more
        robust than depending on any single key always being present.
        """
        for key in keys:
            value = info.get(key)
            if value is not None:
                return value
        return None

    def get_snapshot(self, ticker: str) -> Dict[str, Any]:
        """
        Fetch and standardize a market data snapshot for one ticker.

        Returns a dict with:
            ticker, current_price, previous_close, daily_change_pct,
            fifty_two_week_high, fifty_two_week_low,
            pct_from_52w_high, pct_from_52w_low, trailing_pe,
            market_cap, currency, fetched_at, error (only present on failure)

        NEVER raises — a ticker with missing or unavailable data
        returns a dict with an `error` key explaining why, and every
        other field set to None, so a single bad ticker never breaks a
        batch of many.
        """
        fetched_at = datetime.now(timezone.utc).isoformat()

        try:
            info = self.fetch_snapshot(ticker)
        except Exception as exc:  # noqa: BLE001 — never let one bad ticker halt a batch
            logger.error("Failed to fetch market data for '%s': %s", ticker, exc)
            return {
                "ticker": ticker, "current_price": None, "previous_close": None,
                "daily_change_pct": None, "fifty_two_week_high": None,
                "fifty_two_week_low": None, "pct_from_52w_high": None,
                "pct_from_52w_low": None, "trailing_pe": None, "market_cap": None,
                "currency": None, "fetched_at": fetched_at,
                "error": f"Market data unavailable: {exc}",
            }

        if not info:
            return {
                "ticker": ticker, "current_price": None, "previous_close": None,
                "daily_change_pct": None, "fifty_two_week_high": None,
                "fifty_two_week_low": None, "pct_from_52w_high": None,
                "pct_from_52w_low": None, "trailing_pe": None, "market_cap": None,
                "currency": None, "fetched_at": fetched_at,
                "error": "No data returned for this ticker",
            }

        current_price = self._safe_get(info, "currentPrice", "regularMarketPrice")
        previous_close = self._safe_get(info, "previousClose", "regularMarketPreviousClose")
        fifty_two_week_high = self._safe_get(info, "fiftyTwoWeekHigh")
        fifty_two_week_low = self._safe_get(info, "fiftyTwoWeekLow")

        daily_change_pct = None
        if current_price is not None and previous_close:
            daily_change_pct = round((current_price - previous_close) / previous_close * 100, 2)

        pct_from_52w_high = None
        if current_price is not None and fifty_two_week_high:
            pct_from_52w_high = round((current_price - fifty_two_week_high) / fifty_two_week_high * 100, 2)

        pct_from_52w_low = None
        if current_price is not None and fifty_two_week_low:
            pct_from_52w_low = round((current_price - fifty_two_week_low) / fifty_two_week_low * 100, 2)

        return {
            "ticker": ticker,
            "current_price": current_price,
            "previous_close": previous_close,
            "daily_change_pct": daily_change_pct,
            "fifty_two_week_high": fifty_two_week_high,
            "fifty_two_week_low": fifty_two_week_low,
            "pct_from_52w_high": pct_from_52w_high,
            "pct_from_52w_low": pct_from_52w_low,
            "trailing_pe": self._safe_get(info, "trailingPE"),
            "market_cap": self._safe_get(info, "marketCap"),
            "currency": info.get("currency"),
            "fetched_at": fetched_at,
        }

    def get_snapshots_batch(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch snapshots for a whole list of tickers.

        Returns:
            A dict mapping ticker -> snapshot. A ticker that fails
            still appears in the result, with its `error` field set —
            it is never silently dropped, and never stops the rest of
            the batch from being fetched.
        """
        snapshots = {ticker: self.get_snapshot(ticker) for ticker in tickers}

        succeeded = sum(1 for s in snapshots.values() if "error" not in s)
        logger.info(
            "Market Data: %d/%d ticker snapshot(s) fetched successfully",
            succeeded, len(tickers),
        )
        return snapshots

    def fetch_price_history_raw(self, ticker: str, days: int = 30):
        """
        Fetch raw daily price history for a ticker via yfinance.

        Isolated as its own method — same pattern as fetch_snapshot()
        and every other network call in this project — so unit tests
        can mock it with no real network call.
        """
        import yfinance as yf
        return yf.Ticker(ticker).history(period=f"{days}d")

    def get_price_history(self, ticker: str, days: int = 30) -> List[Dict[str, Any]]:
        """
        Fetch a simple daily closing-price series for one ticker — real
        historical closes only, no smoothing/interpolation, meant for
        charting a price line on the Dashboard.

        Returns:
            A list of {"date": "YYYY-MM-DD", "close": float}, oldest
            first. Returns an empty list on any failure or if no data
            is available — NEVER raises, so a chart with no data just
            renders empty rather than breaking the whole report.
        """
        try:
            history = self.fetch_price_history_raw(ticker, days)
        except Exception as exc:  # noqa: BLE001 — never let one bad ticker break the report
            logger.error("Failed to fetch price history for '%s': %s", ticker, exc)
            return []

        if history is None or history.empty or "Close" not in history.columns:
            return []

        series = []
        for index_value, close_value in zip(history.index, history["Close"]):
            date_str = str(index_value.date()) if hasattr(index_value, "date") else str(index_value)
            series.append({"date": date_str, "close": round(float(close_value), 2)})
        return series

    def get_price_history_batch(self, tickers: List[str], days: int = 30) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch price history series for a whole list of tickers.

        Returns:
            A dict mapping ticker -> its price history list (possibly
            empty for a ticker that failed — never dropped, never
            stops the rest of the batch).
        """
        histories = {ticker: self.get_price_history(ticker, days) for ticker in tickers}
        succeeded = sum(1 for h in histories.values() if h)
        logger.info(
            "Market Data: %d/%d ticker price history series fetched successfully",
            succeeded, len(tickers),
        )
        return histories
