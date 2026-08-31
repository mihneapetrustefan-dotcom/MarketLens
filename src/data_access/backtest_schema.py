"""
src/data_access/backtest_schema.py
---------------------------------------
SQL persistence for Phase 12 (backtesting and market simulation).

WHY THE CONFIGURATION IS STORED TWICE
-----------------------------------------
`backtest_runs` carries both `config_json` — the complete, verbatim
configuration — and a handful of promoted columns for the fields people
actually filter on (cost model, slippage model, execution timing,
period).

The JSON is what makes a run reproducible: spec §54 requires the exact
configuration, and a set of columns will always lag the dataclass. The
promoted columns are what makes "show me every run with zero costs" a
query instead of a full scan. Neither alone is sufficient, and keeping
both is cheaper than regretting either.

RUNS ARE APPEND-ONLY
------------------------
A run records what a specific configuration produced at a specific
moment. Re-running writes a new row, keyed by a run id derived from the
configuration fingerprint plus the code version — so an identical rerun
is idempotent, while any changed assumption produces a distinct run
rather than overwriting the evidence of the previous one. Spec §85 and
§86 depend on that: you cannot detect repeated optimisation against the
same period if each attempt erases the last.

ORDERS, FILLS AND TRADES ARE SEPARATE TABLES
------------------------------------------------
Spec §18 and §58 both require it. An order that was rejected has no
fill; a fill that only reduced a position has no trade. Collapsing them
would make "how often did we fail to fill?" and "what did this trade
cost?" unanswerable, which are the questions the ledger exists for.

NOTHING HERE REFERENCES A BROKER
------------------------------------
No account, venue, order id from an exchange, or execution report.
These are simulation records priced against cached bars.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_backtest_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 12 tables and indexes. Idempotent."""

    # ---------------- the experiment and its runs ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtests (
            backtest_id  TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            created_at   TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id                 TEXT PRIMARY KEY,
            backtest_id            TEXT NOT NULL,
            status                 TEXT NOT NULL,
            config_fingerprint     TEXT NOT NULL,

            period_start           TEXT,
            period_end             TEXT,
            initial_capital        REAL,
            base_currency          TEXT NOT NULL DEFAULT 'USD',
            benchmark_instrument_id TEXT,

            -- promoted for filtering; the full truth is config_json
            execution_timing       TEXT,
            cost_model_version     TEXT,
            slippage_model_version TEXT,
            slippage_method        TEXT,
            constraint_set_version TEXT,
            sizing_strategy_id     TEXT,

            -- reproducibility (spec §6, §54)
            risk_engine_version    TEXT,
            execution_model_version TEXT,
            calendar_version       TEXT,
            strategy_version       TEXT,
            model_version          TEXT,
            feature_set_version    TEXT,
            dataset_version        TEXT,
            code_version           TEXT,
            random_seed            INTEGER NOT NULL DEFAULT 0,

            config_json            TEXT NOT NULL DEFAULT '{}',
            identity_json          TEXT NOT NULL DEFAULT '{}',
            quality_json           TEXT NOT NULL DEFAULT '{}',

            observations_processed INTEGER NOT NULL DEFAULT 0,
            duration_seconds       REAL,
            started_at             TEXT,
            finished_at            TEXT,
            created_at             TEXT
        )
    """)

    # ---------------- execution records ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS simulated_orders (
            order_id           TEXT PRIMARY KEY,
            run_id             TEXT NOT NULL,
            instrument_id      TEXT NOT NULL,
            side               TEXT NOT NULL,
            quantity           REAL NOT NULL,
            filled_quantity    REAL NOT NULL DEFAULT 0,
            state              TEXT NOT NULL,
            reject_reason      TEXT,
            information_cutoff TEXT,
            decision_at        TEXT,
            created_at         TEXT,
            signal_id          TEXT,
            decision_id        TEXT,
            target_weight      REAL,
            note               TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS simulated_fills (
            fill_id         TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            order_id        TEXT NOT NULL,
            instrument_id   TEXT NOT NULL,
            side            TEXT NOT NULL,
            quantity        REAL NOT NULL,
            -- the bar price before slippage, and what was charged after
            reference_price REAL NOT NULL,
            price           REAL NOT NULL,
            commission      REAL NOT NULL DEFAULT 0,
            slippage_cost   REAL NOT NULL DEFAULT 0,
            participation   REAL,
            is_partial      INTEGER NOT NULL DEFAULT 0,
            bar_timestamp   TEXT,
            filled_at       TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            trade_id          TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL,
            instrument_id     TEXT NOT NULL,
            side              TEXT NOT NULL,
            quantity          REAL NOT NULL,
            entry_price       REAL NOT NULL,
            exit_price        REAL NOT NULL,
            entry_at          TEXT NOT NULL,
            exit_at           TEXT NOT NULL,
            gross_pnl         REAL NOT NULL DEFAULT 0,
            costs             REAL NOT NULL DEFAULT 0,
            net_pnl           REAL NOT NULL DEFAULT 0,
            holding_days      REAL,
            return_pct        REAL,
            mfe               REAL,
            mae               REAL,
            exit_reason       TEXT NOT NULL DEFAULT '',
            sector_id         TEXT,
            strategy_id       TEXT,
            entry_signal_id   TEXT,
            entry_decision_id TEXT
        )
    """)

    # ---------------- series and metrics ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_equity (
            run_id          TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            equity          REAL,
            cash            REAL,
            positions_value REAL,
            gross_exposure  REAL,
            net_exposure    REAL,
            benchmark_value REAL,
            drawdown        REAL,
            open_positions  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, timestamp)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_metrics (
            run_id      TEXT NOT NULL,
            metric      TEXT NOT NULL,
            value       REAL,
            -- Populated when a metric could NOT be computed. A row with
            -- a null value and a reason is a statement; a missing row
            -- is silence.
            unavailable_reason TEXT,
            PRIMARY KEY (run_id, metric)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_attribution (
            run_id     TEXT NOT NULL,
            dimension  TEXT NOT NULL,
            bucket_key TEXT NOT NULL,
            label      TEXT NOT NULL DEFAULT '',
            trades     INTEGER NOT NULL DEFAULT 0,
            wins       INTEGER NOT NULL DEFAULT 0,
            gross_pnl  REAL NOT NULL DEFAULT 0,
            costs      REAL NOT NULL DEFAULT 0,
            net_pnl    REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, dimension, bucket_key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_drawdowns (
            run_id        TEXT NOT NULL,
            peak_at       TEXT NOT NULL,
            peak_equity   REAL,
            trough_at     TEXT NOT NULL,
            trough_equity REAL,
            depth         REAL,
            recovered_at  TEXT,
            duration_days REAL,
            recovery_days REAL,
            PRIMARY KEY (run_id, peak_at, trough_at)
        )
    """)

    # ---------------- diagnostics ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_warnings (
            run_id  TEXT NOT NULL,
            code    TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            detail  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (run_id, code)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_errors (
            run_id        TEXT NOT NULL,
            seq           INTEGER NOT NULL,
            code          TEXT NOT NULL,
            message       TEXT NOT NULL DEFAULT '',
            instrument_id TEXT,
            occurred_at   TEXT,
            fatal         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, seq)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_risk_events (
            run_id       TEXT NOT NULL,
            seq          INTEGER NOT NULL,
            kind         TEXT NOT NULL,     -- 'rejected' | 'modified'
            occurred_at  TEXT,
            proposal_id  TEXT,
            reason       TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (run_id, seq)
        )
    """)

    # ---------------- indexes ----------------

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_runs_backtest ON backtest_runs(backtest_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_runs_status ON backtest_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_runs_fingerprint ON backtest_runs(config_fingerprint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_orders_run ON simulated_orders(run_id, state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_orders_instrument ON simulated_orders(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_fills_run ON simulated_fills(run_id, filled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_fills_order ON simulated_fills(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id, exit_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_trades_instrument ON backtest_trades(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_trades_signal ON backtest_trades(entry_signal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_equity_run ON backtest_equity(run_id, timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_attr_dim ON backtest_attribution(run_id, dimension)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bt_risk_events_run ON backtest_risk_events(run_id, kind)")

    conn.commit()
