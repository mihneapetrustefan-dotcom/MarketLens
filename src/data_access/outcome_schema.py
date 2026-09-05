"""
src/data_access/outcome_schema.py
-----------------------------------------
Persistence for outcome measurements and their aggregates (Phase 19).

WHY THIS IS NOT AN EXTENSION OF `signal_outcomes` (§48)
-----------------------------------------------------------
§48 says to reuse the existing outcome tables where semantically
correct. They are audited and they are not, for two structural reasons:

  1. `signal_outcomes` is keyed `PRIMARY KEY (signal_id, horizon)`.
     There is nowhere to put a methodology version, so re-measuring
     under a new rule would OVERWRITE the old measurement. §26 and §31
     both forbid that, and it cannot be fixed by adding a column —
     SQLite cannot alter a primary key in place.

  2. It is LABEL-derived. Its `realized_return` comes from the Phase 7
     research label that resolved after the cutoff. Phase 19 measures
     from price candles: a reference price, an end price, and the highs
     and lows in between. There is no column for any of those, and no
     concept of a window that has not closed yet.

So `signal_outcomes` keeps its ten rows and its history, documented as
the Phase 10 predecessor, and is neither extended nor deleted. This
table is the canonical factual layer. Two tables that MEASURE the same
thing would be the redundancy §48 warns about; a superseded table left
intact for its history is not.

ONE TABLE FOR PREDICTIONS AND SIGNALS
-----------------------------------------
`subject_kind` distinguishes them rather than two near-identical
tables. A prediction outcome and a signal outcome are the same
measurement — a forward return over a window from a reference price —
asked about different claims. The claims differ; the measurement does
not. Keeping them together is what makes "did the signal layer add
anything to the model's own prediction" a single query rather than a
join between two schemas that will drift.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS. Nothing here alters or drops an existing table.
"""

from __future__ import annotations

import sqlite3


def initialize_outcome_schema(conn: sqlite3.Connection) -> None:
    """Create the outcome tables. Idempotent and additive."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcome_measurements (
            -- IDENTITY (§30). The methodology version is part of the
            -- key, so re-measuring under a new rule ADDS a row beside
            -- the old one instead of rewriting what was reported.
            subject_kind        TEXT NOT NULL,   -- 'prediction' | 'signal'
            subject_id          TEXT NOT NULL,
            horizon             TEXT NOT NULL,   -- '15m','1h','1d','5d'
            method_version      TEXT NOT NULL,

            -- Split out so a consumer can order horizons arithmetically.
            -- '10d' sorts before '5d' as text, and a decay curve built
            -- on string order would be wrong without ever looking wrong.
            horizon_value       REAL NOT NULL,
            horizon_unit        TEXT NOT NULL,

            -- STATUS (§32). 'pending' and 'insufficient_data' are
            -- different answers: come back later versus stop waiting.
            status              TEXT NOT NULL,

            -- THE WINDOW, EXPLICIT (§6)
            information_cutoff  TEXT,
            window_start        TEXT,
            window_end          TEXT,

            -- PRICES (§7, §8). The rule is stored, not assumed: a
            -- return without a stated starting point is not
            -- reproducible.
            reference_price     REAL,
            reference_at        TEXT,
            reference_rule      TEXT NOT NULL,
            end_price           REAL,
            end_at              TEXT,

            -- RETURNS (§7). Both forms: log returns add across time,
            -- simple returns add across a portfolio.
            simple_return       REAL,
            log_return          REAL,

            -- EXCURSIONS (§11, §12). Signed favourable-positive for
            -- both directions so longs and shorts pool into one
            -- distribution.
            mfe                 REAL,
            mae                 REAL,
            mfe_at              TEXT,
            mae_at              TEXT,
            time_to_mfe_seconds REAL,
            time_to_mae_seconds REAL,

            -- DIRECTION (§9, §10). 'insufficient_data' is a real value
            -- here; a NULL that silently reads as MISS would understate
            -- every hit rate in the system.
            expected_direction  TEXT NOT NULL DEFAULT '',
            realized_direction  TEXT,
            direction_result    TEXT NOT NULL,

            expected_return     REAL,
            error               REAL,
            absolute_error      REAL,

            -- PROVENANCE OF THE MEASUREMENT (§26, §56)
            data_source         TEXT NOT NULL DEFAULT '',
            data_interval       TEXT NOT NULL DEFAULT '',
            bars_observed       INTEGER NOT NULL DEFAULT 0,
            data_as_of          TEXT,

            -- CONTEXT for slicing without a join (§16-§22). Copied at
            -- measurement time so a later demotion or relabelling
            -- cannot silently rewrite what a past measurement was made
            -- under.
            instrument_id       TEXT NOT NULL DEFAULT '',
            trained_model_id    TEXT,
            model_status        TEXT,
            strategy_id         TEXT,
            market_regime       TEXT,
            event_type          TEXT,
            confidence          REAL,
            strength            REAL,
            signal_status       TEXT,

            notes_json          TEXT NOT NULL DEFAULT '[]',
            computed_at         TEXT NOT NULL,

            PRIMARY KEY (subject_kind, subject_id, horizon, method_version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcome_aggregates (
            aggregate_id        TEXT PRIMARY KEY,
            method_version      TEXT NOT NULL,
            subject_kind        TEXT NOT NULL,
            -- 'overall','model','instrument','asset_class','event_type',
            -- 'regime','direction','confidence_bucket','strength_bucket',
            -- 'model_status'
            cohort_kind         TEXT NOT NULL,
            cohort_value        TEXT NOT NULL,
            horizon             TEXT NOT NULL,

            -- SAMPLE QUALITY (§23). Never optional. An aggregate whose
            -- sample size is not visible invites a reader to trust a
            -- mean of four.
            sample_size         INTEGER NOT NULL DEFAULT 0,
            instrument_count    INTEGER NOT NULL DEFAULT 0,
            small_sample        INTEGER NOT NULL DEFAULT 1,

            -- Counts, kept separate so a hit rate can always be
            -- recomputed and audited from its parts.
            hits                INTEGER NOT NULL DEFAULT 0,
            misses              INTEGER NOT NULL DEFAULT 0,
            neutrals            INTEGER NOT NULL DEFAULT 0,
            insufficient        INTEGER NOT NULL DEFAULT 0,
            directional_accuracy REAL,

            -- DISTRIBUTION, not just win/loss (§15)
            mean_return         REAL,
            median_return       REAL,
            stdev_return        REAL,
            min_return          REAL,
            max_return          REAL,
            p10_return          REAL,
            p25_return          REAL,
            p75_return          REAL,
            p90_return          REAL,

            mean_mfe            REAL,
            mean_mae            REAL,
            median_mfe          REAL,
            median_mae          REAL,
            mean_time_to_mfe    REAL,

            mean_expected_return REAL,
            mean_absolute_error REAL,

            -- Uncertainty, only where it was actually computed (§40).
            -- NULL means "not calculated", never "zero width".
            ci_low              REAL,
            ci_high             REAL,
            ci_method           TEXT,

            notes_json          TEXT NOT NULL DEFAULT '[]',
            computed_at         TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome_subject
        ON outcome_measurements (subject_kind, subject_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome_status
        ON outcome_measurements (status, horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome_model
        ON outcome_measurements (trained_model_id, horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome_instrument
        ON outcome_measurements (instrument_id, horizon)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outcome_aggregate_cohort
        ON outcome_aggregates (cohort_kind, cohort_value, horizon)
    """)
    conn.commit()
