"""
src/data_access/news_repository.py
---------------------------------------
Internal Data Access Layer for the Phase 2 news tables, and the
internal News API the application queries through (spec §17).

RESPONSIBILITY:
The only code that writes SQL against news_articles / raw_articles /
article_entities / ingestion_checkpoints. Provides:

  - idempotent upsert (spec §6: the same article arriving five times
    must not create five rows)
  - article-update detection (spec §8: an update is a new VERSION of an
    existing article, not an unrelated new article)
  - a bounded dedup-candidate query (so the dedup engine never scans
    the whole table)
  - cursor pagination (spec §18: the UI must stay responsive at
    millions of rows — never load everything into memory)
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any

from src.domain.news_models import (
    RawArticle, NormalizedArticle, ProcessingStatus, DuplicateMatchLevel, IngestionCheckpoint,
)

logger = logging.getLogger("marketlens.news.repository")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except Exception:  # noqa: BLE001 — a malformed stored number must not break a whole query
        return None


class NewsRepository:
    """Storage + query access for normalized articles, raw payloads, entity links, and ingestion checkpoints."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    # ---------------- raw articles ----------------

    def save_raw(self, raw: RawArticle) -> None:
        """Persist the provider's own response, keyed by raw_id. Idempotent."""
        self._conn.execute(
            "INSERT OR REPLACE INTO raw_articles (raw_id, provider, provider_article_id, fetched_at, payload_json) VALUES (?, ?, ?, ?, ?)",
            (raw.raw_id, raw.provider, raw.provider_article_id, _iso(raw.fetched_at), json.dumps(raw.payload or {})),
        )
        self._conn.commit()

    def get_raw(self, raw_id: str) -> Optional[RawArticle]:
        row = self._conn.execute("SELECT * FROM raw_articles WHERE raw_id = ?", (raw_id,)).fetchone()
        if not row:
            return None
        return RawArticle(
            raw_id=row["raw_id"], provider=row["provider"],
            provider_article_id=row["provider_article_id"],
            fetched_at=_parse_iso(row["fetched_at"]),
            payload=json.loads(row["payload_json"]),
        )

    # ---------------- normalized articles ----------------

    def _row_to_article(self, row: sqlite3.Row) -> NormalizedArticle:
        return NormalizedArticle(
            article_id=row["article_id"], provider=row["provider"],
            provider_article_id=row["provider_article_id"], raw_id=row["raw_id"],
            source_id=row["source_id"], source_name=row["source_name"],
            source_url=row["source_url"], canonical_url=row["canonical_url"],
            title=row["title"], summary=row["summary"],
            language=row["language"], country=row["country"], author=row["author"],
            categories=json.loads(row["categories_json"] or "[]"),
            published_at=_parse_iso(row["published_at"]),
            ingested_at=_parse_iso(row["ingested_at"]),
            updated_at=_parse_iso(row["updated_at"]),
            fingerprint=row["fingerprint"], content_fingerprint=row["content_fingerprint"],
            duplicate_of=row["duplicate_of"],
            duplicate_match_level=DuplicateMatchLevel(row["duplicate_match_level"]),
            sentiment_label=row["sentiment_label"],
            sentiment_score=_decimal(row["sentiment_score"]),
            impact_score=_decimal(row["impact_score"]),
            processing_status=ProcessingStatus(row["processing_status"]),
            rejection_reason=row["rejection_reason"], version=row["version"],
        )

    def get(self, article_id: str) -> Optional[NormalizedArticle]:
        row = self._conn.execute("SELECT * FROM news_articles WHERE article_id = ?", (article_id,)).fetchone()
        return self._row_to_article(row) if row else None

    def upsert(self, article: NormalizedArticle) -> str:
        """
        Insert an article, or update it if it already exists.

        Returns:
            "inserted"  — genuinely new
            "updated"   — existed AND the provider's content changed:
                          version is bumped and updated_at set (spec §8)
            "unchanged" — existed with identical content: nothing is
                          written, which is what makes re-ingestion of
                          the same article a true no-op (spec §6)
        """
        existing = self.get(article.article_id)

        if existing is None:
            self._write(article)
            return "inserted"

        content_changed = (
            existing.title != article.title
            or existing.summary != article.summary
            or existing.canonical_url != article.canonical_url
        )
        if not content_changed:
            return "unchanged"

        article.version = existing.version + 1
        article.updated_at = article.ingested_at or datetime.now(timezone.utc)
        # Preserve the ORIGINAL publication and ingestion times — an
        # update must never overwrite when we first knew about the
        # story, or look-ahead-bias protection breaks (spec §9).
        article.published_at = existing.published_at or article.published_at
        article.ingested_at = existing.ingested_at or article.ingested_at
        self._write(article)
        return "updated"

    def _write(self, article: NormalizedArticle) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO news_articles (
                article_id, provider, provider_article_id, raw_id, source_id, source_name,
                source_url, canonical_url, title, summary, language, country, author,
                categories_json, published_at, ingested_at, updated_at, fingerprint,
                content_fingerprint, duplicate_of, duplicate_match_level, sentiment_label,
                sentiment_score, impact_score, processing_status, rejection_reason, version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            article.article_id, article.provider, article.provider_article_id, article.raw_id,
            article.source_id, article.source_name, article.source_url, article.canonical_url,
            article.title, article.summary, article.language, article.country, article.author,
            json.dumps(article.categories), _iso(article.published_at), _iso(article.ingested_at),
            _iso(article.updated_at), article.fingerprint, article.content_fingerprint,
            article.duplicate_of, article.duplicate_match_level.value, article.sentiment_label,
            str(article.sentiment_score) if article.sentiment_score is not None else None,
            str(article.impact_score) if article.impact_score is not None else None,
            article.processing_status.value, article.rejection_reason, article.version,
        ))
        self._conn.commit()

    def find_dedup_candidates(self, article: NormalizedArticle, window_days: int = 1) -> List[NormalizedArticle]:
        """
        Return a BOUNDED candidate set for deduplication — only
        articles published within +/- `window_days` of this one.

        This bound is what keeps deduplication tractable at scale: the
        dedup engine never scans the full table, only one day's
        neighbourhood, which is where genuine duplicates live anyway
        (a story syndicated a month later is a different story).
        """
        if not article.published_at:
            return []
        start = article.published_at - timedelta(days=window_days)
        end = article.published_at + timedelta(days=window_days)
        rows = self._conn.execute(
            "SELECT * FROM news_articles WHERE published_at BETWEEN ? AND ? AND article_id != ?",
            (_iso(start), _iso(end), article.article_id),
        ).fetchall()
        return [self._row_to_article(r) for r in rows]

    # ---------------- entity links ----------------

    def link_entities(self, article_id: str, entity_type: str, entity_ids: List[str]) -> None:
        """Link an article to canonical Phase 1 entity ids. Idempotent (primary key absorbs repeats)."""
        for entity_id in entity_ids:
            self._conn.execute(
                "INSERT OR IGNORE INTO article_entities (article_id, entity_type, entity_id) VALUES (?, ?, ?)",
                (article_id, entity_type, entity_id),
            )
        self._conn.commit()

    def get_entity_ids(self, article_id: str, entity_type: str) -> List[str]:
        rows = self._conn.execute(
            "SELECT entity_id FROM article_entities WHERE article_id = ? AND entity_type = ?",
            (article_id, entity_type),
        ).fetchall()
        return [r["entity_id"] for r in rows]

    # ---------------- internal news API (spec §17) ----------------

    def query(
        self,
        source_name: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        published_after: Optional[datetime] = None,
        published_before: Optional[datetime] = None,
        search: Optional[str] = None,
        include_duplicates: bool = False,
        include_rejected: bool = False,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        The internal news query API. Every filter is applied
        SERVER-SIDE and results are cursor-paginated — the caller never
        receives an unbounded result set (spec §18).

        Args:
            cursor: an opaque pagination cursor returned by a previous
                call (the published_at of the last row seen). Cursor
                pagination is used rather than OFFSET because OFFSET
                degrades linearly as the dataset grows.
            limit: hard-capped at 500 regardless of what is requested.

        Returns:
            {"articles": [...], "next_cursor": str|None, "has_more": bool}
        """
        limit = max(1, min(limit, 500))

        where = []
        params: List[Any] = []

        if not include_duplicates:
            where.append("a.duplicate_of IS NULL")
        if not include_rejected:
            where.append("a.processing_status != ?")
            params.append(ProcessingStatus.REJECTED.value)
        if source_name:
            where.append("a.source_name = ?")
            params.append(source_name)
        if published_after:
            where.append("a.published_at >= ?")
            params.append(_iso(published_after))
        if published_before:
            where.append("a.published_at <= ?")
            params.append(_iso(published_before))
        if search:
            where.append("(a.title LIKE ? OR a.summary LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if cursor:
            where.append("a.published_at < ?")
            params.append(cursor)

        join = ""
        if entity_type and entity_id:
            join = "JOIN article_entities e ON e.article_id = a.article_id"
            where.append("e.entity_type = ? AND e.entity_id = ?")
            params.extend([entity_type, entity_id])

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        # Fetch limit+1 to determine has_more without a second COUNT query.
        sql = f"SELECT a.* FROM news_articles a {join} {clause} ORDER BY a.published_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit + 1)).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        articles = [self._row_to_article(r) for r in rows]
        next_cursor = _iso(articles[-1].published_at) if (articles and has_more) else None

        return {"articles": articles, "next_cursor": next_cursor, "has_more": has_more}

    def count(self, include_duplicates: bool = False, include_rejected: bool = False) -> int:
        """Total article count, matching the same default filters as query()."""
        where = []
        params: List[Any] = []
        if not include_duplicates:
            where.append("duplicate_of IS NULL")
        if not include_rejected:
            where.append("processing_status != ?")
            params.append(ProcessingStatus.REJECTED.value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._conn.execute(f"SELECT COUNT(*) FROM news_articles {clause}", params).fetchone()[0]

    def count_by_source(self) -> Dict[str, int]:
        """Article count per source — backs the existing 'source counts' UI feature."""
        rows = self._conn.execute(
            "SELECT source_name, COUNT(*) AS n FROM news_articles WHERE duplicate_of IS NULL GROUP BY source_name"
        ).fetchall()
        return {r["source_name"]: r["n"] for r in rows if r["source_name"]}

    # ---------------- ingestion checkpoints (spec §15) ----------------

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO ingestion_checkpoints
            (checkpoint_id, provider, period_start, period_end, cursor, articles_ingested, completed, last_updated_at, last_error)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            checkpoint.checkpoint_id, checkpoint.provider, _iso(checkpoint.period_start),
            _iso(checkpoint.period_end), checkpoint.cursor, checkpoint.articles_ingested,
            int(checkpoint.completed), _iso(checkpoint.last_updated_at), checkpoint.last_error,
        ))
        self._conn.commit()

    def get_checkpoint(self, checkpoint_id: str) -> Optional[IngestionCheckpoint]:
        row = self._conn.execute(
            "SELECT * FROM ingestion_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        if not row:
            return None
        return IngestionCheckpoint(
            checkpoint_id=row["checkpoint_id"], provider=row["provider"],
            period_start=_parse_iso(row["period_start"]), period_end=_parse_iso(row["period_end"]),
            cursor=row["cursor"], articles_ingested=row["articles_ingested"],
            completed=bool(row["completed"]), last_updated_at=_parse_iso(row["last_updated_at"]),
            last_error=row["last_error"],
        )
