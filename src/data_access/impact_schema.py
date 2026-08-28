"""
src/data_access/impact_schema.py
-------------------------------------
SQL persistence for Phase 6 (Market Impact Intelligence) event studies.

SCOPE, STATED PLAINLY
----------------------
This persists exactly what EventStudyEngine.build_study() computes:
returns, volume, volatility, and gap measurements per window, plus the
data-quality verdict. EventStudy also has fields for sector/peer/macro
context, market regime, and confounding events — those come from
separate engine methods (compute_market_regime, detect_confounders,
classify_dimensions) that operate across multiple studies, not from
build_study() itself. This schema does not include them; they are a
later addition when that cross-event analysis is actually wired up,
not a gap in this one.

ONE ROW PER (STUDY, WINDOW)
------------------------------
A study measures several windows (d1, d3, d5, d10, d20, and any minute
windows available). event_study_returns / _volume / _volatility hold
one row per window per study, not one wide row per study — a
DEFAULT_WINDOWS change should not require a schema migration.

QUALITY IS PER-MEASUREMENT, NOT JUST PER-STUDY
-------------------------------------------------
Each ReturnMeasurement/VolumeReaction/VolatilityReaction carries its
own DataQuality (a window can fail independently of the study as a
whole — e.g. d20 missing data while d1 is fine). The per-window
quality_issues column preserves that; the top-level event_studies
table also has one for the whole-study verdict (INSUFFICIENT_HISTORY
etc., set before any window is computed).

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_impact_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 6 event study tables and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_studies (
            study_id                    TEXT PRIMARY KEY,
            event_id                    TEXT NOT NULL,
            instrument_id               TEXT NOT NULL,
            benchmark_id                TEXT,
            event_time                  TEXT,
            publication_time            TEXT,
            ingestion_time              TEXT,
            market_visibility_earliest  TEXT,
            market_visibility_latest    TEXT,
            visibility_basis            TEXT,
            is_direct                   INTEGER NOT NULL DEFAULT 1,
            quality_level               TEXT NOT NULL,
            quality_issues_json         TEXT NOT NULL DEFAULT '[]',
            observations_available      INTEGER NOT NULL DEFAULT 0,
            observations_expected       INTEGER NOT NULL DEFAULT 0,
            gap_occurred_during_session INTEGER,
            gap_previous_close          TEXT,
            gap_next_open               TEXT,
            gap_return                  REAL,
            gap_intraday_followthrough  REAL,
            computed_at                 TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_study_returns (
            study_id          TEXT NOT NULL,
            window_name       TEXT NOT NULL,
            method            TEXT NOT NULL,
            price_before      TEXT,
            price_after       TEXT,
            raw_return        REAL,
            benchmark_id      TEXT,
            benchmark_return  REAL,
            expected_return   REAL,
            abnormal_return   REAL,
            benchmark_model   TEXT,
            quality_level     TEXT NOT NULL,
            quality_issues_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (study_id, window_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_study_volume (
            study_id             TEXT NOT NULL,
            window_name          TEXT NOT NULL,
            event_volume         REAL,
            baseline_mean_volume REAL,
            baseline_std_volume  REAL,
            relative_volume      REAL,
            volume_zscore        REAL,
            quality_level        TEXT NOT NULL,
            quality_issues_json  TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (study_id, window_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_study_volatility (
            study_id              TEXT NOT NULL,
            window_name           TEXT NOT NULL,
            pre_volatility        REAL,
            post_volatility       REAL,
            volatility_change_pct REAL,
            quality_level         TEXT NOT NULL,
            quality_issues_json   TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (study_id, window_name)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_studies_event ON event_studies(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_studies_instrument ON event_studies(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_studies_quality ON event_studies(quality_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_returns_window ON event_study_returns(window_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_volume_window ON event_study_volume(window_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_volatility_window ON event_study_volatility(window_name)")

    conn.commit()
