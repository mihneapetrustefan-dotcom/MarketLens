"""
src/news/ingestion.py
--------------------------
The ingestion pipeline (Phase 2, spec §6, §13, §15, §22).

    PROVIDER -> RAW -> VALIDATE -> NORMALIZE -> DEDUPLICATE -> STORE -> ENTITY LINK

PROPERTIES THIS GUARANTEES:
  - IDEMPOTENT: the same article ingested five times produces one row
    (deterministic article ids + repository upsert).
  - RESUMABLE: a historical import that stops halfway resumes from its
    checkpoint rather than restarting (spec §15).
  - RECOVERABLE: every article carries a processing_status, so a
    failure leaves it at its last completed stage rather than lost.
  - OBSERVABLE: every run returns IngestionStats, logged as structured
    data containing no credentials (spec §22).

COST DISCIPLINE (spec §19): every stage here is deterministic and
cheap — parsing, hashing, string comparison, SQL. No model call
anywhere in this path. Expensive stages (real entity resolution, event
extraction) are deliberately NOT part of ingestion; they belong to
later phases and operate on already-cheaply-processed articles.
"""

import time
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Callable

from src.domain.news_models import (
    NormalizedArticle, ProcessingStatus, IngestionStats, IngestionCheckpoint,
)
from src.news.normalizer import ArticleNormalizer
from src.news.deduplication import DeduplicationEngine
from src.news.providers import NewsProvider, fetch_with_retry, ProviderError
from src.data_access.news_repository import NewsRepository

