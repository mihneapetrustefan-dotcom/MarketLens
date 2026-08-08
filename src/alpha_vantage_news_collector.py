"""
alpha_vantage_news_collector.py
-----------------------------------
Alpha Vantage NEWS_SENTIMENT connector for MarketLens.

RESPONSIBILITY:
Fetch recent news via Alpha Vantage's official NEWS_SENTIMENT
endpoint, standardized into the same article schema every other
collector in this project produces. Alpha Vantage's OWN sentiment
score is deliberately NOT used here — MarketLens computes its own,
independent sentiment via Sentiment Engine, so results stay
consistent regardless of which source an article came from.

IMPORTANT — the free tier is genuinely tiny (25 requests per DAY
total, verified via Alpha Vantage's own documentation — NOT per
minute like Finnhub). To make every request count, collect_batch()
sends ALL tickers in a SINGLE call (NEWS_SENTIMENT accepts a
comma-separated `tickers` parameter) instead of one call per ticker.
Even so, this source is best used sparingly (e.g. once per day, for a
short watchlist) — flagged here explicitly rather than silently
exhausting the daily quota partway through a run.

REQUIRES AN API KEY (free, from alphavantage.co) — read from the
ALPHA_VANTAGE_API_KEY environment variable, never hardcoded.
"""

import os
import json
import logging
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.parse import urlencode
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.alpha_vantage_news_collector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class AlphaVantageNewsCollector:
    """
    Fetches and standardizes news from Alpha Vantage's NEWS_SENTIMENT
    endpoint, in a single quota-conscious call per batch.
    """

    def __init__(self, api_key: Optional[str] = None, limit: int = 50):
        """
        Args:
            api_key: Alpha Vantage API key. Defaults to the
                ALPHA_VANTAGE_API_KEY environment variable — never
                hardcode a real key.
            limit: max articles Alpha Vantage should return in one
                call (their own `limit` parameter). Default 50.
        """
        self.api_key = api_key if api_key is not None else os.environ.get("ALPHA_VANTAGE_API_KEY")
        self.limit = limit

    def is_configured(self) -> bool:
        """Whether an API key is available."""
        return bool(self.api_key)

    def fetch_raw(self, tickers_param: str) -> Any:
        """
        Perform the actual HTTP GET to Alpha Vantage's NEWS_SENTIMENT
        endpoint. Isolated as its own method — same pattern as every
        other network call in this project — so unit tests can mock
        it with no real request.
        """
        params = urlencode({
            "function": "NEWS_SENTIMENT",
            "tickers": tickers_param,
            "limit": self.limit,
            "apikey": self.api_key,
        })
        url = f"https://www.alphavantage.co/query?{params}"
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def collect_batch(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch news for a list of tickers in ONE API call, to conserve
        the tight 25-request/day free quota.

        Returns:
            A list of standardized article dicts. NEVER raises —
            returns [] on any failure, missing configuration, empty
            ticker list, or exhausted quota.
        """
        if not self.is_configured() or not tickers:
            return []

        tickers_param = ",".join(tickers)

        try:
            data = self.fetch_raw(tickers_param)
        except Exception as exc:  # noqa: BLE001 — a connector failure must never break the pipeline
            logger.error("Alpha Vantage fetch failed: %s", exc)
            return []

        if not isinstance(data, dict) or "feed" not in data:
            # A common failure mode: Alpha Vantage returns HTTP 200
            # with a plain "Information"/"Note" field instead of real
            # data when the daily quota is exhausted or the key is
            # invalid — NOT an HTTP error code, so this must be
            # checked explicitly rather than relying on an exception.
            logger.warning(
                "Alpha Vantage response had no 'feed' field (likely quota exhausted or invalid key): %s",
                str(data)[:200],
            )
            return []

        articles = []
        for item in data["feed"]:
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue

            published_at = None
            raw_time = item.get("time_published")  # format: YYYYMMDDTHHMMSS
            if raw_time:
                try:
                    published_at = datetime.strptime(raw_time, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    published_at = None

            articles.append({
                "article_id": url,
                "title": title,
                "summary": item.get("summary", ""),
                "url": url,
                "source": item.get("source", "Alpha Vantage"),
                "category": "stocks",
                "published_at": published_at,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })

        logger.info("Alpha Vantage: collected %d article(s) for %d ticker(s) in 1 call", len(articles), len(tickers))
        return articles
