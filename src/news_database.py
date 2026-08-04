"""
news_database.py
--------------------
Database module for MarketLens.

RESPONSIBILITY:
Persist processed articles (the fully-tagged output of the entire
pipeline — Cleaner through Impact Engine) across separate runs, using
SQLite. This is what turns MarketLens from "reprocesses whatever it
happens to collect right now" into a system that ACCUMULATES evidence
over time — which is exactly what Confidence Score and Recommendation
Engine need to become genuinely useful (more independent confirmations
appearing as days pass, not just within a single run).

KEY DESIGN DECISION — identity key is URL, not article_id:
`article_id` is a UUID generated fresh by News Collector on EVERY run,
even for the exact same real-world article collected again tomorrow.
It can never be used to detect "have I already stored this." The
article's (cleaned) URL is the only stable identity across runs, so it
is stored with a UNIQUE constraint and used for idempotent inserts:
saving the same URL twice is a no-op the second time, not a duplicate
row.

SCHEMA:
One table, `articles`, with the nested fields produced by Company/
Ticker/Sector Detector, Sentiment Engine, and Impact Engine
(`companies_mentioned`, `tickers_mentioned`, `sectors`, `sentiment`,
`impact`) stored as JSON text columns — SQLite has no native list/dict
type, and this keeps the schema simple (one table, no joins) while
losing nothing: they round-trip back to their exact original Python
shape on load.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger("marketlens.news_database")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class NewsDatabase:
    """
    SQLite-backed persistence layer for processed MarketLens articles.
    """

    # Columns that hold nested Python objects (lists/dicts) and must be
    # JSON-encoded/decoded on the way in/out. Centralized as a constant
    # so save/load never drift out of sync on which fields need this.
    _JSON_COLUMNS = ("companies_mentioned", "tickers_mentioned", "sectors", "sentiment", "impact")

    def __init__(self, db_path: str):
        """
        Args:
            db_path: filesystem path to the SQLite database file. Pass
                ":memory:" for a temporary, in-process database (used
                by this module's own unit tests). In Colab, pass a path
                under a mounted Google Drive folder so the file survives
                across sessions, e.g.
                "/content/drive/MyDrive/MarketLens/marketlens.db".
        """
        self.db_path = db_path
        # check_same_thread=False: Colab notebooks can execute cells
        # that reuse this connection object across different call
        # frames; this avoids sqlite3's default same-thread restriction
        # without needing a connection pool for what is, in practice,
        # single-threaded notebook usage.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # fetch rows as dict-like objects, not bare tuples
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """
        Create the `articles` table if it doesn't already exist. Safe
        to call every time the class is instantiated — CREATE TABLE IF
        NOT EXISTS is a no-op against an already-initialized database.
        """
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                article_id           TEXT PRIMARY KEY,
                url                   TEXT UNIQUE NOT NULL,
                title                 TEXT,
                summary               TEXT,
                source                TEXT,
                category              TEXT,
                published_at          TEXT,
                collected_at          TEXT,
                duplicate_group_id    TEXT,
                duplicate_group_size  INTEGER,
                companies_mentioned   TEXT,
                tickers_mentioned     TEXT,
                sectors               TEXT,
                sentiment             TEXT,
                impact                TEXT,
                stored_at             TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def _article_to_row(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert one pipeline article dict into a flat dict of column
        values ready for SQL insertion — JSON-encoding the nested
        fields listed in _JSON_COLUMNS.
        """
        row = {
            "article_id": article.get("article_id"),
            "url": article.get("url"),
            "title": article.get("title"),
            "summary": article.get("summary"),
            "source": article.get("source"),
            "category": article.get("category"),
            "published_at": article.get("published_at"),
            "collected_at": article.get("collected_at"),
            "duplicate_group_id": article.get("duplicate_group_id"),
            "duplicate_group_size": article.get("duplicate_group_size"),
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        for column in self._JSON_COLUMNS:
            row[column] = json.dumps(article.get(column))
        return row

    def _row_to_article(self, row: sqlite3.Row) -> Dict[str, Any]:
        """
        Convert one database row back into a plain article dict,
        decoding the JSON-encoded nested fields back into their
        original Python list/dict shape.
        """
        article = {key: row[key] for key in row.keys() if key not in self._JSON_COLUMNS}
        for column in self._JSON_COLUMNS:
            raw_value = row[column]
            article[column] = json.loads(raw_value) if raw_value is not None else None
        return article

    def save_articles(self, articles: List[Dict[str, Any]]) -> int:
        """
        Persist a batch of processed articles. Idempotent by URL: an
        article whose URL already exists in the database is silently
        skipped (the ALREADY-STORED version is kept — see module
        docstring for why URL, not article_id, is the identity key).

        Returns:
            The number of articles NEWLY inserted (excludes skipped
            already-known URLs), so callers can report how much new
            ground was actually covered by a given run.
        """
        if not articles:
            return 0

        inserted_count = 0
        for article in articles:
            row = self._article_to_row(article)
            cursor = self._conn.execute("""
                INSERT OR IGNORE INTO articles (
                    article_id, url, title, summary, source, category,
                    published_at, collected_at, duplicate_group_id,
                    duplicate_group_size, companies_mentioned,
                    tickers_mentioned, sectors, sentiment, impact, stored_at
                ) VALUES (
                    :article_id, :url, :title, :summary, :source, :category,
                    :published_at, :collected_at, :duplicate_group_id,
                    :duplicate_group_size, :companies_mentioned,
                    :tickers_mentioned, :sectors, :sentiment, :impact, :stored_at
                )
            """, row)
            if cursor.rowcount > 0:
                inserted_count += 1

        self._conn.commit()
        skipped_count = len(articles) - inserted_count
        logger.info(
            "NewsDatabase: %d article(s) newly stored, %d already known (skipped)",
            inserted_count, skipped_count,
        )
        return inserted_count

    def load_all_articles(self) -> List[Dict[str, Any]]:
        """
        Load every article ever stored in this database, across every
        past run — this is what lets Confidence Score / Recommendation
        Engine aggregate evidence accumulated over multiple days, not
        just whatever was collected in the current run.
        """
        cursor = self._conn.execute("SELECT * FROM articles")
        return [self._row_to_article(row) for row in cursor.fetchall()]

    def load_articles_since(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        """
        Load only articles whose `collected_at` timestamp is at or
        after `cutoff_iso` (an ISO 8601 string) — useful for "articles
        from the last N days" style queries without loading the entire
        history every time.
        """
        cursor = self._conn.execute(
            "SELECT * FROM articles WHERE collected_at >= ?", (cutoff_iso,)
        )
        return [self._row_to_article(row) for row in cursor.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        """
        Return a small summary of the database's current contents:
        total article count, number of distinct sources, and the
        earliest/latest `collected_at` timestamps on file.
        """
        total = self._conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        distinct_sources = self._conn.execute(
            "SELECT COUNT(DISTINCT source) FROM articles"
        ).fetchone()[0]
        date_range = self._conn.execute(
            "SELECT MIN(collected_at), MAX(collected_at) FROM articles"
        ).fetchone()

        return {
            "total_articles": total,
            "distinct_sources": distinct_sources,
            "earliest_collected_at": date_range[0],
            "latest_collected_at": date_range[1],
        }

    def archive_old_articles(self, older_than_days: int, archive_path: Optional[str] = None) -> int:
        """
        Remove articles older than `older_than_days` (by `collected_at`)
        from the live database, optionally preserving them first in a
        separate JSON archive file.

        WHY THIS EXISTS: NewsDatabase accumulates articles forever, with
        no built-in limit — after months of daily runs, the live table
        could grow large enough to slow down `load_all_articles()` and
        Confidence Score's per-run aggregation (which loads and
        processes every stored article on every run). This lets old,
        no-longer-actionable articles be moved out of that hot path
        without losing them outright.

        Args:
            older_than_days: articles with `collected_at` older than
                this many days ago are archived/removed.
            archive_path: optional file path. If given, matched
                articles are appended (as a JSON array) to this file
                BEFORE being deleted from the database — so no data is
                destroyed, just relocated out of the live table. If
                omitted, matched articles are deleted without being
                saved anywhere; use that only if you're confident you
                won't need them again (e.g. a test/demo database).

        Returns:
            The number of articles archived/removed. Never raises for
            an empty result — returns 0 if nothing matched the cutoff.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        cursor = self._conn.execute("SELECT * FROM articles WHERE collected_at < ?", (cutoff,))
        old_articles = [self._row_to_article(row) for row in cursor.fetchall()]

        if not old_articles:
            logger.info("NewsDatabase: no articles older than %d days to archive", older_than_days)
            return 0

        if archive_path:
            existing: List[Dict[str, Any]] = []
            if os.path.exists(archive_path):
                try:
                    with open(archive_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    # A corrupted/unreadable archive file must never
                    # block archival — start a fresh archive instead of
                    # silently losing the current batch of old articles.
                    logger.warning("Could not read existing archive at '%s'; starting a new one", archive_path)
                    existing = []
            existing.extend(old_articles)
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=1)

        self._conn.execute("DELETE FROM articles WHERE collected_at < ?", (cutoff,))
        self._conn.commit()

        logger.info(
            "NewsDatabase: archived/removed %d article(s) older than %d days%s",
            len(old_articles), older_than_days,
            f" (saved to {archive_path})" if archive_path else " (not saved anywhere)",
        )
        return len(old_articles)

    def close(self) -> None:
        """Close the underlying SQLite connection cleanly."""
        self._conn.close()
