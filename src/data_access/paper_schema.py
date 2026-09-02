"""
src/data_access/paper_schema.py
------------------------------------
SQL persistence for Phase 13 (paper trading).

WHY THIS IS THE PHASE WHERE PERSISTENCE MATTERS MOST
--------------------------------------------------------
A backtest runs start to finish in one process; if it crashes you re-run
it. A paper session cannot be re-run — it advances with real time, and
the ticks it already processed happened. So its state has to survive a
restart, and spec §78 requires the latest valid state to be
reconstructable from what was persisted.

That is why orders, fills, snapshots and events are all durable, and why
`paper_checkpoints` exists: replaying a long session's entire event log
to recover is possible but slow, so a checkpoint records the ledger
state directly and recovery replays only what followed it (spec §80).

THE AUDIT TRAIL IS APPEND-ONLY
----------------------------------
`paper_events`, `paper_control_actions` and `paper_reconciliations`
are never updated in place. Spec §74 requires audit logs that ordinary
operation cannot edit, and the cheapest enforcement is that no code
path issues an UPDATE against them.

IDEMPOTENCY IS ENFORCED BY THE SCHEMA
-----------------------------------------
`paper_orders.idempotency_key` and `paper_fills.idempotency_key` carry
UNIQUE indexes. In-memory checks catch duplicates within a process; the
index catches them across processes, restarts and concurrent
invocations, which is where duplicates actually come from.

NOTHING HERE REFERENCES A BROKER
------------------------------------
No account credential, no venue, no external order id, no connection
record. `venue` exists and its only value is 'paper'.

SAFE TO RUN REPEATEDLY: every statement is CREATE TABLE / CREATE INDEX
IF NOT EXISTS.
"""

import sqlite3


