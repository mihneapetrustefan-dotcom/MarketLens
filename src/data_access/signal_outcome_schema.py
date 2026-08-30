"""
src/data_access/signal_outcome_schema.py
---------------------------------------------
Persistence for signal outcomes and evaluations (Phase 10, spec §29,
§30).

AN OUTCOME IS A SEPARATE ROW FROM THE SIGNAL, NEVER A COLUMN ON IT
----------------------------------------------------------------------
Spec §30 is explicit: "do not overwrite historical outcomes". Writing
the realized return back onto the signal row would do exactly that —
the signal would stop being a record of what was believed BEFORE the
outcome and become a mixture of claim and result.

Keeping outcomes in their own table also means the same signal can be
scored over several horizons (d1, d5, d20) without the row growing a
column per horizon, and an outcome recorded today never mutates the
claim made last week.

WHAT IS AND IS NOT MEASURED HERE
------------------------------------
Measured: forward return, whether the direction was right, and the gap
between what was expected and what happened.

NOT measured: profit, position P&L, portfolio contribution, drawdown.
Those need position sizes, and Phase 10 has none by construction. A
signal-level hit rate is an honest measure of signal quality; a
"return" that silently assumed equal position sizing would be a
portfolio result wearing a signal's clothes.

EVALUATIONS ARE AGGREGATES, RECOMPUTED, NOT ACCUMULATED
-----------------------------------------------------------
signal_evaluations holds aggregate metrics over a cohort of outcomes
(by strategy, by confidence bucket, by regime). Those ARE recomputed
wholesale on each run — an aggregate is a derived view over outcomes,
and a stale one is worse than a rebuilt one. The underlying outcomes,
which are the actual observations, are never recomputed.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_signal_outcome_schema(conn: sqlite3.Connection) -> None:
    """Create the outcome and evaluation tables. Idempotent."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id           TEXT NOT NULL,
            horizon             TEXT NOT NULL,   -- 'd1', 'd5', 'd20'
            -- What actually happened, taken from the Phase 7 label
            -- that resolved after the signal's information cutoff.
            realized_return     REAL,
            realized_direction  TEXT,
            -- What the signal claimed, copied at scoring time so the
            -- comparison stays reconstructable even if thresholds
            -- change later.
            signal_direction    TEXT NOT NULL,
            expected_return     REAL,
            strength            REAL,
            confidence          REAL,
            -- Derived comparison
            direction_correct   INTEGER,
            error               REAL,
            absolute_error      REAL,
            -- Context carried for slicing without a join
            strategy_id         TEXT,
            strategy_version    TEXT,
            market_regime       TEXT,
            event_type          TEXT,
            confidence_bucket   TEXT,
            label_name          TEXT,
            measured_at         TEXT,
            scored_at           TEXT NOT NULL,
            PRIMARY KEY (signal_id, horizon)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_evaluations (
            evaluation_id       TEXT PRIMARY KEY,
            cohort_kind         TEXT NOT NULL,   -- 'overall','strategy','confidence_bucket','event_type','regime'
            cohort_value        TEXT NOT NULL,
            horizon             TEXT NOT NULL,
            sample_size         INTEGER NOT NULL DEFAULT 0,
            instrument_count    INTEGER,
            hit_rate            REAL,
            mean_return         REAL,
            median_return       REAL,
            mean_absolute_error REAL,
            mean_expected_return REAL,
            return_stdev        REAL,
            -- A baseline is mandatory here for the same reason it is
            -- in Phase 9: a hit rate without one is uninterpretable.
            baseline_hit_rate   REAL,
            beats_baseline      INTEGER,
            small_sample        INTEGER NOT NULL DEFAULT 0,
            notes_json          TEXT NOT NULL DEFAULT '[]',
            evaluated_at        TEXT NOT NULL
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_horizon ON signal_outcomes(horizon)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_strategy ON signal_outcomes(strategy_id, strategy_version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_bucket ON signal_outcomes(confidence_bucket)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_correct ON signal_outcomes(direction_correct)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evaluations_cohort ON signal_evaluations(cohort_kind, cohort_value)")

    conn.commit()
