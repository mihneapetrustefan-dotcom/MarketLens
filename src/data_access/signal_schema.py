"""
src/data_access/signal_schema.py
-------------------------------------
SQL persistence for Phase 10 (Signal Engine).

SIGNALS ARE APPEND-ONLY IN SPIRIT (spec §15, §45, §49)
----------------------------------------------------------
A signal is never deleted and never rewritten into a different claim.
It expires, or it is superseded by a newer signal that points back at
it. That is what makes the table an audit record rather than a cache
of current opinion: "what did the system believe on the 14th?" must be
answerable later, and it cannot be if yesterday's row was overwritten.

Re-running generation for an information state that already produced a
signal is therefore idempotent by IDENTITY, not by overwrite — see
signal_identity_hash below.

CONTRIBUTIONS AND SUPPRESSIONS ARE TABLES, NOT JSON BLOBS
-------------------------------------------------------------
Both are queryable on purpose. "Which model contributed to signals
that later proved wrong?" and "how often do we suppress for stale
data?" are the questions this phase exists to make answerable; burying
either in a serialized column would turn them into log-grep exercises.

Explanation factors and caveats DO live as JSON: they are prose meant
for a human reading one signal, not aggregate-queried across many.

NO QUANTITY COLUMN ANYWHERE
-------------------------------
There is no position_size, no notional, no order quantity, no account.
Phase 11 introduces those with its own tables. A quantity column here
would let a later shortcut skip the risk layer entirely, and the
cheapest place to prevent that is the schema.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_signal_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 10 tables and indexes. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_strategies (
            strategy_id           TEXT NOT NULL,
            version               TEXT NOT NULL,
            name                  TEXT NOT NULL,
            signal_type           TEXT NOT NULL,
            description           TEXT NOT NULL DEFAULT '',
            is_active             INTEGER NOT NULL DEFAULT 1,
            configuration_version TEXT NOT NULL DEFAULT 'v1',
            parameters_json       TEXT NOT NULL DEFAULT '{}',
            created_at            TEXT,
            PRIMARY KEY (strategy_id, version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id                   TEXT PRIMARY KEY,
            -- Deterministic hash of (strategy, instrument, information
            -- state). Two generation runs over the same information
            -- produce the same hash, which is how duplicate signals are
            -- detected without overwriting the original (spec §22).
            signal_identity_hash        TEXT NOT NULL,
            instrument_id               TEXT NOT NULL,
            security_id                 TEXT,
            company_id                  TEXT,
            signal_type                 TEXT NOT NULL,
            direction                   TEXT NOT NULL,
            status                      TEXT NOT NULL,
            strength                    REAL,
            confidence                  REAL,
            expected_return             REAL,
            expected_return_horizon_days REAL,
            probability_up              REAL,
            agreement_state             TEXT NOT NULL,

            -- provenance
            observation_id              TEXT,
            event_id                    TEXT,
            strategy_id                 TEXT,
            strategy_version            TEXT,
            configuration_version       TEXT,
            feature_set_version         TEXT,
            dataset_version             TEXT,
            source_information_cutoff   TEXT,
            provenance_inputs_json      TEXT NOT NULL DEFAULT '{}',

            -- context
            market_regime               TEXT,
            volatility_percentile       REAL,
            relative_volume             REAL,
            liquidity_note              TEXT,
            event_type                  TEXT,
            event_corroboration_state   TEXT,
            independent_source_count    INTEGER,
            data_quality_level          TEXT,

            -- explanation
            explanation_summary         TEXT NOT NULL DEFAULT '',
            explanation_factors_json    TEXT NOT NULL DEFAULT '[]',
            explanation_caveats_json    TEXT NOT NULL DEFAULT '[]',

            suppression_note            TEXT NOT NULL DEFAULT '',
            created_at                  TEXT,
            valid_from                  TEXT,
            valid_until                 TEXT,
            superseded_by               TEXT,
            metadata_json               TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_contributions (
            signal_id          TEXT NOT NULL,
            prediction_id      TEXT NOT NULL,
            trained_model_id   TEXT NOT NULL,
            model_qualified_id TEXT NOT NULL,
            predicted_value    REAL,
            probability_up     REAL,
            confidence         REAL,
            weight             REAL NOT NULL DEFAULT 1.0,
            reliability        REAL,
            is_abstention      INTEGER NOT NULL DEFAULT 0,
            note               TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (signal_id, prediction_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_suppressions (
            signal_id  TEXT NOT NULL,
            reason     TEXT NOT NULL,
            PRIMARY KEY (signal_id, reason)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_instrument ON signals(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_identity ON signals(signal_identity_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id, strategy_version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_cutoff ON signals(source_information_cutoff)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_valid_until ON signals(valid_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contributions_model ON signal_contributions(trained_model_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contributions_prediction ON signal_contributions(prediction_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suppressions_reason ON signal_suppressions(reason)")

    conn.commit()
