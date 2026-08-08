"""
finnhub_news_collector.py
-----------------------------
Finnhub Company News connector for MarketLens.

RESPONSIBILITY:
Fetch recent news for specific tracked companies from Finnhub's
official company-news endpoint and standardize results into the same
article schema every other collector in this project produces — so
they merge seamlessly into the same downstream processing pipeline
(News Cleaner, Company Detector, Sentiment Engine, etc.).

WHY THIS SOURCE: unlike RSS feeds (which only show an outlet's most
recent items) or Google News search (an unofficial, undocumented
endpoint), this is an OFFICIAL, documented, per-company news API.
Finnhub's free tier (60 calls/minute, no credit card, verified via
their own documentation) is generous enough for daily use across a
meaningful watchlist.

REQUIRES AN API KEY (free, from finnhub.io) — read from the
FINNHUB_API_KEY environment variable, never hardcoded. If no key is
configured, this collector cleanly returns no articles rather than
raising, so its absence never breaks the rest of the pipeline — the
exact same resilience pattern already used by every other optional
integration in this project (Email Notifier, etc.).
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen
from urllib.parse import urlencode
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.finnhub_news_collector")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class FinnhubNewsCollector:
    """
    Fetches and standardizes per-company news from Finnhub.
    """

    def __init__(self, api_key: Optional[str] = None, days_back: int = 7):
        """
        Args:
            api_key: Finnhub API key. Defaults to the FINNHUB_API_KEY
                environment variable — never hardcode a real key.
            days_back: how many days of history to request per ticker
                on each call. Default 7 (Finnhub's company-news
                endpoint requires an explicit from/to date range).
        """
        self.api_key = api_key if api_key is not None else os.environ.get("FINNHUB_API_KEY")
        self.days_back = days_back

    def is_configured(self) -> bool:
        """Whether an API key is available."""
        return bool(self.api_key)

    def fetch_raw(self, ticker: str, date_from: str, date_to: str) -> Any:
        """
        Perform the actual HTTP GET to Finnhub's company-news endpoint.
        Isolated as its own method — same pattern as every other
        network call in this project — so unit tests can mock it with
        no real request.
        """
        params = urlencode({"symbol": ticker, "from": date_from, "to": date_to, "token": self.api_key})
        url = f"https://finnhub.io/api/v1/company-news?{params}"
        with urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def collect_for_ticker(self, ticker: str) -> List[Dict[str, Any]]:
        """
        Fetch and standardize recent news for one ticker.

        Returns:
            A list of standardized article dicts. NEVER raises —
            returns [] on any failure (missing configuration, network
            error, unexpected response shape) — a single bad ticker or
            a temporary outage must never break the rest of the batch.
        """
        if not self.is_configured():
            return []

        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=self.days_back)).isoformat()
        date_to = today.isoformat()

        try:
            raw_items = self.fetch_raw(ticker, date_from, date_to)
        except Exception as exc:  # noqa: BLE001 — never let one bad ticker halt a batch
            logger.error("Finnhub fetch failed for '%s': %s", ticker, exc)
            return []

        if not isinstance(raw_items, list):
            logger.warning("Finnhub returned an unexpected response shape for '%s'", ticker)
            return []

        articles = []
        for item in raw_items:
            headline = item.get("headline")
            url = item.get("url")
            if not headline or not url:
                continue

            published_at = None
            if item.get("datetime"):
                try:
                    published_at = datetime.fromtimestamp(item["datetime"], tz=timezone.utc).isoformat()
                except (ValueError, OSError, OverflowError):
                    published_at = None

            articles.append({
                "article_id": str(item.get("id", url)),
                "title": headline,
                "summary": item.get("summary", ""),
                "url": url,
                "source": item.get("source", "Finnhub"),
                "category": "stocks",
                "published_at": published_at,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })
        return articles

    def collect_batch(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch news for a whole list of tickers, one call per ticker.
        A failing ticker is skipped (handled inside
        collect_for_ticker) — never blocks the rest of the batch.
        """
        all_articles: List[Dict[str, Any]] = []
        for ticker in tickers:
            all_articles.extend(self.collect_for_ticker(ticker))

        logger.info("Finnhub: collected %d article(s) across %d ticker(s)", len(all_articles), len(tickers))
        return all_articles
