"""
src/data_access/research_schema.py
-------------------------------------
SQL persistence for Phase 7 (Historical Event Studies / Research
Dataset) research observations.

WHAT AN OBSERVATION IS HERE
------------------------------
One (event, instrument) pair from Phase 6, split into the two sides
the project's own domain model insists stay separate:
  - research_features:  InformationSnapshot — everything knowable
    strictly BEFORE the event's market-visibility cutoff.
  - research_labels:     OutcomeSet — everything measured strictly
    AFTER that cutoff (the post-event returns, volume and volatility
    reactions Phase 6 already computed).

research_observations.quality_level mirrors SampleQuality.level
(spec §19-20): a study that came out UNUSABLE in Phase 6 still gets a
row here, marked INVALID with its exclusion reason recorded — never
silently dropped. Filtering happens at read time (DatasetBuilder,
Phase 7's own code), not at write time.

NEVER MERGE FEATURES AND LABELS INTO ONE ROW
------------------------------------------------
Two tables, not one wide table with a features/labels split by
column prefix. This is the same discipline ResearchObservation itself
enforces in code (no `to_row()` that merges both sides) — a schema
that merged them here would quietly undo that protection the moment
someone wrote `SELECT *`.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_research_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 7 research tables and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_observations (
            observation_id          TEXT PRIMARY KEY,
            event_id                TEXT NOT NULL,
            instrument_id           TEXT NOT NULL,
            benchmark_id            TEXT,
            event_type              TEXT,
            event_time              TEXT,
            information_time        TEXT,
            observation_created_at  TEXT NOT NULL,
            sector_id               TEXT,
            geography               TEXT,
            market_regime           TEXT,
            information_cutoff      TEXT NOT NULL,
            event_cluster_id        TEXT,
            quality_level           TEXT NOT NULL,
            exclusions_json         TEXT NOT NULL DEFAULT '[]',
            notes_json              TEXT NOT NULL DEFAULT '[]',
            dataset_version         TEXT NOT NULL,
            label_version           TEXT NOT NULL DEFAULT 'v1',
            feature_version         TEXT NOT NULL DEFAULT 'v1'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_features (
            observation_id  TEXT NOT NULL,
            qualified_name  TEXT NOT NULL,
            namespace       TEXT NOT NULL,
            value_json      TEXT,
            as_of           TEXT,
            source          TEXT NOT NULL DEFAULT '',
            calculation     TEXT NOT NULL DEFAULT '',
            feature_version TEXT NOT NULL DEFAULT 'v1',
            is_contemporaneous INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (observation_id, qualified_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_labels (
            observation_id  TEXT NOT NULL,
            name            TEXT NOT NULL,
            value_json      TEXT,
            measured_at     TEXT,
            window_name     TEXT NOT NULL DEFAULT '',
            label_version   TEXT NOT NULL DEFAULT 'v1',
            calculation     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (observation_id, name)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_obs_event ON research_observations(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_obs_instrument ON research_observations(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_obs_quality ON research_observations(quality_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_obs_cluster ON research_observations(event_cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_features_ns ON research_features(namespace)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_labels_window ON research_labels(window_name)")

    conn.commit()
