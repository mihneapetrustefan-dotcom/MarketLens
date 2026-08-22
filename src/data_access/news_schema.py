"""
src/data_access/news_schema.py
-----------------------------------
Additive storage schema for the Phase 2 news data layer.

RESPONSIBILITY:
Create the news tables in the SAME database Phase 1 established, never
touching the EXISTING `articles` / `recommendations` /
`portfolio_snapshots` tables — the running application keeps reading
and writing those exactly as it does today.

INDEX DISCIPLINE (spec §16: "do not blindly index every field"): each
index below exists for one named query pattern the spec requires.
Fields with low selectivity (language, country, processing_status) are
deliberately NOT indexed — they filter poorly and cost write
throughput at ingestion scale.

    idx_articles_published_at   -> "latest news", date-range queries, cursor pagination
    idx_articles_source         -> "news by source"
    idx_articles_provider_pid   -> dedup Level 1 + idempotent upsert
    idx_articles_canonical_url  -> dedup Level 2
    idx_articles_fingerprint    -> dedup Level 3
    idx_article_entities_*      -> "news by company/instrument/sector"

ENTITY LINKS AS A JOIN TABLE, not JSON columns: "news by company" is a
first-class query pattern (spec §17), and a JSON array cannot be
indexed for it. One row per (article, entity) keeps that query fast at
millions of articles.
"""

import sqlite3


def initialize_news_schema(conn: sqlite3.Connection) -> None:
    """Create every Phase 2 news table and index if absent. Idempotent — safe on every startup."""

    # Raw provider responses, preserved for inspection/reproducibility
    # (spec §3). Separate table so the hot `news_articles` table stays
    # narrow — raw payloads are large and rarely read.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_articles (
            raw_id               TEXT PRIMARY KEY,
            provider             TEXT NOT NULL,
            provider_article_id  TEXT,
            fetched_at           TEXT NOT NULL,
            payload_json         TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_articles (
            article_id            TEXT PRIMARY KEY,
            provider              TEXT NOT NULL,
            provider_article_id   TEXT,
            raw_id                TEXT,
            source_id             TEXT,
            source_name           TEXT,
            source_url            TEXT,
            canonical_url         TEXT,
            title                 TEXT NOT NULL,
            summary               TEXT,
            language              TEXT,
            country               TEXT,
            author                TEXT,
            categories_json       TEXT NOT NULL DEFAULT '[]',
            published_at          TEXT,
            ingested_at           TEXT,
            updated_at            TEXT,
            fingerprint           TEXT,
            content_fingerprint   TEXT,
            duplicate_of          TEXT,
            duplicate_match_level TEXT NOT NULL DEFAULT 'none',
            sentiment_label       TEXT,
            sentiment_score       TEXT,
            impact_score          TEXT,
            processing_status     TEXT NOT NULL DEFAULT 'ingested',
            rejection_reason      TEXT,
            version               INTEGER NOT NULL DEFAULT 1
        )
    """)

    # One row per (article, entity) — see the module docstring for why
    # this is a join table rather than JSON columns.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS article_entities (
            article_id   TEXT NOT NULL,
            entity_type  TEXT NOT NULL,   -- 'company' | 'instrument' | 'sector' | 'event'
            entity_id    TEXT NOT NULL,
            PRIMARY KEY (article_id, entity_type, entity_id)
        )
    """)

    # Resumable historical-import position (spec §15).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
            checkpoint_id      TEXT PRIMARY KEY,
            provider           TEXT NOT NULL,
            period_start       TEXT,
            period_end         TEXT,
            cursor             TEXT,
            articles_ingested  INTEGER NOT NULL DEFAULT 0,
            completed          INTEGER NOT NULL DEFAULT 0,
            last_updated_at    TEXT,
            last_error         TEXT
        )
    """)

    # --- Indexes: one per named query pattern (see module docstring) ---
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_published_at ON news_articles(published_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON news_articles(source_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_provider_pid ON news_articles(provider, provider_article_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_canonical_url ON news_articles(canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_fingerprint ON news_articles(fingerprint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_type, entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_entities_article ON article_entities(article_id)")

    conn.commit()
