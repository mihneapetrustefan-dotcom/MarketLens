"""
src/data_access/entity_schema.py
-------------------------------------
Additive storage schema for the Phase 3 entity-intelligence layer.

Same discipline as Phases 1 and 2: new tables only, in the SAME
database, never touching anything that already exists. Every index
below serves one named lookup the resolution pipeline actually
performs at scale — nothing is indexed speculatively.

    idx_aliases_normalized  -> the hot path: normalized-name lookup
    idx_identifiers_value   -> ticker / ISIN / provider-id lookup
    idx_mentions_article    -> "which entities does this article mention"
    idx_mentions_entity     -> "which articles mention this entity"
    idx_relationships_from  -> outgoing edges
    idx_relationships_to    -> incoming edges
    idx_identity_entity     -> an entity's identity history
"""

import sqlite3


def initialize_entity_schema(conn: sqlite3.Connection) -> None:
    """Create every Phase 3 entity table and index if absent. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_aliases (
            entity_id         TEXT NOT NULL,
            entity_type       TEXT NOT NULL,
            alias             TEXT NOT NULL,
            normalized_alias  TEXT NOT NULL,
            alias_type        TEXT NOT NULL DEFAULT 'display_name',
            ambiguity_risk    INTEGER NOT NULL DEFAULT 0,
            valid_from        TEXT,
            valid_until       TEXT,
            PRIMARY KEY (entity_id, normalized_alias, alias_type)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_identifiers (
            entity_id        TEXT NOT NULL,
            entity_type      TEXT NOT NULL,
            identifier_type  TEXT NOT NULL,
            value            TEXT NOT NULL,
            provider         TEXT,
            active           INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (identifier_type, value, entity_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_mentions (
            article_id    TEXT NOT NULL,
            entity_id     TEXT NOT NULL,
            entity_type   TEXT NOT NULL,
            mention_text  TEXT NOT NULL,
            relevance     TEXT NOT NULL DEFAULT 'mentioned',
            confidence    TEXT NOT NULL DEFAULT '0',
            method        TEXT NOT NULL DEFAULT 'none',
            position      INTEGER,
            PRIMARY KEY (article_id, entity_id, mention_text)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_relationships (
            from_entity_id    TEXT NOT NULL,
            to_entity_id      TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            source            TEXT NOT NULL,
            provenance_kind   TEXT NOT NULL DEFAULT 'sourced_fact',
            confidence        TEXT NOT NULL DEFAULT '1',
            observed_at       TEXT,
            method            TEXT,
            valid_until       TEXT,
            PRIMARY KEY (from_entity_id, to_entity_id, relationship_type, source)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_identity_changes (
            entity_id          TEXT NOT NULL,
            change_type        TEXT NOT NULL,
            effective_at       TEXT NOT NULL,
            previous_value     TEXT,
            new_value          TEXT,
            related_entity_id  TEXT,
            source             TEXT,
            notes              TEXT,
            PRIMARY KEY (entity_id, change_type, effective_at)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_sector_classifications (
            entity_id     TEXT NOT NULL,
            sector_id     TEXT,
            industry_id   TEXT,
            source        TEXT NOT NULL DEFAULT 'internal',
            is_canonical  INTEGER NOT NULL DEFAULT 0,
            effective_at  TEXT,
            PRIMARY KEY (entity_id, source)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_normalized ON entity_aliases(normalized_alias)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_identifiers_value ON entity_identifiers(identifier_type, value)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_article ON entity_mentions(article_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_entity ON entity_mentions(entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_from ON entity_relationships(from_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_to ON entity_relationships(to_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_entity ON entity_identity_changes(entity_id)")

    conn.commit()