def initialize_paper_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase 13 tables and indexes. Idempotent."""

    # ---------------- accounts and sessions ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            account_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            base_currency   TEXT NOT NULL DEFAULT 'USD',
            initial_capital REAL NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            account_type    TEXT NOT NULL DEFAULT 'long_only',
            -- Increments on reset. Old generations keep their history
            -- (spec §63) rather than being wiped.
            generation      INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT,
            metadata_json   TEXT NOT NULL DEFAULT '{}'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_sessions (
            session_id           TEXT PRIMARY KEY,
            account_id           TEXT NOT NULL,
            name                 TEXT NOT NULL,
            status               TEXT NOT NULL DEFAULT 'created',
            config_json          TEXT NOT NULL DEFAULT '{}',
            config_fingerprint   TEXT NOT NULL DEFAULT '',
            clock_kind           TEXT NOT NULL DEFAULT 'system',

            -- version identity, mirroring Phase 12's RunIdentity
            risk_engine_version  TEXT,
            constraint_set_version TEXT,
            cost_model_version   TEXT,
            slippage_model_version TEXT,
            execution_model_version TEXT,
            strategy_version     TEXT,
            code_version         TEXT,

            started_at           TEXT,
            ended_at             TEXT,
            last_tick_at         TEXT,
            ticks_processed      INTEGER NOT NULL DEFAULT 0,
            notes                TEXT NOT NULL DEFAULT '',
            created_at           TEXT
        )
    """)

    # ---------------- execution records ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_orders (
            order_id             TEXT PRIMARY KEY,
            session_id           TEXT NOT NULL,
            account_id           TEXT NOT NULL,
            instrument_id        TEXT NOT NULL,
            side                 TEXT NOT NULL,
            quantity             REAL NOT NULL,
            order_type           TEXT NOT NULL DEFAULT 'market',
            time_in_force        TEXT NOT NULL DEFAULT 'day',
            limit_price          REAL,
            stop_price           REAL,

            state                TEXT NOT NULL,
            filled_quantity      REAL NOT NULL DEFAULT 0,
            average_fill_price   REAL,
            reject_reason        TEXT,
            reject_detail        TEXT NOT NULL DEFAULT '',

            -- provenance: the chain spec §29 requires to stay queryable
            idempotency_key      TEXT NOT NULL DEFAULT '',
            signal_id            TEXT,
            decision_id          TEXT,
            intent_id            TEXT,
            strategy_id          TEXT,
            model_version        TEXT,
            target_weight        REAL,

            information_cutoff   TEXT,
            decided_at           TEXT,
            created_at           TEXT,
            accepted_at          TEXT,
            terminal_at          TEXT,
            expires_at           TEXT,

            execution_model_version TEXT NOT NULL DEFAULT 'paper-exec-v1',
            note                 TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_fills (
            fill_id          TEXT PRIMARY KEY,
            session_id       TEXT NOT NULL,
            order_id         TEXT NOT NULL,
            account_id       TEXT NOT NULL,
            instrument_id    TEXT NOT NULL,
            side             TEXT NOT NULL,
            quantity         REAL NOT NULL,
            -- the bar price before slippage, and what was charged after
            reference_price  REAL NOT NULL,
            price            REAL NOT NULL,
            commission       REAL NOT NULL DEFAULT 0,
            slippage_cost    REAL NOT NULL DEFAULT 0,
            -- only ever 'paper'; the column a future broker phase widens
            venue            TEXT NOT NULL DEFAULT 'paper',
            execution_model_version TEXT NOT NULL DEFAULT 'paper-exec-v1',
            slippage_model_version  TEXT NOT NULL DEFAULT 'slip-v1',
            cost_model_version      TEXT NOT NULL DEFAULT 'cost-v1',
            bar_timestamp    TEXT,
            participation    REAL,
            is_partial       INTEGER NOT NULL DEFAULT 0,
            -- the bar spanned both a stop and a limit, so intrabar
            -- ordering is unknowable from OHLC
            intrabar_ambiguous INTEGER NOT NULL DEFAULT 0,
            idempotency_key  TEXT NOT NULL DEFAULT '',
            filled_at        TEXT NOT NULL
        )
    """)

    # ---------------- state ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_snapshots (
            snapshot_id       TEXT PRIMARY KEY,
            session_id        TEXT NOT NULL,
            account_id        TEXT NOT NULL,
            at                TEXT NOT NULL,
            equity            REAL,
            cash              REAL,
            positions_value   REAL,
            gross_exposure    REAL,
            net_exposure      REAL,
            long_exposure     REAL,
            short_exposure    REAL,
            leverage          REAL,
            realized_pnl      REAL,
            unrealized_pnl    REAL,
            drawdown          REAL,
            open_positions    INTEGER NOT NULL DEFAULT 0,
            unpriced_positions INTEGER NOT NULL DEFAULT 0,
            data_freshness    TEXT,
            health            TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            session_id      TEXT NOT NULL,
            instrument_id   TEXT NOT NULL,
            quantity        REAL NOT NULL,
            average_cost    REAL NOT NULL,
            opened_at       TEXT,
            entry_signal_id TEXT,
            updated_at      TEXT,
            PRIMARY KEY (session_id, instrument_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_checkpoints (
            checkpoint_id  TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            at             TEXT NOT NULL,
            -- The ledger state, so recovery does not have to replay the
            -- whole event log (spec §80).
            cash           REAL NOT NULL,
            realized_pnl   REAL NOT NULL DEFAULT 0,
            total_costs    REAL NOT NULL DEFAULT 0,
            total_slippage REAL NOT NULL DEFAULT 0,
            traded_notional REAL NOT NULL DEFAULT 0,
            positions_json TEXT NOT NULL DEFAULT '[]',
            ticks_processed INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT
        )
    """)

    # ---------------- audit and diagnostics (append-only) ----------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_events (
            session_id    TEXT NOT NULL,
            sequence      INTEGER NOT NULL,
            at            TEXT NOT NULL,
            kind          TEXT NOT NULL,
            instrument_id TEXT,
            order_id      TEXT,
            fill_id       TEXT,
            signal_id     TEXT,
            message       TEXT NOT NULL DEFAULT '',
            payload_json  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, sequence)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_alerts (
            alert_id     TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            code         TEXT NOT NULL,
            severity     TEXT NOT NULL,
            message      TEXT NOT NULL DEFAULT '',
            detail       TEXT NOT NULL DEFAULT '',
            at           TEXT,
            acknowledged INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_health (
            session_id        TEXT NOT NULL,
            at                TEXT NOT NULL,
            component         TEXT NOT NULL,
            state             TEXT NOT NULL,
            detail            TEXT NOT NULL DEFAULT '',
            latency_ms        REAL,
            last_heartbeat_at TEXT,
            PRIMARY KEY (session_id, at, component)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_reconciliations (
            session_id         TEXT NOT NULL,
            at                 TEXT NOT NULL,
            checks_performed   INTEGER NOT NULL DEFAULT 0,
            orders_examined    INTEGER NOT NULL DEFAULT 0,
            fills_examined     INTEGER NOT NULL DEFAULT 0,
            positions_examined INTEGER NOT NULL DEFAULT 0,
            is_clean           INTEGER NOT NULL DEFAULT 1,
            discrepancies_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (session_id, at)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_control_actions (
            action_id      TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            action         TEXT NOT NULL,
            at             TEXT NOT NULL,
            actor          TEXT NOT NULL DEFAULT 'system',
            reason         TEXT NOT NULL DEFAULT '',
            previous_value TEXT,
            new_value      TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_latency (
            session_id   TEXT NOT NULL,
            at           TEXT NOT NULL,
            stage        TEXT NOT NULL,
            milliseconds REAL NOT NULL,
            PRIMARY KEY (session_id, at, stage)
        )
    """)

    # ---------------- indexes ----------------

    # Idempotency across processes, not just within one (spec §12).
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_idem "
                 "ON paper_orders(session_id, idempotency_key) "
                 "WHERE idempotency_key <> ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_fills_idem "
                 "ON paper_fills(idempotency_key) WHERE idempotency_key <> ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_sessions_account ON paper_sessions(account_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_sessions_status ON paper_sessions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_session ON paper_orders(session_id, state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_instrument ON paper_orders(instrument_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_orders_signal ON paper_orders(signal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_fills_session ON paper_fills(session_id, filled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_fills_order ON paper_fills(order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_snapshots_session ON paper_snapshots(session_id, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_events_session ON paper_events(session_id, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_events_kind ON paper_events(session_id, kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_alerts_session ON paper_alerts(session_id, severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_checkpoints_session ON paper_checkpoints(session_id, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_health_session ON paper_health(session_id, at)")

    conn.commit()
