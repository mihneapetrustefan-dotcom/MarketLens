"""
src/impact/polygon_connector.py
------------------------------------
Licensed market data connector: Polygon.io (rebranded "Massive" as of
October 2025 — same API, same keys, new company name).

WHY THIS EXISTS: the Phase 6 audit flagged yfinance as an unofficial,
unsupported scraping library — acceptable for a dashboard, a real risk
for event studies whose correctness the whole quant layer depends on.
This connector is the recommended replacement path: a licensed
provider, with genuinely split/dividend-adjusted daily bars, on a free
tier that fits this project's batch (cron-scheduled, not live-trading)
usage pattern.

FREE TIER, VERIFIED AUGUST 2026: 5 requests/minute, end-of-day +
15-minute-delayed data, no credit card required. Sign up at
massive.com (Polygon.io's new home — the old domain redirects there).

WHAT THIS PRODUCES: `Candle` objects from src/impact/engine.py directly
— this connector's whole job is being a second source for the SAME
canonical shape the engine already consumes, so switching from
yfinance to this (or running both side by side) requires no change to
any calculation code.

RATE LIMITING: the free tier's 5/minute cap is enforced client-side via
the same RateLimiter already built for Phase 2's news providers
(src/news/providers.py) — reused rather than reimplemented, so there
is exactly one rate-limiting pattern in the codebase.
"""

import os
import json
import logging
from datetime import datetime, timezone, date
from urllib.request import urlopen
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional

from src.impact.engine import Candle
from src.news.providers import RateLimiter

logger = logging.getLogger("marketlens.impact.polygon_connector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

_BASE_URL = "https://api.polygon.io"


class PolygonConnector:
    """Fetches split/dividend-adjusted daily bars from Polygon.io (Massive) and returns them as Candles."""

    def __init__(self, api_key: Optional[str] = None, min_interval_seconds: float = 12.5):
        """
        Args:
            api_key: Polygon/Massive API key. Defaults to the
                POLYGON_API_KEY environment variable — never hardcode a
                real key.
            min_interval_seconds: client-side pacing between requests.
                Default 12.5s comfortably respects the free tier's
                5-calls-per-minute limit (12s would be the exact bound;
                the small margin absorbs clock jitter).
        """
        self.api_key = api_key if api_key is not None else os.environ.get("POLYGON_API_KEY")
        self._rate_limiter = RateLimiter(min_interval_seconds=min_interval_seconds)

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def fetch_daily_bars_raw(self, ticker: str, from_date: date, to_date: date) -> Any:
        """
        Perform the actual HTTP GET against Polygon's aggregates
        endpoint, requesting SPLIT/DIVIDEND-ADJUSTED bars explicitly
        (`adjusted=true`) — this is the single parameter that fixes the
        Phase 6 audit's corporate-actions concern; omitting it would
        silently return raw, unadjusted prices.

        Isolated as its own method, same pattern as every other
        network call in this project, so tests can mock it with no
        real request.
        """
        self._rate_limiter.wait()
        params = urlencode({"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": self.api_key})
        url = (f"{_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/"
               f"{from_date.isoformat()}/{to_date.isoformat()}?{params}")
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_daily_candles(self, ticker: str, from_date: date, to_date: date) -> List[Candle]:
        """
        Fetch and convert daily bars into Candles, ready for the Phase
        6 EventStudyEngine.

        Returns an EMPTY list on any failure — missing configuration,
        network error, or an unexpected response shape — never raises
        and never fabricates a bar. An empty result upstream becomes an
        INSUFFICIENT_HISTORY data-quality issue on the study, which is
        the correct, visible failure mode (spec §26).
        """
        if not self.is_configured():
            return []
        try:
            data = self.fetch_daily_bars_raw(ticker, from_date, to_date)
        except Exception as exc:  # noqa: BLE001 — one bad ticker must not break a batch
            logger.error("Polygon fetch failed for '%s': %s", ticker, exc)
            return []

        if not isinstance(data, dict) or data.get("status") not in ("OK", "DELAYED"):
            logger.warning("Polygon returned an unexpected status for '%s': %s",
                            ticker, data.get("status") if isinstance(data, dict) else type(data))
            return []

        candles = []
        for bar in data.get("results") or []:
            candle = self._bar_to_candle(bar)
            if candle:
                candles.append(candle)
        return candles

    def _bar_to_candle(self, bar: Dict[str, Any]) -> Optional[Candle]:
        """
        Convert one Polygon aggregate bar into a Candle.

        Polygon's `c` (close) field IS the adjusted close when
        `adjusted=true` was requested — so it is deliberately mapped
        onto BOTH Candle.close and Candle.adjusted_close. Setting
        adjusted_close explicitly (rather than leaving it None) is what
        makes Candle.uses_adjusted report True and Candle.price prefer
        it, matching this connector's whole purpose.
        """
        timestamp_ms = bar.get("t")
        if timestamp_ms is None:
            return None
        try:
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

        close = bar.get("c")
        return Candle(
            timestamp=timestamp,
            open_=bar.get("o"),
            high=bar.get("h"),
            low=bar.get("l"),
            close=close,
            volume=bar.get("v"),
            adjusted_close=close,   # 'c' is already split/dividend-adjusted when adjusted=true
        )

    def get_daily_candles_batch(self, tickers: List[str], from_date: date, to_date: date) -> Dict[str, List[Candle]]:
        """
        Fetch candles for several tickers. Sequential, rate-limited —
        the free tier has no batch/multi-symbol endpoint, so this is
        genuinely N requests, paced by the same limiter as every
        individual call.
        """
        result = {}
        for ticker in tickers:
            result[ticker] = self.get_daily_candles(ticker, from_date, to_date)
        return result