logger = logging.getLogger("marketlens.news.ingestion")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class IngestionEngine:
    """Runs the ingestion pipeline for one provider at a time."""

    def __init__(
        self,
        repository: NewsRepository,
        normalizer: Optional[ArticleNormalizer] = None,
        deduplicator: Optional[DeduplicationEngine] = None,
        store_raw: bool = True,
    ):
        """
        Args:
            repository: storage layer.
            normalizer / deduplicator: injectable for testing and for
                tuning (e.g. a stricter similarity threshold).
            store_raw: whether to persist provider payloads. Default
                True (spec §3 wants reproducibility); can be disabled
                where a provider's licence does not permit retention
                (spec §20).
        """
        self.repository = repository
        self.normalizer = normalizer or ArticleNormalizer()
        self.deduplicator = deduplicator or DeduplicationEngine()
        self.store_raw = store_raw

    def ingest_once(
        self,
        provider: NewsProvider,
        source_id_by_name: Optional[Dict[str, str]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> Dict[str, Any]:
        """
        Fetch and fully process ONE page from a provider.

        Returns:
            {"stats": IngestionStats, "next_cursor": str|None,
             "has_more": bool, "articles": [NormalizedArticle]}

        Never raises on a provider failure: the error is counted in
        stats.provider_errors and reported, so a caller running a long
        import can checkpoint and stop cleanly.
        """
        started = time.monotonic()
        stats = IngestionStats(provider=provider.name)

        if not provider.is_configured():
            logger.info("Provider '%s' not configured — skipping", provider.name)
            stats.duration_seconds = round(time.monotonic() - started, 3)
            return {"stats": stats, "next_cursor": None, "has_more": False, "articles": []}

        stats.requested = limit
        try:
            result = fetch_with_retry(
                provider, cursor=cursor, since=since, until=until, limit=limit, sleep_fn=sleep_fn
            )
        except ProviderError as exc:
            stats.provider_errors = 1
            stats.duration_seconds = round(time.monotonic() - started, 3)
            logger.error("Ingestion aborted for '%s': %s", provider.name, exc)
            return {"stats": stats, "next_cursor": cursor, "has_more": True, "articles": [], "error": str(exc)}

        stats.received = len(result.raw_articles)
        processed: List[NormalizedArticle] = []

        for raw in result.raw_articles:
            if self.store_raw:
                self.repository.save_raw(raw)

            article = self.normalizer.normalize(raw)
            if article.source_name and source_id_by_name:
                article.source_id = source_id_by_name.get(article.source_name)

            if article.processing_status == ProcessingStatus.REJECTED:
                stats.rejected += 1
                # Rejected articles are STORED, not dropped — they stay
                # inspectable and recoverable (spec §23).
                self.repository.upsert(article)
                processed.append(article)
                continue

            stats.normalized += 1

            candidates = self.repository.find_dedup_candidates(article)
            self.deduplicator.mark_if_duplicate(article, candidates)
            if article.is_duplicate:
                stats.duplicates_detected += 1
            article.processing_status = ProcessingStatus.DEDUPLICATED

            outcome = self.repository.upsert(article)
            if outcome == "updated":
                stats.updated += 1

            processed.append(article)

        stats.duration_seconds = round(time.monotonic() - started, 3)
        logger.info("Ingestion run: %s", stats.as_log_dict())
        return {
            "stats": stats,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
            "articles": processed,
        }

    def link_entities(
        self,
        articles: List[NormalizedArticle],
        company_ids_by_article: Optional[Dict[str, List[str]]] = None,
        sector_ids_by_article: Optional[Dict[str, List[str]]] = None,
        instrument_ids_by_article: Optional[Dict[str, List[str]]] = None,
    ) -> int:
        """
        Attach canonical Phase 1 entity ids to already-stored articles.

        DELIBERATELY NOT AN ENTITY RESOLVER (spec §10: "do not build
        the full Entity Resolution Engine yet"): this method does not
        DECIDE which entities an article mentions — it persists links a
        caller supplies. That keeps the storage foundation ready while
        leaving resolution itself to Phase 3.

        Returns:
            The number of articles that had at least one link written.
        """
        company_ids_by_article = company_ids_by_article or {}
        sector_ids_by_article = sector_ids_by_article or {}
        instrument_ids_by_article = instrument_ids_by_article or {}

        linked_count = 0
        for article in articles:
            wrote_any = False
            for entity_type, mapping in (
                ("company", company_ids_by_article),
                ("sector", sector_ids_by_article),
                ("instrument", instrument_ids_by_article),
            ):
                ids = mapping.get(article.article_id)
                if ids:
                    self.repository.link_entities(article.article_id, entity_type, ids)
                    wrote_any = True
            if wrote_any:
                linked_count += 1
                article.processing_status = ProcessingStatus.ENTITY_LINKED
                self.repository.upsert(article)
        return linked_count

    def run_historical_import(
        self,
        provider: NewsProvider,
        checkpoint_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        page_limit: int = 100,
        max_pages: int = 100,
        source_id_by_name: Optional[Dict[str, str]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> IngestionCheckpoint:
        """
        Run (or RESUME) a bounded historical import (spec §15).

        On every page, the checkpoint is saved BEFORE the next page is
        requested — so a crash, timeout, or provider outage at any
        point leaves a valid resume position. Calling this again with
        the same `checkpoint_id` continues from that position instead
        of restarting from zero.

        `max_pages` bounds one invocation (so a CI job cannot run
        unbounded); an import needing more pages simply resumes on the
        next invocation.

        Returns:
            The checkpoint as it stands after this invocation —
            `completed=True` only when the provider reported no more
            pages.
        """
        checkpoint = self.repository.get_checkpoint(checkpoint_id) or IngestionCheckpoint(
            checkpoint_id=checkpoint_id, provider=provider.name,
            period_start=period_start, period_end=period_end,
        )

        if checkpoint.completed:
            logger.info("Historical import '%s' already completed — nothing to do", checkpoint_id)
            return checkpoint

        if not provider.supports_historical():
            checkpoint.last_error = f"provider '{provider.name}' does not support historical fetching"
            checkpoint.last_updated_at = datetime.now(timezone.utc)
            self.repository.save_checkpoint(checkpoint)
            logger.warning("Historical import '%s': %s", checkpoint_id, checkpoint.last_error)
            return checkpoint

        pages = 0
        while pages < max_pages:
            pages += 1
            result = self.ingest_once(
                provider,
                source_id_by_name=source_id_by_name,
                limit=page_limit,
                cursor=checkpoint.cursor,
                since=checkpoint.period_start,
                until=checkpoint.period_end,
                sleep_fn=sleep_fn,
            )

            checkpoint.articles_ingested += result["stats"].normalized
            checkpoint.last_updated_at = datetime.now(timezone.utc)

            if result.get("error"):
                # Persist the resume position and stop cleanly — the
                # next invocation picks up exactly here.
                checkpoint.last_error = result["error"]
                self.repository.save_checkpoint(checkpoint)
                logger.warning("Historical import '%s' paused at cursor %s: %s",
                                checkpoint_id, checkpoint.cursor, result["error"])
                return checkpoint

            checkpoint.last_error = None
            checkpoint.cursor = result["next_cursor"]
            if not result["has_more"]:
                checkpoint.completed = True
                self.repository.save_checkpoint(checkpoint)
                logger.info("Historical import '%s' completed: %d article(s)",
                             checkpoint_id, checkpoint.articles_ingested)
                return checkpoint

            self.repository.save_checkpoint(checkpoint)

        self.repository.save_checkpoint(checkpoint)
        logger.info("Historical import '%s' paused after %d page(s) — resume to continue", checkpoint_id, pages)
        return checkpoint
