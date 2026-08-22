"""
src/news/providers.py
--------------------------
News provider abstraction (Phase 2, spec §5).

RESPONSIBILITY:
Each provider owns its OWN authentication, request shape, pagination,
rate limits, error handling and response parsing — and exposes one
uniform interface to the ingestion engine. The ingestion engine never
contains `if provider == "finnhub"` logic.

WHAT THIS PHASE DELIBERATELY DOES NOT DO: the existing live collectors
(rss_collector.py, finnhub_news_collector.py,
alpha_vantage_news_collector.py) are NOT modified, rewritten, or
deleted — they keep running in the existing pipeline exactly as they
do today. `ExistingCollectorProvider` below WRAPS one of them, so the
new architecture can consume them through the uniform interface
without touching a line of their code. Migrating the pipeline to call
providers instead of collectors directly is future work, deliberately
not forced in this phase (spec §2: "do not replace existing
functionality blindly").
"""

import time
import uuid
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Callable

from src.domain.news_models import RawArticle

logger = logging.getLogger("marketlens.news.providers")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class ProviderError(Exception):
    """A provider-side failure. Distinguished from RateLimitError so retry logic can treat them differently."""


class RateLimitError(ProviderError):
    """The provider signalled we are over its rate limit. Always retried with backoff, never treated as fatal."""


class FetchResult:
    """One page of provider results, plus the cursor needed to request the next one."""

    def __init__(self, raw_articles: List[RawArticle], next_cursor: Optional[str] = None, has_more: bool = False):
        self.raw_articles = raw_articles
        self.next_cursor = next_cursor
        self.has_more = has_more


class NewsProvider(ABC):
    """
    Uniform interface every news provider must implement.

    Implementations are responsible for authentication, provider
    request/response specifics, pagination and rate limits — and must
    return RawArticle objects, never their own response shapes.
    """

    #: Short, stable provider identifier, e.g. "rss", "finnhub".
    name: str = "unknown"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider has what it needs (API key, etc.) to run. Never raises."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, cursor: Optional[str] = None, since: Optional[datetime] = None,
              until: Optional[datetime] = None, limit: int = 100) -> FetchResult:
        """
        Fetch one page of articles.

        Args:
            cursor: provider-specific continuation token from a previous
                FetchResult; None for the first page.
            since / until: optional time bounds, used by historical import.
            limit: maximum articles to return in this page.

        Raises:
            RateLimitError: provider rate limit hit (caller should back off).
            ProviderError: any other provider-side failure.
        """
        raise NotImplementedError

    def supports_historical(self) -> bool:
        """
        Whether this provider can serve a bounded historical period.
        Default False — RSS feeds, for instance, only expose a recent
        window, and claiming otherwise would silently produce
        incomplete historical datasets.
        """
        return False


class RateLimiter:
    """
    Minimal token-bucket-style pacing: guarantees at least
    `min_interval_seconds` between calls. Deliberately simple — no
    Redis, no distributed coordination (spec §14: do not over-engineer).
    """

    def __init__(self, min_interval_seconds: float = 0.0):
        self.min_interval_seconds = min_interval_seconds
        self._last_call_at: Optional[float] = None

    def wait(self, sleep_fn: Callable[[float], None] = time.sleep) -> None:
        """Block until the minimum interval has elapsed. `sleep_fn` is injectable so tests never actually sleep."""
        if self.min_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            if elapsed < self.min_interval_seconds:
                sleep_fn(self.min_interval_seconds - elapsed)
        self._last_call_at = time.monotonic()


def fetch_with_retry(
    provider: NewsProvider,
    cursor: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 100,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """
    Call provider.fetch() with exponential backoff on failure.

    Rate-limit errors and general provider errors are both retried
    (rate limits with the same backoff), because a transient provider
    outage mid-import must not lose the whole run. After
    `max_attempts`, the error is re-raised so the ingestion engine can
    checkpoint and stop cleanly rather than silently returning partial
    data as if it were complete.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return provider.fetch(cursor=cursor, since=since, until=until, limit=limit)
        except (RateLimitError, ProviderError) as exc:
            if attempt >= max_attempts:
                logger.error("Provider '%s' failed after %d attempts: %s", provider.name, attempt, exc)
                raise
            delay = backoff_seconds * (2 ** (attempt - 1))
            logger.warning("Provider '%s' attempt %d failed (%s) — retrying in %.1fs", provider.name, attempt, exc, delay)
            sleep_fn(delay)


class ExistingCollectorProvider(NewsProvider):
    """
    Adapts an EXISTING MarketLens collector (rss_collector.py,
    finnhub_news_collector.py, alpha_vantage_news_collector.py) to the
    NewsProvider interface — WITHOUT modifying that collector.

    This is what lets the new architecture consume today's working
    ingestion sources immediately, while leaving the existing pipeline
    untouched and running.
    """

    def __init__(self, name: str, collect_fn: Callable[[], List[Dict[str, Any]]],
                 is_configured_fn: Optional[Callable[[], bool]] = None):
        """
        Args:
            name: provider identifier recorded on every RawArticle.
            collect_fn: a zero-argument callable returning the
                collector's own list of article dicts (e.g.
                `RSSCollector(feeds=RSS_FEEDS).collect_all`).
            is_configured_fn: optional check (e.g. an API-key-gated
                collector's own `is_configured`). Defaults to always
                configured, which is correct for RSS.
        """
        self.name = name
        self._collect_fn = collect_fn
        self._is_configured_fn = is_configured_fn or (lambda: True)

    def is_configured(self) -> bool:
        try:
            return bool(self._is_configured_fn())
        except Exception:  # noqa: BLE001 — a broken check must not crash ingestion
            return False

    def fetch(self, cursor: Optional[str] = None, since: Optional[datetime] = None,
              until: Optional[datetime] = None, limit: int = 100) -> FetchResult:
        """
        Run the wrapped collector once and wrap its output as
        RawArticles.

        NOTE: existing collectors have no pagination concept — they
        return their current window in one call — so this always
        reports has_more=False. That is an honest reflection of what
        those collectors can actually do, not a limitation of the
        interface (a paginating provider simply returns a cursor).
        """
        try:
            records = self._collect_fn() or []
        except Exception as exc:  # noqa: BLE001 — normalize any collector failure into a provider error
            raise ProviderError(f"collector '{self.name}' failed: {exc}") from exc

        fetched_at = datetime.now(timezone.utc)
        raws = []
        for record in records[:limit]:
            raws.append(RawArticle(
                raw_id=f"raw-{uuid.uuid4().hex[:20]}",
                provider=self.name,
                provider_article_id=record.get("id") or record.get("provider_article_id"),
                fetched_at=fetched_at,
                payload=dict(record),
            ))
        return FetchResult(raw_articles=raws, next_cursor=None, has_more=False)
