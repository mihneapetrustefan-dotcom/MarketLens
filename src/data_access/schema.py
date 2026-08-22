"""
src/data_access/schema.py
------------------------------
Additive schema for the Phase 1 canonical tables.

RESPONSIBILITY:
Create the NEW tables this phase introduces, in the SAME SQLite file
the existing application already uses (data/marketlens.db) — never
touching, renaming, or dropping the EXISTING tables (`articles`,
`recommendations`, `portfolio_snapshots`), which keep working exactly
as they do today, read and written by exactly the same existing code.

SAFE TO RUN REPEATEDLY: every statement is `CREATE TABLE IF NOT
EXISTS` — calling initialize_schema() against an already-migrated
database is a safe no-op, the same pattern already established by
recommendation_log.py's own migration guard.

TABLES DELIBERATELY NOT CREATED YET (foundation only per this phase's
scope): news_articles (existing `articles` table is NOT migrated
in this phase), events, economic_events, market_observations,
corporate_actions. The canonical MODELS for these exist
(domain/models.py) so a future phase can add their tables and
populate them without any further model design work — see the
migration script's own "not yet migrated" documentation.
"""

import sqlite3


def initialize_schema(conn: sqlite3.Connection) -> None:
    """
    Create every Phase 1 canonical table if it doesn't already exist.
    Idempotent — safe to call on every application startup, exactly
    like recommendation_log.py's own _migrate_schema().
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exchanges (
            exchange_id   TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            country       TEXT NOT NULL,
            timezone      TEXT NOT NULL DEFAULT 'UTC'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sectors (
            sector_id     TEXT PRIMARY KEY,
            name          TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            company_id      TEXT PRIMARY KEY,
            canonical_name  TEXT NOT NULL UNIQUE,
            aliases_json    TEXT NOT NULL DEFAULT '[]',
            sector_id       TEXT,
            FOREIGN KEY (sector_id) REFERENCES sectors(sector_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS securities (
            security_id       TEXT PRIMARY KEY,
            company_id        TEXT,
            instrument_type   TEXT NOT NULL,
            currency          TEXT NOT NULL DEFAULT 'USD',
            FOREIGN KEY (company_id) REFERENCES companies(company_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id   TEXT PRIMARY KEY,
            security_id     TEXT NOT NULL,
            exchange_id     TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            asset_class     TEXT NOT NULL,
            FOREIGN KEY (security_id) REFERENCES securities(security_id),
            FOREIGN KEY (exchange_id) REFERENCES exchanges(exchange_id),
            UNIQUE (exchange_id, ticker)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_sources (
            source_id     TEXT PRIMARY KEY,
            name          TEXT NOT NULL UNIQUE,
            source_type   TEXT NOT NULL DEFAULT 'unclassified',
            url           TEXT,
            active        INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.commit()


def get_all_table_names(conn: sqlite3.Connection) -> list:
    """Utility for tests/verification: list every table currently in the database (canonical + pre-existing alike)."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row[0] for row in cursor.fetchall()]
